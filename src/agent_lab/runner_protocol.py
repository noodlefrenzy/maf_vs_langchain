"""Versioned JSON-lines protocol shared by control plane and runner processes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from agent_lab.contracts import (
    AgentRole,
    ApprovalDecision,
    FrameworkLane,
    HarnessAblationProfile,
    NativeTracingProfile,
    NormalizedEvent,
    RunManifest,
    TerminalStatus,
)
from agent_lab.scenario import MitigationActionId

PROTOCOL_VERSION = "0.1.0"


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["0.1.0"] = PROTOCOL_VERSION


class RunPaths(ProtocolModel):
    """Absolute, isolated paths allocated for one runner."""

    run_root: Path
    bundle: Path
    workspace: Path
    checkpoints: Path
    state: Path
    traces: Path

    @field_validator(
        "run_root",
        "bundle",
        "workspace",
        "checkpoints",
        "state",
        "traces",
    )
    @classmethod
    def require_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("runner paths must be absolute")
        return value

    @model_validator(mode="after")
    def require_isolation(self) -> RunPaths:
        for name in ("bundle", "workspace", "checkpoints", "state", "traces"):
            path = getattr(self, name)
            if path != self.run_root and self.run_root not in path.parents:
                raise ValueError(f"{name} must be contained by run_root")
        return self


class RoleEndpoint(ProtocolModel):
    """One role-scoped MCP endpoint; the token must never be logged."""

    role: AgentRole
    url: str = Field(pattern=r"^http://(?:127\.0\.0\.1|localhost):\d+/.+")
    token: str = Field(min_length=1, repr=False)
    expires_at: int = Field(ge=0)


class RunRequest(ProtocolModel):
    """Framework-neutral request passed to exactly one runner process."""

    manifest: RunManifest
    paths: RunPaths
    role_endpoints: dict[AgentRole, RoleEndpoint]
    public_ticket: str
    order_seed: int
    tracing: NativeTracingProfile = Field(default_factory=NativeTracingProfile)
    harness_ablation: HarnessAblationProfile = Field(default_factory=HarnessAblationProfile)
    capture_sensitive: bool = False
    capture_reasoning: Literal[False] = False

    @model_validator(mode="after")
    def validate_request_identity(self) -> RunRequest:
        expected_root = self.paths.run_root.name
        if expected_root != str(self.manifest.run_id):
            raise ValueError("run_root name must equal the manifest run_id")
        for role, endpoint in self.role_endpoints.items():
            if endpoint.role != role:
                raise ValueError("role endpoint key and role claim differ")
        harness_lanes = {FrameworkLane.MAF_HARNESS, FrameworkLane.DEEP_AGENTS}
        if (
            self.harness_ablation != HarnessAblationProfile()
            and self.manifest.lane not in harness_lanes
        ):
            raise ValueError("non-default harness ablation requires a harness lane")
        return self


class CheckpointCapability(ProtocolModel):
    resumable: bool
    restart_safe: bool
    format: str


class PreparedRun(ProtocolModel):
    run_id: UUID
    lane: FrameworkLane
    topology_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_request_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_provenance_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    framework_versions: dict[str, str] = Field(min_length=1)
    checkpoint: CheckpointCapability


class RunnerUsage(ProtocolModel):
    """Cumulative counters; calls are reserved pre-dispatch, tokens post-response."""

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    approval_requests: int = Field(default=0, ge=0)

    def is_monotonic_from(self, previous: RunnerUsage) -> bool:
        return all(
            getattr(self, field_name) >= getattr(previous, field_name)
            for field_name in (
                "model_calls",
                "tool_calls",
                "output_tokens",
                "approval_requests",
            )
        )


class PrepareCommand(ProtocolModel):
    kind: Literal["prepare"] = "prepare"
    request: RunRequest


class StartCommand(ProtocolModel):
    kind: Literal["start"] = "start"
    run_id: UUID


class ApprovalCommand(ProtocolModel):
    kind: Literal["approval"] = "approval"
    run_id: UUID
    request_id: str
    decision: ApprovalDecision
    decided_by: str = Field(min_length=1)
    decided_tick: int = Field(ge=0)
    action_id_to_execute: MitigationActionId | None = None
    action_to_execute: str | None = None
    approval_token: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_decision(self) -> ApprovalCommand:
        if self.decision in {ApprovalDecision.APPROVE, ApprovalDecision.EDIT}:
            if (
                not self.action_id_to_execute
                or not self.action_to_execute
                or not self.approval_token
            ):
                raise ValueError(
                    "approved or edited commands require an action ID, description, and token"
                )
        elif (
            self.action_id_to_execute is not None
            or self.action_to_execute is not None
            or self.approval_token is not None
        ):
            raise ValueError("rejected commands cannot authorize an action")
        return self


class FinalizedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(pattern=r"^artifacts/[A-Za-z0-9][A-Za-z0-9._-]*\.json$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class FinalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[FinalizedArtifact] = Field(default_factory=list)
    task_success: bool | None = None
    total_score: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_unique_artifacts(self) -> FinalizationResult:
        paths = [artifact.path for artifact in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("finalized artifact paths must be unique")
        return self


class FinalizeCommand(ProtocolModel):
    kind: Literal["finalize"] = "finalize"
    run_id: UUID
    finalization: FinalizationResult


class CancelCommand(ProtocolModel):
    kind: Literal["cancel"] = "cancel"
    run_id: UUID
    status: TerminalStatus = TerminalStatus.CANCELLED
    reason: str

    @model_validator(mode="after")
    def require_failure_status(self) -> CancelCommand:
        if self.status in {TerminalStatus.COMPLETED, TerminalStatus.APPROVAL_REJECTED}:
            raise ValueError("cancel commands require a non-success terminal status")
        return self


class ShutdownCommand(ProtocolModel):
    kind: Literal["shutdown"] = "shutdown"
    run_id: UUID


RunnerCommand = Annotated[
    PrepareCommand
    | StartCommand
    | ApprovalCommand
    | FinalizeCommand
    | CancelCommand
    | ShutdownCommand,
    Field(discriminator="kind"),
]


class PreparedMessage(ProtocolModel):
    kind: Literal["prepared"] = "prepared"
    prepared: PreparedRun


class UsageMessage(ProtocolModel):
    kind: Literal["usage"] = "usage"
    run_id: UUID
    usage: RunnerUsage


class EventMessage(ProtocolModel):
    kind: Literal["event"] = "event"
    event: NormalizedEvent


class HeartbeatMessage(ProtocolModel):
    kind: Literal["heartbeat"] = "heartbeat"
    run_id: UUID
    phase: str


class TerminalMessage(ProtocolModel):
    kind: Literal["terminal"] = "terminal"
    run_id: UUID
    status: TerminalStatus
    reason: str
    usage: RunnerUsage


class ErrorMessage(ProtocolModel):
    kind: Literal["error"] = "error"
    run_id: UUID | None = None
    error: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


RunnerMessage = Annotated[
    PreparedMessage
    | UsageMessage
    | EventMessage
    | HeartbeatMessage
    | TerminalMessage
    | ErrorMessage,
    Field(discriminator="kind"),
]

_COMMAND_ADAPTER = TypeAdapter(RunnerCommand)
_MESSAGE_ADAPTER = TypeAdapter(RunnerMessage)


def encode_protocol_message(message: BaseModel) -> bytes:
    """Serialize one compact JSON object terminated by exactly one newline."""

    return (message.model_dump_json() + "\n").encode("utf-8")


def decode_runner_command(line: bytes | str) -> RunnerCommand:
    return _COMMAND_ADAPTER.validate_json(line)


def decode_runner_message(line: bytes | str) -> RunnerMessage:
    return _MESSAGE_ADAPTER.validate_json(line)


def redacted_request_payload(request: RunRequest) -> dict[str, Any]:
    """Return a log-safe request view that cannot contain bearer tokens."""

    payload = json.loads(request.model_dump_json())
    for endpoint in payload["role_endpoints"].values():
        endpoint["token"] = "[REDACTED]"
    return payload
