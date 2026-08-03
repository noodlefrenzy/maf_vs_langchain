"""Hermetic probe for Deep Agents Harness per-dispatch model accounting."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from deepagents import create_deep_agent
from langchain_core.tools import tool

from agent_lab.model_compat_contracts import (
    MODEL_NAME,
    ModelFixture,
    compatibility_model_profile,
    fixture_prompt,
)
from langgraph_runner.chat_telemetry import InstrumentedChatOpenAI


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
    ) -> None:
        self.model_finishes.append(
            {
                "actor": actor,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    async def tool_started(self, **_: Any) -> None:
        return None

    async def tool_finished(self, **_: Any) -> None:
        return None


async def run(base_url: str, model_call_limit: int | None = None) -> dict[str, Any]:
    telemetry = _RecordingTelemetry(model_call_limit)
    profile = compatibility_model_profile(base_url, MODEL_NAME)
    model = InstrumentedChatOpenAI(
        actor="dispatch_probe",
        profile=profile,
        telemetry=telemetry,
        framework="deep-agents-probe",
    )
    graph = create_deep_agent(
        model=model,
        tools=[lookup_incident],
        system_prompt="Use the supplied incident tool when the user requests it.",
        name="dispatch_probe",
    )
    text = None
    stream_text = None
    error = None
    try:
        state = await graph.ainvoke(
            {"messages": [{"role": "user", "content": fixture_prompt(ModelFixture.MULTI_TURN)}]}
        )
        text = state["messages"][-1].text
        chunks = [chunk async for chunk in model.astream(fixture_prompt(ModelFixture.STREAMING))]
        stream_text = "".join(chunk.text for chunk in chunks)
    except RuntimeError as exc:
        error = str(exc)
    return {
        "text": text,
        "stream_text": stream_text,
        "error": error,
        "model_calls": model.lab_stats.model_calls,
        "input_tokens": model.lab_stats.input_tokens,
        "output_tokens": model.lab_stats.output_tokens,
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
