"""Shared completion contract and evidence gate for opinionated harness lanes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import AgentRole, ApprovalDecision, ApprovalRequest
from agent_lab.prompt_bundle import render_role_prompt
from agent_lab.scenario import MitigationActionId
from agent_lab.tool_catalog import MitigationResult, RecoveryCheckResult

SPECIALIST_ROLES = frozenset(
    {
        AgentRole.TELEMETRY_INVESTIGATOR,
        AgentRole.CHANGE_INVESTIGATOR,
        AgentRole.RELIABILITY_REVIEWER,
    }
)


class HarnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HarnessSpecialistFinding(HarnessModel):
    role: AgentRole
    summary: str = Field(min_length=1)
    claims: list[str] = Field(min_length=1, max_length=6)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    tools_used: list[str] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class HarnessCommanderSynthesis(HarnessModel):
    role: Literal[AgentRole.INCIDENT_COMMANDER] = AgentRole.INCIDENT_COMMANDER
    root_cause: str = Field(min_length=1)
    chronology: list[str] = Field(min_length=2, max_length=6)
    evidence_ids: list[str] = Field(min_length=2, max_length=12)
    proposed_mitigation_id: MitigationActionId
    proposed_mitigation: str = Field(min_length=1)
    recovery_checks: list[str] = Field(min_length=1, max_length=6)
    evidence_sufficient: bool
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class HarnessProposal(HarnessModel):
    findings: list[HarnessSpecialistFinding] = Field(min_length=2, max_length=3)
    synthesis: HarnessCommanderSynthesis

    @model_validator(mode="after")
    def validate_specialist_roles(self) -> HarnessProposal:
        roles = [finding.role for finding in self.findings]
        if any(role not in SPECIALIST_ROLES for role in roles):
            raise ValueError("harness findings must come from specialist roles")
        if len(roles) != len(set(roles)):
            raise ValueError("harness findings must use unique specialist roles")
        return self


class HarnessEvidencePacket(HarnessModel):
    passed: bool
    findings: list[HarnessSpecialistFinding]
    synthesis: HarnessCommanderSynthesis
    evidence_ids: list[str]
    independent_roles: list[AgentRole]
    gate_reasons: list[str]
    attempt: int = Field(default=0, ge=0, le=1)


class HarnessApprovalResponse(HarnessModel):
    request_id: str
    decision: ApprovalDecision
    action_id_to_execute: MitigationActionId | None = None
    action_to_execute: str | None = None
    approval_token: str | None = Field(default=None, repr=False, exclude=True)


class HarnessExecutionResult(HarnessModel):
    status: Literal["completed", "approval_rejected"]
    evidence: HarnessEvidencePacket
    approval_request: ApprovalRequest
    approval_decision: ApprovalDecision
    action_id_to_execute: MitigationActionId | None = None
    action_to_execute: str | None = None
    mitigation: MitigationResult | None = None
    recovery: RecoveryCheckResult | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> HarnessExecutionResult:
        if self.status == "completed":
            if self.approval_decision not in {
                ApprovalDecision.APPROVE,
                ApprovalDecision.EDIT,
            }:
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


def evaluate_harness_proposal(proposal: HarnessProposal) -> HarnessEvidencePacket:
    """Apply the framework-neutral evidence gate after native harness delegation."""

    available_ids = {
        evidence_id for finding in proposal.findings for evidence_id in finding.evidence_ids
    }
    cited_ids = set(proposal.synthesis.evidence_ids)
    cited_roles = sorted(
        {
            finding.role
            for finding in proposal.findings
            if cited_ids.intersection(finding.evidence_ids)
        },
        key=lambda role: role.value,
    )
    reasons: list[str] = []
    if len(proposal.findings) < 2:
        reasons.append("at least two specialist roles must report")
    if len(available_ids) < 2:
        reasons.append("at least two distinct evidence identifiers are required")
    unknown_ids = sorted(cited_ids - available_ids)
    if unknown_ids:
        reasons.append(f"commander cited unknown evidence identifiers: {unknown_ids}")
    if len(cited_roles) < 2:
        reasons.append("commander synthesis must cite at least two specialist roles")
    if not proposal.synthesis.evidence_sufficient:
        reasons.append("incident commander marked the evidence insufficient")
    return HarnessEvidencePacket(
        passed=not reasons,
        findings=proposal.findings,
        synthesis=proposal.synthesis,
        evidence_ids=sorted(available_ids),
        independent_roles=cited_roles,
        gate_reasons=reasons,
    )


def harness_task_prompt(public_ticket: str) -> str:
    """Render the byte-identical user-authored coordinator task for both harnesses."""

    return (
        f"Public ticket:\n{public_ticket}\n\n"
        "Investigate and respond as incident commander. Use the native harness planning and "
        "workspace facilities. You must delegate evidence collection to at least two of the "
        "role-scoped telemetry, change, and reliability specialists; do not perform their MCP "
        "work yourself and do not invent a finding for a specialist you did not call. Collect at "
        "least two independently sourced evidence identifiers, establish chronology, and choose "
        "the least-destructive reversible mitigation. Use the canonical action ID from the "
        "reversible-mitigation runbook as proposed_mitigation_id while retaining a readable "
        "description in proposed_mitigation. Return the HarnessProposal schema with "
        "each consulted specialist's actual finding and one commander synthesis. Do not claim "
        "that approval, mitigation, or recovery has occurred; the outer control plane owns those "
        "steps."
    )


def harness_specialist_prompt(role: AgentRole) -> str:
    """Render the matched natural-text completion contract for harness specialists."""

    return (
        f"{render_role_prompt(role)}\n\n"
        "Use your role-scoped MCP tools to gather sufficient evidence, then stop calling tools. "
        "Your final assistant message must be one JSON object with exactly these fields: role, "
        "summary, claims, evidence_ids, tools_used, and uncertainties. Set role to your role ID; "
        "cite only evidence IDs returned by your own MCP calls; list only MCP tools you actually "
        "called. Do not wrap the JSON in Markdown."
    )
