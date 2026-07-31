"""Shared fixtures and result contracts for model-client compatibility probes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

MODEL_NAME = "agent-lab-protocol-stub"
TEMPERATURE = 0.2
TOP_P = 0.9
MAX_OUTPUT_TOKENS = 64
REQUEST_TIMEOUT_SECONDS = 5.0
SEED = 1729


class ModelFixture(StrEnum):
    """Model interaction shapes that both native clients must support."""

    PLAIN = "plain"
    STRUCTURED = "structured"
    STREAMING = "streaming"
    ONE_TOOL = "one_tool"
    MULTI_TOOL = "multi_tool"
    MULTI_TURN = "multi_turn"
    MALFORMED_TOOL = "malformed_tool"


SUCCESS_FIXTURES = tuple(
    fixture for fixture in ModelFixture if fixture is not ModelFixture.MALFORMED_TOOL
)


class StructuredAnswer(BaseModel):
    """Structured response expected from both framework clients."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    severity: int


def fixture_prompt(fixture: ModelFixture) -> str:
    """Return the public deterministic prompt marker consumed by the stub."""

    instructions = {
        ModelFixture.PLAIN: "Reply exactly with: plain response",
        ModelFixture.STRUCTURED: "Return summary 'stable' and integer severity 1.",
        ModelFixture.STREAMING: "Reply exactly with: stream response",
        ModelFixture.ONE_TOOL: (
            "Call lookup_incident exactly once with incident_id INC-42; do not answer in text."
        ),
        ModelFixture.MULTI_TOOL: (
            "Call lookup_incident with INC-42 and list_services with platform in parallel."
        ),
        ModelFixture.MULTI_TURN: (
            "Call lookup_incident with INC-42, then reply exactly with: multi-turn complete"
        ),
        ModelFixture.MALFORMED_TOOL: "Exercise malformed tool-argument parser classification.",
    }
    return f"[model-wire-fixture:{fixture.value}] {instructions[fixture]}"


def explicit_request_settings() -> dict[str, Any]:
    """Return the sampling and ceiling settings shared by both runners."""

    return {
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "seed": SEED,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
    }


def openai_tool_schemas() -> list[dict[str, Any]]:
    """Return fresh OpenAI function definitions shared by direct-client fixtures."""

    return [
        {
            "type": "function",
            "function": {
                "name": "lookup_incident",
                "description": "Look up an incident by identifier.",
                "parameters": {
                    "type": "object",
                    "properties": {"incident_id": {"type": "string"}},
                    "required": ["incident_id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_services",
                "description": "List services owned by a team.",
                "parameters": {
                    "type": "object",
                    "properties": {"team": {"type": "string"}},
                    "required": ["team"],
                    "additionalProperties": False,
                },
            },
        },
    ]
