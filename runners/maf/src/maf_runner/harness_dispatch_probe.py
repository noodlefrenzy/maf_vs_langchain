"""Hermetic probe for MAF Harness per-dispatch model accounting."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from agent_framework import Agent, tool

from agent_lab.model_compat_contracts import (
    MODEL_NAME,
    ModelFixture,
    compatibility_model_profile,
    fixture_prompt,
)
from agent_lab.model_request_policy import maf_request_options
from maf_runner.chat_telemetry import InstrumentedChatClient


@tool(description="Look up an incident by identifier.")
def lookup_incident(incident_id: str) -> str:
    return f"{incident_id} is stable"


class _RecordingTelemetry:
    def __init__(self, model_call_limit: int | None = None) -> None:
        self.model_call_limit = model_call_limit
        self.model_starts: list[dict[str, str]] = []
        self.model_finishes: list[dict[str, str | int]] = []

    async def handoff(self, **_: Any) -> None:
        return None

    async def model_started(self, *, actor: str, model: str) -> None:
        if self.model_call_limit is not None and len(self.model_starts) >= self.model_call_limit:
            raise RuntimeError("model_call_budget_exhausted")
        self.model_starts.append({"actor": actor, "model": model})

    async def model_finished(
        self,
        *,
        actor: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        parser_outcome: str = "provider_response_accepted",
        provider_request_id: str | None = None,
        queue_seconds: float | None = None,
        time_to_first_token_seconds: float | None = None,
        generation_seconds: float | None = None,
    ) -> None:
        self.model_finishes.append(
            {
                "actor": actor,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "reasoning_tokens": reasoning_tokens,
                "parser_outcome": parser_outcome,
                "provider_request_id": provider_request_id,
                "queue_seconds": queue_seconds,
                "time_to_first_token_seconds": time_to_first_token_seconds,
                "generation_seconds": generation_seconds,
            }
        )

    async def tool_started(self, **_: Any) -> None:
        return None

    async def tool_finished(self, **_: Any) -> None:
        return None


async def run(base_url: str, model_call_limit: int | None = None) -> dict[str, Any]:
    telemetry = _RecordingTelemetry(model_call_limit)
    profile = compatibility_model_profile(base_url, MODEL_NAME)
    client = InstrumentedChatClient(
        actor="dispatch_probe",
        profile=profile,
        telemetry=telemetry,
        framework="maf-harness-probe",
        model_call_limit=model_call_limit or 6,
    )
    agent = Agent(client, name="dispatch_probe", tools=[lookup_incident])
    response = None
    error = None
    try:
        response = await agent.run(
            fixture_prompt(ModelFixture.MULTI_TURN),
            options={
                **maf_request_options(profile),
                "tool_choice": "auto",
                "allow_multiple_tool_calls": False,
            },
        )
    except RuntimeError as exc:
        error = str(exc)
    finally:
        await client.client.close()
    return {
        "text": response.text if response is not None else None,
        "error": error,
        "model_calls": client.model_calls,
        "input_tokens": client.input_tokens,
        "output_tokens": client.output_tokens,
        "model_starts": telemetry.model_starts,
        "model_finishes": telemetry.model_finishes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model-call-limit", type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run(args.base_url, args.model_call_limit)),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
