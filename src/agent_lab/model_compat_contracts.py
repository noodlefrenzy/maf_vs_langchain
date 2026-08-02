"""Shared fixtures and result contracts for model-client compatibility probes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_lab.contracts import ModelProfile

MODEL_NAME = "agent-lab-protocol-stub"
TEMPERATURE = 0.2
TOP_P = 0.9
TOP_K = 23
MIN_P = 0.07
PRESENCE_PENALTY = 0.15
REPETITION_PENALTY = 1.1
MAX_OUTPUT_TOKENS = 64
CONTEXT_WINDOW_TOKENS = 4096
REQUEST_TIMEOUT_SECONDS = 5.0
SEED = 1729
THINKING = False
MODEL_ACTOR_PROBES = (
    "workflow_specialist",
    "workflow_commander",
    "harness_specialist",
    "harness_commander",
)


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
        "top_k": TOP_K,
        "min_p": MIN_P,
        "presence_penalty": PRESENCE_PENALTY,
        "repetition_penalty": REPETITION_PENALTY,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "seed": SEED,
        "thinking": THINKING,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
    }


def compatibility_model_profile(base_url: str, model: str) -> ModelProfile:
    """Build the non-default profile exercised by both native wire probes."""

    return ModelProfile(
        name=model,
        base_url=base_url,
        temperature=TEMPERATURE,
        top_p=TOP_P,
        top_k=TOP_K,
        min_p=MIN_P,
        presence_penalty=PRESENCE_PENALTY,
        repetition_penalty=REPETITION_PENALTY,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        context_window_tokens=CONTEXT_WINDOW_TOKENS,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        seed=SEED,
        thinking=THINKING,
    )


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
