"""Instrumented non-streaming LangChain chat model shared by native lanes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agent_lab.contracts import ModelProfile
from agent_lab.model_request_policy import langchain_model_kwargs, model_request_headers
from agent_lab.workflow_telemetry import WorkflowTelemetry


@dataclass
class ModelStats:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class ProviderMetricsChatOpenAI(ChatOpenAI):
    """Retain vLLM's content-free per-request timing extension."""

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> Any:
        result = super()._create_chat_result(response, generation_info)
        payload = response if isinstance(response, dict) else response.model_dump(warnings=False)
        provider = _provider_telemetry(payload)
        if provider:
            result.llm_output = {**(result.llm_output or {}), "provider_telemetry": provider}
            for generation in result.generations:
                generation.message.response_metadata["provider_telemetry"] = provider
        return result


class InstrumentedChatOpenAI(ProviderMetricsChatOpenAI):
    """ChatOpenAI adapter with normalized call/token telemetry."""

    _actor: str = PrivateAttr()
    _lab_profile: ModelProfile = PrivateAttr()
    _telemetry: WorkflowTelemetry = PrivateAttr()
    _stats: ModelStats = PrivateAttr(default_factory=ModelStats)

    def __init__(
        self,
        *,
        actor: str,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
        framework: str,
    ) -> None:
        super().__init__(
            api_key="agent-lab-local",
            default_headers=model_request_headers(framework, actor),
            disable_streaming=True,
            **langchain_model_kwargs(profile),
        )
        self._actor = actor
        self._lab_profile = profile
        self._telemetry = telemetry

    @property
    def lab_stats(self) -> ModelStats:
        return self._stats

    async def _agenerate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        await self._telemetry.model_started(
            actor=self._actor,
            model=self._lab_profile.name,
        )
        self._stats.model_calls += 1
        response = await super()._agenerate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        input_tokens = 0
        output_tokens = 0
        cached_input_tokens = 0
        reasoning_tokens = 0
        cached_input_reported = False
        reasoning_reported = False
        parser_outcome = "provider_response_accepted"
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                if getattr(message, "invalid_tool_calls", None):
                    parser_outcome = "tool_parse_failed"
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
                input_details = usage.get("input_token_details") or {}
                output_details = usage.get("output_token_details") or {}
                if input_details.get("cache_read") is not None:
                    cached_input_reported = True
                    cached_input_tokens += int(input_details["cache_read"])
                if output_details.get("reasoning") is not None:
                    reasoning_reported = True
                    reasoning_tokens += int(output_details["reasoning"])
        if input_tokens == 0 and output_tokens == 0:
            token_usage = (response.llm_output or {}).get("token_usage", {})
            input_tokens = int(token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)))
            output_tokens = int(
                token_usage.get("completion_tokens", token_usage.get("output_tokens", 0))
            )
            prompt_details = token_usage.get("prompt_tokens_details") or {}
            completion_details = token_usage.get("completion_tokens_details") or {}
            if prompt_details.get("cached_tokens") is not None:
                cached_input_reported = True
                cached_input_tokens = int(prompt_details["cached_tokens"])
            if completion_details.get("reasoning_tokens") is not None:
                reasoning_reported = True
                reasoning_tokens = int(completion_details["reasoning_tokens"])
        provider = (response.llm_output or {}).get("provider_telemetry", {})
        await self._telemetry.model_finished(
            actor=self._actor,
            model=self._lab_profile.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=(cached_input_tokens if cached_input_reported else None),
            reasoning_tokens=reasoning_tokens if reasoning_reported else None,
            parser_outcome=parser_outcome,
            **provider,
        )
        self._stats.input_tokens += input_tokens
        self._stats.output_tokens += output_tokens
        return response

    async def _astream(self, *_: Any, **__: Any) -> Any:
        """Tripwire: public streaming must fall through instrumented `_agenerate`."""

        raise ValueError("the protocol adapter requires non-streaming model calls")
        yield  # pragma: no cover


def _provider_telemetry(payload: dict[str, Any]) -> dict[str, str | float | None]:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    normalized: dict[str, str | float | None] = {
        "provider_request_id": payload.get("id") if isinstance(payload.get("id"), str) else None
    }
    for source, destination in (
        ("queue_time_ms", "queue_seconds"),
        ("time_to_first_token_ms", "time_to_first_token_seconds"),
        ("generation_time_ms", "generation_seconds"),
    ):
        observed = metrics.get(source)
        if isinstance(observed, int | float) and not isinstance(observed, bool) and observed >= 0:
            normalized[destination] = float(observed) / 1000
    return normalized


def provider_telemetry_from_message(message: Any) -> dict[str, str | float | None]:
    if not isinstance(message, AIMessage):
        return {}
    value = message.response_metadata.get("provider_telemetry")
    return value if isinstance(value, dict) else {}
