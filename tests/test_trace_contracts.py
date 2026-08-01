from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_lab.contracts import EventKind, FrameworkLane, NativeTracingProfile, NormalizedEvent
from agent_lab.trace_contracts import (
    NativeSpanRecord,
    TraceOperation,
    classify_native_span,
    compute_trace_completeness,
    structural_span_attributes,
)


def _event(sequence: int, kind: EventKind) -> NormalizedEvent:
    return NormalizedEvent(
        run_id=uuid4(),
        sequence=sequence,
        lane=FrameworkLane.LANGGRAPH,
        kind=kind,
    )


def _span(operation: TraceOperation) -> NativeSpanRecord:
    return NativeSpanRecord(
        run_id=uuid4(),
        lane=FrameworkLane.LANGGRAPH,
        trace_id="1" * 32,
        span_id=f"{len(operation.value):016x}",
        name=operation.value,
        span_kind="internal",
        status_code="unset",
        started_unix_nano=1,
        ended_unix_nano=2,
        instrumentation_scope="test",
        operation=operation,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:6006/v1/traces",
        "http://collector.example/v1/traces",
        "http://127.0.0.1:6006/not-traces",
        "http://user:secret@127.0.0.1:6006/v1/traces",
    ],
)
def test_tracing_profile_rejects_non_loopback_or_unsafe_collectors(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        NativeTracingProfile(collector_endpoint=endpoint)


def test_structural_projection_drops_prompts_arguments_and_results() -> None:
    projected = structural_span_attributes(
        {
            "agent.lab.run_id": "run-1",
            "gen_ai.tool.name": "lookup_trace",
            "input.value": "secret prompt",
            "tool.arguments": '{"token":"secret"}',
            "tool.result": "private result",
        }
    )

    assert projected == {
        "agent.lab.run_id": "run-1",
        "gen_ai.tool.name": "lookup_trace",
    }


def test_native_semantics_are_classified_without_content_fields() -> None:
    assert classify_native_span("chat", {"openinference.span.kind": "LLM"}) is (
        TraceOperation.MODEL
    )
    assert classify_native_span("call", {"mcp.method.name": "tools/call"}) is (
        TraceOperation.TOOL
    )
    assert classify_native_span("approval interrupt", {}) is TraceOperation.APPROVAL


def test_trace_completeness_retains_operation_level_gaps() -> None:
    events = [
        _event(0, EventKind.RUN_STARTED),
        _event(1, EventKind.MODEL_REQUESTED),
        _event(2, EventKind.TOOL_REQUESTED),
        _event(3, EventKind.APPROVAL_REQUESTED),
    ]
    spans = [
        _span(TraceOperation.RUN),
        _span(TraceOperation.MODEL),
        _span(TraceOperation.TOOL),
    ]

    completeness = compute_trace_completeness(events, spans)

    assert completeness.expected == 4
    assert completeness.covered == 3
    assert completeness.ratio == 0.75
    assert completeness.operations[TraceOperation.APPROVAL].expected == 1
    assert completeness.operations[TraceOperation.APPROVAL].observed == 0
