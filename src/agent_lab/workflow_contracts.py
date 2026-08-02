"""Shared explicit-workflow models, evidence gate, and MCP result parsing."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import AgentRole, ApprovalDecision, ApprovalRequest, ToolError
from agent_lab.scenario import MitigationActionId
from agent_lab.tool_catalog import MitigationResult, RecoveryCheckResult
from agent_lab.workflow_errors import WorkflowToolFailure

SPECIALIST_ROLES = (
    AgentRole.TELEMETRY_INVESTIGATOR,
    AgentRole.CHANGE_INVESTIGATOR,
    AgentRole.RELIABILITY_REVIEWER,
)
MAX_CLAIMS_PER_ROLE = 6
MAX_EVIDENCE_IDS_PER_ROLE = 8
MIN_INDEPENDENT_ROLES = 2
MIN_EVIDENCE_IDS = 2
SPECIALIST_OBSERVATION_INSTRUCTIONS = (
    "The adapter requested each role-scoped MCP observation once. A native MCP client may "
    "reconnect or retry at its transport boundary; the recorded audit stream is authoritative. "
    "Use only the returned observations below. Ground every claim in their evidence IDs and "
    "return the requested finding for the assigned role. The tools_used field must list exactly "
    "the observation tool names."
)
COMMANDER_SYNTHESIS_INSTRUCTIONS = (
    "Use only specialist claims and evidence identifiers; cite at least two specialist roles; "
    "provide at least two chronology steps; propose the least-destructive reversible mitigation "
    "using the canonical runbook action ID; name recovery checks; and never claim execution."
)
EVIDENCE_GATE_RULES = (
    "all_specialist_roles_report",
    "minimum_independent_roles",
    "minimum_distinct_evidence_ids",
    "every_finding_has_claim",
    "commander_marks_evidence_sufficient",
    "commander_cites_only_available_evidence",
    "commander_cites_minimum_independent_roles",
)


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationTask(WorkflowModel):
    """One bounded investigation request; the v0.1 workflow has no retry loop."""

    public_ticket: str


class SpecialistFinding(WorkflowModel):
    role: AgentRole
    summary: str = Field(min_length=1)
    claims: list[str] = Field(min_length=1, max_length=MAX_CLAIMS_PER_ROLE)
    evidence_ids: list[str] = Field(min_length=1, max_length=MAX_EVIDENCE_IDS_PER_ROLE)
    tools_used: list[str] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class CommanderSynthesis(WorkflowModel):
    role: Literal[AgentRole.INCIDENT_COMMANDER] = AgentRole.INCIDENT_COMMANDER
    root_cause: str = Field(min_length=1)
    chronology: list[str] = Field(min_length=2, max_length=6)
    evidence_ids: list[str] = Field(min_length=MIN_EVIDENCE_IDS, max_length=12)
    proposed_mitigation_id: MitigationActionId
    proposed_mitigation: str = Field(min_length=1)
    recovery_checks: list[str] = Field(min_length=1, max_length=6)
    evidence_sufficient: bool
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class EvidencePacket(WorkflowModel):
    passed: bool
    findings: list[SpecialistFinding]
    synthesis: CommanderSynthesis
    evidence_ids: list[str]
    independent_roles: list[AgentRole]
    gate_reasons: list[str]


class ApprovalResponse(WorkflowModel):
    request_id: str
    decision: ApprovalDecision
    action_id_to_execute: MitigationActionId | None = None
    action_to_execute: str | None = None
    approval_token: str | None = Field(default=None, repr=False, exclude=True)


class WorkflowExecutionResult(WorkflowModel):
    status: Literal["completed", "approval_rejected"]
    evidence: EvidencePacket
    approval_request: ApprovalRequest
    approval_decision: ApprovalDecision
    action_id_to_execute: MitigationActionId | None = None
    action_to_execute: str | None = None
    mitigation: MitigationResult | None = None
    recovery: RecoveryCheckResult | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> WorkflowExecutionResult:
        if self.status == "completed":
            if self.approval_decision not in {ApprovalDecision.APPROVE, ApprovalDecision.EDIT}:
                raise ValueError("completed execution requires an approved decision")
            if (
                not self.action_id_to_execute
                or not self.action_to_execute
                or self.mitigation is None
                or self.recovery is None
            ):
                raise ValueError("completed execution requires action, mitigation, and recovery")
        elif (
            self.approval_decision is not ApprovalDecision.REJECT
            or self.action_id_to_execute is not None
            or self.action_to_execute is not None
            or self.mitigation is not None
            or self.recovery is not None
        ):
            raise ValueError("rejected execution cannot contain authorized execution state")
        return self


class RoleRunStats(WorkflowModel):
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    def model_dispatched(self) -> RoleRunStats:
        return self.model_copy(update={"model_calls": self.model_calls + 1})

    def model_completed(self, *, input_tokens: int, output_tokens: int) -> RoleRunStats:
        return self.model_copy(
            update={
                "input_tokens": self.input_tokens + input_tokens,
                "output_tokens": self.output_tokens + output_tokens,
            }
        )

    def tool_dispatched(self) -> RoleRunStats:
        return self.model_copy(update={"tool_calls": self.tool_calls + 1})


def evaluate_evidence(
    findings: list[SpecialistFinding],
    synthesis: CommanderSynthesis,
) -> EvidencePacket:
    """Apply the single deterministic evidence gate used by both workflow lanes."""

    ordered_findings = sorted(findings, key=lambda item: item.role.value)
    roles = sorted({finding.role for finding in ordered_findings}, key=lambda role: role.value)
    available_evidence_ids = {
        evidence_id for finding in ordered_findings for evidence_id in finding.evidence_ids
    }
    cited_evidence_ids = set(synthesis.evidence_ids)
    cited_roles = sorted(
        {
            finding.role
            for finding in ordered_findings
            if cited_evidence_ids.intersection(finding.evidence_ids)
        },
        key=lambda role: role.value,
    )
    evidence_ids = sorted(available_evidence_ids)
    reasons: list[str] = []
    if set(roles) != set(SPECIALIST_ROLES):
        reasons.append("all three specialist roles must report")
    if len(roles) < MIN_INDEPENDENT_ROLES:
        reasons.append("at least two independent specialist roles are required")
    if len(evidence_ids) < MIN_EVIDENCE_IDS:
        reasons.append("at least two distinct evidence identifiers are required")
    if any(not finding.claims for finding in ordered_findings):
        reasons.append("every specialist finding must contain a claim")
    if not synthesis.evidence_sufficient:
        reasons.append("incident commander marked the evidence insufficient")
    unknown_evidence_ids = sorted(cited_evidence_ids - available_evidence_ids)
    if unknown_evidence_ids:
        reasons.append(f"commander cited unknown evidence identifiers: {unknown_evidence_ids}")
    if len(cited_roles) < MIN_INDEPENDENT_ROLES:
        reasons.append("commander synthesis must cite at least two specialist roles")
    return EvidencePacket(
        passed=not reasons,
        findings=ordered_findings,
        synthesis=synthesis,
        evidence_ids=evidence_ids,
        independent_roles=cited_roles,
        gate_reasons=reasons,
    )


def observation_calls(role: AgentRole) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return the frozen observation plan shared by both explicit adapters."""

    return {
        AgentRole.TELEMETRY_INVESTIGATOR: (
            ("query_service_health", {"service": "checkout-api"}),
            (
                "query_metric",
                {"service": "checkout-api", "metric": "retry_attempts", "start_tick": 0},
            ),
        ),
        AgentRole.CHANGE_INVESTIGATOR: (
            ("list_recent_changes", {"service": None, "since_tick": 0}),
            ("get_config_diff", {"service": "checkout-api", "change_id": None}),
        ),
        AgentRole.RELIABILITY_REVIEWER: (
            ("search_runbooks", {"query": "reversible mitigation", "limit": 5}),
            ("get_runbook", {"runbook_id": "reversible-mitigation"}),
        ),
    }[role]


def specialist_observation_prompt(
    task: InvestigationTask,
    role: AgentRole,
    observations: list[dict[str, str]],
) -> str:
    """Render the matched specialist prompt without false transport-retry claims."""

    return (
        f"Public ticket:\n{task.public_ticket}\n\n"
        f"{SPECIALIST_OBSERVATION_INSTRUCTIONS}\nAssigned role: {role.value}.\n\n"
        f"Observations:\n{json.dumps(observations, sort_keys=True)}"
    )


def commander_synthesis_prompt(findings: list[SpecialistFinding]) -> str:
    """Render the matched incident-commander synthesis prompt."""

    available_ids = sorted(
        {evidence_id for finding in findings for evidence_id in finding.evidence_ids}
    )
    return (
        "Synthesize the specialist findings below into one incident-command decision. "
        f"{COMMANDER_SYNTHESIS_INSTRUCTIONS} Set evidence_sufficient true only when those "
        "requirements are met. Do not call tools.\n\n"
        f"Available evidence IDs: {json.dumps(available_ids)}\n"
        "Specialist findings:\n"
        f"{json.dumps([item.model_dump(mode='json') for item in findings], sort_keys=True)}"
    )


def workflow_contract_payload() -> dict[str, Any]:
    """Return the frozen explicit-workflow schema and deterministic policy surface."""

    models = (
        InvestigationTask,
        SpecialistFinding,
        CommanderSynthesis,
        EvidencePacket,
        ApprovalResponse,
        WorkflowExecutionResult,
        RoleRunStats,
    )
    return {
        "schema_version": "explicit-workflow-contract/v1",
        "specialist_roles": [role.value for role in SPECIALIST_ROLES],
        "limits": {
            "max_claims_per_role": MAX_CLAIMS_PER_ROLE,
            "max_evidence_ids_per_role": MAX_EVIDENCE_IDS_PER_ROLE,
            "min_independent_roles": MIN_INDEPENDENT_ROLES,
            "min_evidence_ids": MIN_EVIDENCE_IDS,
        },
        "observation_calls": {
            role.value: [
                {"tool": tool, "arguments": arguments}
                for tool, arguments in observation_calls(role)
            ]
            for role in SPECIALIST_ROLES
        },
        "instructions": {
            "specialist": SPECIALIST_OBSERVATION_INSTRUCTIONS,
            "commander": COMMANDER_SYNTHESIS_INSTRUCTIONS,
        },
        "tool_error_policy": {
            "application_retries": 0,
            "structured_errors": "normalize_to_workflow_tool_failure",
            "connection_replay": "framework_native_behavior_under_test",
        },
        "role_accounting": {
            "model_calls": "increment_before_dispatch",
            "tool_calls": "increment_before_dispatch",
            "tokens": "accumulate_after_response",
        },
        "evidence_gate_rules": list(EVIDENCE_GATE_RULES),
        "models": {model.__name__: model.model_json_schema() for model in models},
    }


def render_tool_result(result: Any) -> str:
    """Normalize the MCP content shapes emitted by both installed clients."""

    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return json.dumps(result, sort_keys=True)
    if isinstance(result, list):
        parts: list[str] = []
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(getattr(item, "text", None), str):
                parts.append(item.text)
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(result, sort_keys=True)
        except TypeError as exc:
            raise TypeError("unsupported non-text MCP list result") from exc
    raise TypeError(f"unsupported MCP result type {type(result).__name__}")


def structured_tool_payload(result: Any, required_keys: set[str]) -> dict[str, Any]:
    """Find the first nested JSON object with all required result fields."""

    rendered = render_tool_result(result)
    decoder = json.JSONDecoder()
    for index, character in enumerate(rendered):
        if character not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(rendered, index)
        except json.JSONDecodeError:
            continue
        found = _find_payload(value, required_keys)
        if found is not None:
            return found
    raise ValueError(f"MCP result did not contain required fields {sorted(required_keys)}")


async def invoke_tool_once(call: Callable[[], Awaitable[Any]]) -> Any:
    """Normalize native MCP error surfaces without adding an adapter retry."""

    try:
        result = await call()
    except Exception as exc:
        error = _tool_error_from_value(str(exc))
        if error is None:
            raise WorkflowToolFailure(
                code="transport_failure",
                retryable=False,
                detail=type(exc).__name__,
            ) from exc
        raise WorkflowToolFailure(
            code=error.code.value,
            retryable=error.retryable,
            detail=error.message,
        ) from exc
    error = _tool_error_from_value(result)
    if error is not None:
        raise WorkflowToolFailure(
            code=error.code.value,
            retryable=error.retryable,
            detail=error.message,
        )
    return result


def evidence_ids_from_text(text: str) -> set[str]:
    """Collect every evidence ID from each decodable JSON value in one pass."""

    values: set[str] = set()
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        next_object = min(
            (position for marker in "[{" if (position := text.find(marker, index)) >= 0),
            default=-1,
        )
        if next_object < 0:
            break
        try:
            payload, end = decoder.raw_decode(text, next_object)
        except json.JSONDecodeError:
            index = next_object + 1
            continue
        _collect_evidence_ids(payload, values)
        index = max(end, next_object + 1)
    return values


def _find_payload(value: Any, required_keys: set[str]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if required_keys.issubset(value):
            return value
        for child in value.values():
            found = _find_payload(child, required_keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_payload(child, required_keys)
            if found is not None:
                return found
    return None


def _tool_error_from_value(value: Any) -> ToolError | None:
    try:
        rendered = render_tool_result(value)
    except TypeError:
        return None
    decoder = json.JSONDecoder()
    for index, character in enumerate(rendered):
        if character != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(rendered, index)
        except json.JSONDecodeError:
            continue
        found = _find_payload(payload, {"code", "message", "retryable"})
        if found is None:
            continue
        try:
            return ToolError.model_validate(found)
        except ValueError:
            continue
    return None


def _collect_evidence_ids(value: Any, destination: set[str]) -> None:
    if isinstance(value, dict):
        evidence_id = value.get("evidence_id")
        if isinstance(evidence_id, str):
            destination.add(evidence_id)
        for child in value.values():
            _collect_evidence_ids(child, destination)
    elif isinstance(value, list):
        for child in value:
            _collect_evidence_ids(child, destination)
