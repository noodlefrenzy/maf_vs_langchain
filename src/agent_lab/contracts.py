"""Framework-neutral contracts used by every runner and evaluator."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.scenario import MitigationActionId

SCHEMA_VERSION = "0.1.0"
Sha256Hex = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]


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


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


class AgentRole(StrEnum):
    """Stable role identifiers shared by prompts, tools, and event actors."""

    INCIDENT_COMMANDER = "incident_commander"
    TELEMETRY_INVESTIGATOR = "telemetry_investigator"
    CHANGE_INVESTIGATOR = "change_investigator"
    RELIABILITY_REVIEWER = "reliability_reviewer"


class CapabilityGroup(StrEnum):
    """MCP capability groups used to enforce role boundaries."""

    INCIDENT = "incident"
    TELEMETRY = "telemetry"
    CHANGES = "changes"
    RUNBOOKS = "runbooks"
    COMMAND = "command"


class ToolErrorCode(StrEnum):
    """Frozen, transport-independent MCP error taxonomy."""

    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    TRANSIENT_TIMEOUT = "transient_timeout"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_REJECTED = "approval_rejected"
    STALE_APPROVAL = "stale_approval"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"


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


class NativeTracingProfile(BaseModel):
    """Held-constant native tracing and local Phoenix export settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    collector_endpoint: str = "http://127.0.0.1:6006/v1/traces"
    phoenix_base_url: str = "http://127.0.0.1:6006"
    exporter_timeout_seconds: float = Field(default=1.0, gt=0, le=10)

    @model_validator(mode="after")
    def validate_loopback_endpoints(self) -> NativeTracingProfile:
        collector = urlsplit(self.collector_endpoint)
        phoenix = urlsplit(self.phoenix_base_url)
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}
        for label, parsed in (("collector", collector), ("Phoenix", phoenix)):
            if parsed.scheme != "http" or parsed.hostname not in allowed_hosts:
                raise ValueError(f"{label} endpoint must be an HTTP loopback URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"{label} endpoint cannot contain credentials, query, or fragment")
        if collector.path.rstrip("/") != "/v1/traces":
            raise ValueError("collector endpoint path must be /v1/traces")
        if phoenix.path.rstrip("/"):
            raise ValueError("Phoenix base URL cannot contain a path")
        return self


class InferenceRuntimeProfile(BaseModel):
    """How measured-run preflight identifies the model-serving compute host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]+$")
    compute_backend: Literal["auto", "nvidia", "vulkan"] = "auto"
    process_name: str = Field(default="llama-server", pattern=r"^[A-Za-z0-9._-]+$")
    command_timeout_seconds: float = Field(default=10.0, gt=0, le=60)


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
    tracing: NativeTracingProfile = Field(default_factory=NativeTracingProfile)
    inference_runtime: InferenceRuntimeProfile = Field(default_factory=InferenceRuntimeProfile)
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
    scenario_generator_version: str
    scenario_seed: int
    repetition: int = Field(ge=0)
    model: ModelProfile
    budget: Budget
    source_commit: GitCommit
    source_tree_sha256: Sha256Hex
    framework_versions: dict[str, str] = Field(min_length=1)
    dependency_lock_sha256: Sha256Hex
    prompt_bundle_sha256: Sha256Hex
    tool_catalog_sha256: Sha256Hex
    effective_request_policy_sha256: Sha256Hex
    execution_provenance_sha256: Sha256Hex
    world_manifest_sha256: Sha256Hex
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


class EvidenceLedger(BaseModel):
    """Auditable evidence exchanged between specialists and the commander."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    case_id: str
    items: list[EvidenceItem] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    """A proposed simulator action that has not changed world state."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    request_id: str
    case_id: str
    proposed_action_id: MitigationActionId
    proposed_action: str
    rationale: str
    evidence_ids: list[str] = Field(min_length=1)
    requested_by: str
    created_tick: int = Field(ge=0)
    status: Literal[ApprovalStatus.PENDING] = ApprovalStatus.PENDING


class ApprovalRecord(BaseModel):
    """Audited human or scripted decision for one mitigation request."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    request_id: str
    case_id: str
    decision: ApprovalDecision
    decided_by: str
    decided_tick: int = Field(ge=0)
    action_id_to_execute: MitigationActionId | None = None
    action_to_execute: str | None = None
    approval_token: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_decision_payload(self) -> ApprovalRecord:
        if self.decision in {ApprovalDecision.APPROVE, ApprovalDecision.EDIT}:
            if (
                not self.action_id_to_execute
                or not self.action_to_execute
                or not self.approval_token
            ):
                raise ValueError(
                    "approved or edited decisions require an action ID, description, "
                    "and approval token"
                )
        elif (
            self.action_id_to_execute is not None
            or self.action_to_execute is not None
            or self.approval_token is not None
        ):
            raise ValueError("rejected decisions cannot authorize an action")
        return self


class ToolError(BaseModel):
    """Stable error payload returned by the simulator without hidden retries."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    code: ToolErrorCode
    message: str
    retryable: bool
    world_tick: int = Field(ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class ToolAuditEvent(BaseModel):
    """One append-only simulator call/control record used by the evaluator."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    sequence: int = Field(ge=0)
    world_tick: int = Field(ge=0)
    operation: str
    arguments: dict[str, Any]
    outcome: Literal["succeeded", "failed", "decided"]
    evidence_id: str | None = None
    result_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    result_size_bytes: int | None = Field(default=None, ge=0)
    error: ToolError | None = None
    state_changed: bool = False


class IncidentReport(BaseModel):
    """Structured terminal artifact shared by every framework lane."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    case_id: str
    root_cause: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceItem] = Field(min_length=2)
    mitigation_action_id: MitigationActionId
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
    execution_started: bool = True
    event_stream_complete: bool = True


class WorldEvaluationState(BaseModel):
    """Minimal hidden terminal state passed from the simulator to the scorer."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    case_id: str
    applied_request_id: str | None = None
    applied_action_id: MitigationActionId | None = None
    applied_action: str | None = None
    mitigation_effective: bool
    recovery_verified: bool
    safety_violation: bool
