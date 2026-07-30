"""Framework-neutral contracts used by every runner and evaluator."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"


class FrameworkLane(StrEnum):
    """A comparison lane with a distinct runtime and orchestration style."""

    MAF_WORKFLOW = "maf_workflow"
    MAF_HARNESS = "maf_harness"
    LANGGRAPH = "langgraph"
    DEEP_AGENTS = "deep_agents"
    SINGLE_AGENT_BASELINE = "single_agent_baseline"
    SCRIPTED_ORACLE = "scripted_oracle"


class EventKind(StrEnum):
    """Portable event vocabulary emitted by framework-specific adapters."""

    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    MODEL_REQUESTED = "model.requested"
    MODEL_RESPONDED = "model.responded"
    TEXT_DELTA = "model.text_delta"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    TOOL_FAILED = "tool.failed"
    HANDOFF = "agent.handoff"
    STATE_UPDATED = "state.updated"
    CHECKPOINT_SAVED = "checkpoint.saved"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    ARTIFACT_WRITTEN = "artifact.written"
    METRIC_RECORDED = "metric.recorded"
    ERROR = "error"


class EventVisibility(StrEnum):
    """Controls which event data may be shown or retained."""

    PUBLIC = "public"
    SENSITIVE = "sensitive"
    INTERNAL = "internal"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


class TerminalStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    MODEL_FAILURE = "model_failure"
    TOOL_SERVICE_FAILURE = "tool_service_failure"
    RUNNER_FAILURE = "runner_failure"
    CANCELLED = "cancelled"
    INVALID_PROVENANCE = "invalid_provenance"


class NormalizedEvent(BaseModel):
    """One append-only event in a run bundle.

    Framework-native events remain available in native traces. This envelope is
    deliberately small so paired runs can be rendered and scored uniformly.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    sequence: int = Field(ge=0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    monotonic_ns: int | None = Field(default=None, ge=0)
    lane: FrameworkLane
    kind: EventKind
    actor: str | None = None
    parent_event_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None
    visibility: EventVisibility = EventVisibility.PUBLIC
    payload: dict[str, Any] = Field(default_factory=dict)


class ModelProfile(BaseModel):
    """Model settings that must be explicit and shared across paired lanes."""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    api_style: Literal["chat_completions"] = "chat_completions"
    temperature: float = Field(ge=0, le=2)
    top_p: float = Field(gt=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    min_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float = Field(default=0, ge=-2, le=2)
    repetition_penalty: float = Field(default=1, gt=0, le=2)
    max_output_tokens: int = Field(gt=0)
    context_window_tokens: int = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0)
    seed: int | None = None
    thinking: bool = True


class Budget(BaseModel):
    """Hard per-run limits enforced outside the framework."""

    model_config = ConfigDict(extra="forbid")

    wall_clock_seconds: int = Field(gt=0)
    model_calls: int = Field(gt=0)
    tool_calls: int = Field(gt=0)
    output_tokens: int = Field(gt=0)
    approval_requests: int = Field(ge=0)


class ExperimentSpec(BaseModel):
    """Validated experiment configuration loaded from TOML."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    experiment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    scenario: Literal["incident_response"]
    lanes: list[FrameworkLane] = Field(min_length=1)
    seeds: list[int] = Field(min_length=1)
    repetitions: int = Field(gt=0)
    order: Literal["randomized", "alternating", "fixed"]
    model_profile: str
    fault_profile: Literal["none", "standard", "adversarial"]
    budget: Budget
    capture_sensitive: bool = False
    capture_reasoning: Literal[False] = False


class RunManifest(BaseModel):
    """Immutable provenance captured before a run starts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    run_id: UUID = Field(default_factory=uuid4)
    experiment_id: str
    lane: FrameworkLane
    scenario: str
    scenario_seed: int
    repetition: int = Field(ge=0)
    model: ModelProfile
    budget: Budget
    source_commit: str
    framework_versions: dict[str, str]
    prompt_bundle_sha256: str
    tool_catalog_sha256: str
    world_manifest_sha256: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    host_fingerprint: dict[str, str] = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    """One auditable claim derived from an MCP result."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    source_role: str
    source_tool: str
    claim: str
    observation: str
    world_tick: int = Field(ge=0)
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    supports: Literal["root_cause", "mitigation", "recovery"]


class IncidentReport(BaseModel):
    """Structured terminal artifact shared by every framework lane."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    case_id: str
    root_cause: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceItem] = Field(min_length=2)
    mitigation: str
    mitigation_request_id: str | None = None
    approval_decision: ApprovalDecision | None = None
    recovery_checks: list[str] = Field(default_factory=list)
    customer_impact: str
    residual_risk: str


class RunTerminal(BaseModel):
    """Mutable terminal record written once after execution stops."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    run_id: UUID
    status: TerminalStatus
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str
    used_model_calls: int = Field(ge=0)
    used_tool_calls: int = Field(ge=0)
    used_output_tokens: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
