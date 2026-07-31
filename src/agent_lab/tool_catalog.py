"""Frozen MCP tool catalog shared by the service and every runner."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from agent_lab.contracts import (
    SCHEMA_VERSION,
    AgentRole,
    ApprovalRecord,
    ApprovalRequest,
    CapabilityGroup,
    ToolError,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyInput(StrictModel):
    pass


class IncidentBriefResult(StrictModel):
    case_id: str
    seed: int
    severity: str
    brief: str


class IncidentStatusResult(StrictModel):
    case_id: str
    world_tick: int = Field(ge=0)
    state: str
    mitigation_request_id: str | None = None
    recovery_verified: bool


class ServiceInput(StrictModel):
    service: str


class ServiceHealthResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    service: str
    error_rate: float = Field(ge=0, le=1)
    p95_ms: float = Field(ge=0)
    saturation: float = Field(ge=0, le=1)


class LogSearchInput(StrictModel):
    service: str
    query: str
    limit: int = Field(default=20, ge=1, le=100)


class LogSearchResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    service: str
    query: str
    matches: list[str]


class MetricQueryInput(StrictModel):
    service: str
    metric: str
    start_tick: int = Field(default=0, ge=0)
    end_tick: int | None = Field(default=None, ge=0)


class MetricPoint(StrictModel):
    tick: int = Field(ge=0)
    value: float


class MetricQueryResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    service: str
    metric: str
    unit: str
    points: list[MetricPoint]
    summary: str


class TraceLookupInput(StrictModel):
    trace_id: str


class TraceLookupResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    trace_id: str
    services: list[str]
    summary: str


class RecentChangesInput(StrictModel):
    service: str | None = None
    since_tick: int = Field(default=0, ge=0)


class ChangeSummary(StrictModel):
    service: str
    change_id: str
    tick: int = Field(ge=0)
    kind: str
    summary: str


class RecentChangesResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    changes: list[ChangeSummary]


class ChangeDetailInput(StrictModel):
    change_id: str


class ChangeDetailResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    change: ChangeSummary
    details: str


class ConfigDiffInput(StrictModel):
    service: str
    change_id: str | None = None


class DiffResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    subject: str
    diff: str


class FeatureFlagDiffInput(StrictModel):
    flag: str
    regions: list[str] = Field(min_length=2, max_length=2)


class SchemaDiffInput(StrictModel):
    subject: str


class RunbookSearchInput(StrictModel):
    query: str
    limit: int = Field(default=10, ge=1, le=25)


class RunbookSummary(StrictModel):
    runbook_id: str
    title: str
    summary: str


class RunbookSearchResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    matches: list[RunbookSummary]


class RunbookInput(StrictModel):
    runbook_id: str


class RunbookResult(StrictModel):
    evidence_id: str
    world_tick: int = Field(ge=0)
    runbook_id: str
    title: str
    guidance: str


class HypothesisInput(StrictModel):
    root_cause: str
    evidence_ids: list[str] = Field(min_length=2)
    mitigation: str


class HypothesisResult(StrictModel):
    case_id: str
    world_tick: int = Field(ge=0)
    status: str
    evidence_items: int = Field(ge=0)


class MitigationRequestInput(StrictModel):
    proposed_action: str
    rationale: str
    evidence_ids: list[str] = Field(min_length=2)


class ApplyMitigationInput(StrictModel):
    request_id: str
    approval_token: str


class MitigationResult(StrictModel):
    case_id: str
    request_id: str
    world_tick: int = Field(ge=0)
    action: str
    status: str
    effective: bool
    idempotent_replay: bool = False


class RecoveryCheckInput(StrictModel):
    services: list[str] = Field(min_length=1, max_length=10)


class RecoveryCheckResult(StrictModel):
    evidence_id: str
    case_id: str
    world_tick: int = Field(ge=0)
    recovered: bool
    checks: list[ServiceHealthResult]
    summary: str


class ToolResultEnvelope(StrictModel):
    """Transport envelope: exactly one of ``result`` and ``error`` is populated."""

    result: dict[str, Any] | list[Any] | None = None
    error: ToolError | None = None


class ToolDefinition(BaseModel):
    """Serializable definition used to configure and validate MCP clients."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    capability: CapabilityGroup
    allowed_roles: tuple[AgentRole, ...]
    description: str
    mutates_world: bool = False
    approval_required: bool = False
    input_model: type[BaseModel] = Field(exclude=True)
    output_model: type[BaseModel] = Field(exclude=True)

    def contract_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability.value,
            "allowed_roles": [role.value for role in self.allowed_roles],
            "description": self.description,
            "mutates_world": self.mutates_world,
            "approval_required": self.approval_required,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


_ALL_ROLES = tuple(AgentRole)
_COMMANDER = (AgentRole.INCIDENT_COMMANDER,)
_TELEMETRY = (AgentRole.TELEMETRY_INVESTIGATOR,)
_CHANGES = (AgentRole.CHANGE_INVESTIGATOR,)
_RUNBOOKS = (AgentRole.RELIABILITY_REVIEWER,)

TOOL_CATALOG: Final[tuple[ToolDefinition, ...]] = (
    ToolDefinition(
        name="get_incident_brief",
        capability=CapabilityGroup.INCIDENT,
        allowed_roles=_ALL_ROLES,
        description="Return the public incident ticket and case identifier.",
        input_model=EmptyInput,
        output_model=IncidentBriefResult,
    ),
    ToolDefinition(
        name="get_incident_status",
        capability=CapabilityGroup.INCIDENT,
        allowed_roles=_ALL_ROLES,
        description="Return public simulator status without hidden causal ground truth.",
        input_model=EmptyInput,
        output_model=IncidentStatusResult,
    ),
    ToolDefinition(
        name="query_service_health",
        capability=CapabilityGroup.TELEMETRY,
        allowed_roles=_TELEMETRY,
        description="Query current error rate, tail latency, and saturation for one service.",
        input_model=ServiceInput,
        output_model=ServiceHealthResult,
    ),
    ToolDefinition(
        name="search_logs",
        capability=CapabilityGroup.TELEMETRY,
        allowed_roles=_TELEMETRY,
        description="Search bounded logs for one service using a focused query.",
        input_model=LogSearchInput,
        output_model=LogSearchResult,
    ),
    ToolDefinition(
        name="query_metric",
        capability=CapabilityGroup.TELEMETRY,
        allowed_roles=_TELEMETRY,
        description="Query a bounded metric series over simulated world ticks.",
        input_model=MetricQueryInput,
        output_model=MetricQueryResult,
    ),
    ToolDefinition(
        name="lookup_trace",
        capability=CapabilityGroup.TELEMETRY,
        allowed_roles=_TELEMETRY,
        description="Look up a known simulated trace and its cross-service chronology.",
        input_model=TraceLookupInput,
        output_model=TraceLookupResult,
    ),
    ToolDefinition(
        name="list_recent_changes",
        capability=CapabilityGroup.CHANGES,
        allowed_roles=_CHANGES,
        description="List bounded deploy, schema, flag, and configuration changes.",
        input_model=RecentChangesInput,
        output_model=RecentChangesResult,
    ),
    ToolDefinition(
        name="get_change_detail",
        capability=CapabilityGroup.CHANGES,
        allowed_roles=_CHANGES,
        description="Return details for one change identifier.",
        input_model=ChangeDetailInput,
        output_model=ChangeDetailResult,
    ),
    ToolDefinition(
        name="get_config_diff",
        capability=CapabilityGroup.CHANGES,
        allowed_roles=_CHANGES,
        description="Return the runtime-relevant configuration diff for one service.",
        input_model=ConfigDiffInput,
        output_model=DiffResult,
    ),
    ToolDefinition(
        name="get_feature_flag_diff",
        capability=CapabilityGroup.CHANGES,
        allowed_roles=_CHANGES,
        description="Compare a feature flag value in exactly two regions.",
        input_model=FeatureFlagDiffInput,
        output_model=DiffResult,
    ),
    ToolDefinition(
        name="get_schema_diff",
        capability=CapabilityGroup.CHANGES,
        allowed_roles=_CHANGES,
        description="Compare current and previous schemas for one subject.",
        input_model=SchemaDiffInput,
        output_model=DiffResult,
    ),
    ToolDefinition(
        name="search_runbooks",
        capability=CapabilityGroup.RUNBOOKS,
        allowed_roles=_RUNBOOKS,
        description="Find incident policy and reversible mitigation runbooks.",
        input_model=RunbookSearchInput,
        output_model=RunbookSearchResult,
    ),
    ToolDefinition(
        name="get_runbook",
        capability=CapabilityGroup.RUNBOOKS,
        allowed_roles=_RUNBOOKS,
        description="Return one runbook's operational guidance.",
        input_model=RunbookInput,
        output_model=RunbookResult,
    ),
    ToolDefinition(
        name="submit_hypothesis",
        capability=CapabilityGroup.COMMAND,
        allowed_roles=_COMMANDER,
        description="Record a diagnosis and evidence set without exposing evaluator scoring.",
        input_model=HypothesisInput,
        output_model=HypothesisResult,
    ),
    ToolDefinition(
        name="request_mitigation",
        capability=CapabilityGroup.COMMAND,
        allowed_roles=_COMMANDER,
        description="Create an approval request without changing simulator state.",
        input_model=MitigationRequestInput,
        output_model=ApprovalRequest,
    ),
    ToolDefinition(
        name="apply_mitigation",
        capability=CapabilityGroup.COMMAND,
        allowed_roles=_COMMANDER,
        description="Apply the exact action authorized by a valid approval token.",
        mutates_world=True,
        approval_required=True,
        input_model=ApplyMitigationInput,
        output_model=MitigationResult,
    ),
    ToolDefinition(
        name="verify_recovery",
        capability=CapabilityGroup.COMMAND,
        allowed_roles=_COMMANDER,
        description="Run post-mitigation health checks and report whether recovery is verified.",
        input_model=RecoveryCheckInput,
        output_model=RecoveryCheckResult,
    ),
)

_TOOL_INDEX: Final = {tool.name: tool for tool in TOOL_CATALOG}


def get_tool_definition(name: str) -> ToolDefinition:
    return _TOOL_INDEX[name]


def tool_definitions_for_role(role: AgentRole) -> tuple[ToolDefinition, ...]:
    return tuple(tool for tool in TOOL_CATALOG if role in tool.allowed_roles)


def tool_catalog_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tools": [tool.contract_payload() for tool in TOOL_CATALOG],
    }


def artifact_schema_payload() -> dict[str, object]:
    """Return command artifacts that all lanes must read and write identically."""

    from agent_lab.contracts import (
        EvidenceLedger,
        IncidentReport,
        NormalizedEvent,
        RunManifest,
        RunTerminal,
        ToolAuditEvent,
        WorldEvaluationState,
    )
    from agent_lab.scenario import IncidentCase

    return {
        "schema_version": SCHEMA_VERSION,
        "run_manifest": RunManifest.model_json_schema(),
        "normalized_event": NormalizedEvent.model_json_schema(),
        "incident_report": IncidentReport.model_json_schema(),
        "evidence_ledger": EvidenceLedger.model_json_schema(),
        "approval_request": ApprovalRequest.model_json_schema(),
        "approval_record": ApprovalRecord.model_json_schema(),
        "tool_audit_event": ToolAuditEvent.model_json_schema(),
        "world_evaluation_state": WorldEvaluationState.model_json_schema(),
        "incident_world_manifest": IncidentCase.model_json_schema(),
        "terminal": RunTerminal.model_json_schema(),
        "tool_result_envelope": ToolResultEnvelope.model_json_schema(),
    }
