"""Framework-neutral native-span ledger and trace-completeness contracts."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import (
    SCHEMA_VERSION,
    EventKind,
    FrameworkLane,
    NativeTracingProfile,
    NormalizedEvent,
)


class TraceOperation(StrEnum):
    RUN = "run"
    AGENT = "agent"
    MODEL = "model"
    TOOL = "tool"
    APPROVAL = "approval"
    CHECKPOINT = "checkpoint"
    OTHER = "other"


MEASURED_TRACE_OPERATIONS = tuple(
    operation for operation in TraceOperation if operation is not TraceOperation.OTHER
)


class TraceCollectorStatus(StrEnum):
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    EXPORTED = "exported"
    EXPORT_FAILED = "export_failed"


TraceId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
TraceDiagnostic = Annotated[str, Field(min_length=1, max_length=256)]


class NativeTraceProfileRecord(BaseModel):
    """Immutable bundle input controlling runner-native capture and export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    run_id: UUID
    tracing: NativeTracingProfile
    capture_sensitive: bool = False
    capture_reasoning: Literal[False] = False


class NativeSpanRecord(BaseModel):
    """Redacted structural projection of one framework-native OpenTelemetry span."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    run_id: UUID
    lane: FrameworkLane
    trace_id: TraceId
    span_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{16}$")
    name: str = Field(min_length=1, max_length=256)
    span_kind: str = Field(min_length=1, max_length=64)
    status_code: str = Field(min_length=1, max_length=64)
    started_unix_nano: int = Field(ge=0)
    ended_unix_nano: int = Field(ge=0)
    instrumentation_scope: str = Field(min_length=1, max_length=256)
    operation: TraceOperation
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_projection(self) -> NativeSpanRecord:
        if self.ended_unix_nano < self.started_unix_nano:
            raise ValueError("span end time cannot precede its start time")
        if structural_span_attributes(self.attributes) != self.attributes:
            raise ValueError("span attributes must use the redacted structural projection")
        return self


class TraceOperationCompleteness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected: int = Field(ge=0)
    observed: int = Field(ge=0)
    covered: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)


class TraceCompleteness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected: int = Field(ge=0)
    observed: int = Field(ge=0)
    covered: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)
    operations: dict[TraceOperation, TraceOperationCompleteness]


class NativeTraceLink(BaseModel):
    """Bundle sidecar linking normalized evidence to local and Phoenix native traces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    run_id: UUID
    lane: FrameworkLane
    enabled: bool
    capture_sensitive: bool
    capture_reasoning: Literal[False] = False
    collector_endpoint: str | None = None
    collector_status: TraceCollectorStatus
    phoenix_base_url: str | None = None
    phoenix_trace_url: str | None = None
    project_name: str | None = None
    primary_trace_id: TraceId | None = None
    trace_ids: list[TraceId] = Field(default_factory=list)
    span_ledger_path: str | None = None
    span_ledger_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    span_count: int = Field(ge=0)
    completeness: TraceCompleteness
    diagnostics: list[TraceDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_state(self) -> NativeTraceLink:
        if self.enabled == (self.collector_status is TraceCollectorStatus.DISABLED):
            raise ValueError("collector status must be disabled exactly when tracing is disabled")
        if not self.enabled and (
            self.collector_endpoint is not None
            or self.phoenix_base_url is not None
            or self.phoenix_trace_url is not None
            or self.project_name is not None
            or self.span_count
        ):
            raise ValueError("disabled tracing cannot declare collector or span evidence")
        if len(set(self.trace_ids)) != len(self.trace_ids):
            raise ValueError("trace IDs must be unique")
        if self.primary_trace_id is not None and self.primary_trace_id not in self.trace_ids:
            raise ValueError("primary trace ID must appear in trace_ids")
        ledger_fields = (self.span_ledger_path, self.span_ledger_sha256)
        if self.span_count and any(value is None for value in ledger_fields):
            raise ValueError("non-empty span ledgers require a path and digest")
        if not self.span_count and any(value is not None for value in ledger_fields):
            raise ValueError("empty span ledgers cannot declare a path or digest")
        if self.span_count == 0 and self.trace_ids:
            raise ValueError("an empty span ledger cannot declare trace IDs")
        if self.phoenix_trace_url is not None and self.primary_trace_id is None:
            raise ValueError("Phoenix trace URL requires a primary trace ID")
        return self


_EXPECTED_EVENT_OPERATION = {
    EventKind.RUN_STARTED: TraceOperation.RUN,
    EventKind.AGENT_STARTED: TraceOperation.AGENT,
    EventKind.MODEL_REQUESTED: TraceOperation.MODEL,
    EventKind.TOOL_REQUESTED: TraceOperation.TOOL,
    EventKind.APPROVAL_REQUESTED: TraceOperation.APPROVAL,
    EventKind.CHECKPOINT_SAVED: TraceOperation.CHECKPOINT,
}

_STRUCTURAL_ATTRIBUTE_KEYS = frozenset(
    {
        "agent.lab.operation",
        "agent.lab.run_id",
        "agent.lab.lane",
        "gen_ai.operation.name",
        "gen_ai.agent.id",
        "gen_ai.agent.name",
        "gen_ai.tool.name",
        "mcp.method.name",
        "openinference.span.kind",
        "rpc.method",
        "server.address",
    }
)


def structural_span_attributes(attributes: Any) -> dict[str, str | int | float | bool]:
    """Retain only bounded structural attributes, never prompts, arguments, or results."""

    if not isinstance(attributes, dict):
        try:
            attributes = dict(attributes or {})
        except (TypeError, ValueError):
            return {}
    projected: dict[str, str | int | float | bool] = {}
    for key in sorted(_STRUCTURAL_ATTRIBUTE_KEYS.intersection(attributes)):
        value = attributes[key]
        if isinstance(value, bool | int | float):
            projected[key] = value
        elif isinstance(value, str):
            projected[key] = value[:256]
    return projected


def classify_native_span(name: str, attributes: dict[str, Any]) -> TraceOperation:
    """Project differing native semantic conventions into a measured operation family."""

    explicit = str(attributes.get("agent.lab.operation", "")).lower()
    if explicit in {operation.value for operation in TraceOperation}:
        return TraceOperation(explicit)
    openinference_kind = str(attributes.get("openinference.span.kind", "")).lower()
    if openinference_kind == "llm":
        return TraceOperation.MODEL
    if openinference_kind == "tool":
        return TraceOperation.TOOL
    if openinference_kind in {"agent", "chain"}:
        return TraceOperation.AGENT
    gen_ai_operation = str(attributes.get("gen_ai.operation.name", "")).lower()
    if any(marker in gen_ai_operation for marker in ("chat", "completion", "generate")):
        return TraceOperation.MODEL
    if "tool" in gen_ai_operation or "mcp.method.name" in attributes:
        return TraceOperation.TOOL
    if "agent" in gen_ai_operation:
        return TraceOperation.AGENT
    lowered_name = name.lower()
    if "checkpoint" in lowered_name:
        return TraceOperation.CHECKPOINT
    if "approval" in lowered_name or "interrupt" in lowered_name:
        return TraceOperation.APPROVAL
    return TraceOperation.OTHER


def compute_trace_completeness(
    events: list[NormalizedEvent],
    spans: list[NativeSpanRecord],
) -> TraceCompleteness:
    """Compare expected normalized semantic operations with observed native spans."""

    expected = Counter(
        operation
        for event in events
        if (operation := _EXPECTED_EVENT_OPERATION.get(event.kind)) is not None
    )
    observed = Counter(span.operation for span in spans)
    operations: dict[TraceOperation, TraceOperationCompleteness] = {}
    total_expected = 0
    total_observed = 0
    total_covered = 0
    for operation in MEASURED_TRACE_OPERATIONS:
        operation_expected = expected[operation]
        operation_observed = observed[operation]
        covered = min(operation_expected, operation_observed)
        ratio = covered / operation_expected if operation_expected else 1.0
        operations[operation] = TraceOperationCompleteness(
            expected=operation_expected,
            observed=operation_observed,
            covered=covered,
            ratio=ratio,
        )
        total_expected += operation_expected
        total_observed += operation_observed
        total_covered += covered
    return TraceCompleteness(
        expected=total_expected,
        observed=total_observed,
        covered=total_covered,
        ratio=total_covered / total_expected if total_expected else 1.0,
        operations=operations,
    )
