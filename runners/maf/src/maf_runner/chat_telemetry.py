"""Instrumented non-streaming MAF chat client shared by native lanes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_framework import ChatContext, ChatMiddleware, ChatResponse
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from agent_lab.contracts import ModelProfile
from agent_lab.model_request_policy import model_request_headers
from agent_lab.workflow_telemetry import WorkflowTelemetry


class ProviderMetricsOpenAIChatCompletionClient(OpenAIChatCompletionClient):
    """Retain vLLM's content-free per-request timing extension."""

    def _get_metadata_from_chat_response(self, response: Any) -> dict[str, Any]:
        metadata = super()._get_metadata_from_chat_response(response)
        timing = _normalize_provider_timing(getattr(response, "metrics", None))
        if timing:
            metadata["provider_timing"] = timing
        return metadata


class ModelDispatchMiddleware(ChatMiddleware):
    """Count each provider dispatch below MAF's function-invocation loop."""

    def __init__(
        self,
        *,
        actor: str,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
    ) -> None:
        self.actor = actor
        self.profile = profile
        self.telemetry = telemetry
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        if context.stream:
            raise ValueError("the protocol adapter requires non-streaming model calls")
        await self.telemetry.model_started(actor=self.actor, model=self.profile.name)
        await call_next()
        if not isinstance(context.result, ChatResponse):
            raise TypeError("MAF chat middleware returned no non-streaming response")
        usage = context.result.usage_details or {}
        input_tokens = int(usage.get("input_token_count", 0))
        output_tokens = int(usage.get("output_token_count", 0))
        cached_input_tokens = _optional_usage(usage, "cache_read_input_token_count")
        reasoning_tokens = _optional_usage(usage, "reasoning_output_token_count")
        provider = provider_telemetry(context.result)
        await self.telemetry.model_finished(
            actor=self.actor,
            model=self.profile.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            **provider,
        )
        self.model_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


class InstrumentedChatClient(ProviderMetricsOpenAIChatCompletionClient):
    """MAF OpenAI client with normalized model-call telemetry and a hard loop cap."""

    def __init__(
        self,
        *,
        actor: str,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
        framework: str,
        model_call_limit: int,
        function_call_limit: int | None = None,
    ) -> None:
        dispatch = ModelDispatchMiddleware(
            actor=actor,
            profile=profile,
            telemetry=telemetry,
        )
        sdk = AsyncOpenAI(
            api_key="agent-lab-local",
            base_url=profile.base_url,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            default_headers=model_request_headers(framework, actor),
        )
        super().__init__(
            model=profile.name,
            async_client=sdk,
            function_invocation_configuration={
                "enabled": True,
                # MAF performs one final tool-disabled dispatch after exhausting
                # max_iterations. Reserve that dispatch inside the advertised
                # provider-call ceiling. Production callers use limits >= 2;
                # the one-call dispatch probe is still stopped by outer telemetry.
                "max_iterations": max(model_call_limit - 1, 1),
                "max_function_calls": (
                    function_call_limit if function_call_limit is not None else model_call_limit
                ),
            },
            middleware=[dispatch],
        )
        self._dispatch = dispatch

    @property
    def model_calls(self) -> int:
        return self._dispatch.model_calls

    @property
    def input_tokens(self) -> int:
        return self._dispatch.input_tokens

    @property
    def output_tokens(self) -> int:
        return self._dispatch.output_tokens


def _optional_usage(usage: dict, key: str) -> int | None:
    value = usage.get(key)
    return int(value) if value is not None else None


def _normalize_provider_timing(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for source, destination in (
        ("queue_time_ms", "queue_seconds"),
        ("time_to_first_token_ms", "time_to_first_token_seconds"),
        ("generation_time_ms", "generation_seconds"),
    ):
        observed = value.get(source)
        if isinstance(observed, int | float) and not isinstance(observed, bool) and observed >= 0:
            normalized[destination] = float(observed) / 1000
    return normalized


def provider_telemetry(response: ChatResponse) -> dict[str, str | float | None]:
    timing = response.additional_properties.get("provider_timing") or {}
    return {
        "provider_request_id": response.response_id,
        "queue_seconds": timing.get("queue_seconds"),
        "time_to_first_token_seconds": timing.get("time_to_first_token_seconds"),
        "generation_seconds": timing.get("generation_seconds"),
    }
