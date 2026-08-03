"""Shared completion contract and evidence gate for opinionated harness lanes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import (
    AgentRole,
    ApprovalDecision,
    ApprovalRequest,
    HarnessAblationProfile,
)
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

MIN_HARNESS_SPECIALIST_ROLES = 2
HARNESS_DELEGATION_REPAIR_TURNS = 2
HARNESS_PROVENANCE_CORRECTION_TURNS = 1
HARNESS_PHASE_MODEL_CALL_LIMIT = 6
HARNESS_DELEGATION_RECURSION_LIMIT = 64
HARNESS_SYNTHESIS_RECURSION_LIMIT = 16


@dataclass(frozen=True)
class HarnessRoleObservation:
    """Framework-neutral native tool evidence observed for one specialist."""

    called_tools: frozenset[str]
    evidence_ids: frozenset[str]


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
    attempt: int = Field(default=0, ge=0, le=2)


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


def active_harness_roles(
    observations: Mapping[AgentRole, HarnessRoleObservation],
) -> set[AgentRole]:
    """Return specialist roles proven active by at least one native MCP call."""

    return {role for role, observation in observations.items() if observation.called_tools}


def require_harness_delegation(
    observations: Mapping[AgentRole, HarnessRoleObservation],
) -> None:
    """Fail closed unless native tool activity proves two-role delegation."""

    if len(active_harness_roles(observations)) < MIN_HARNESS_SPECIALIST_ROLES:
        raise ValueError("harness delegation did not satisfy the two-specialist MCP gate")


def validate_harness_role_provenance(
    proposal: HarnessProposal,
    observations: Mapping[AgentRole, HarnessRoleObservation],
) -> None:
    """Reject findings whose claimed tools or evidence lack native-call provenance."""

    for finding in proposal.findings:
        observation = observations.get(finding.role)
        if observation is None or not observation.called_tools:
            raise ValueError(f"{finding.role.value} made no native MCP calls")
        unknown_tools = set(finding.tools_used) - observation.called_tools
        if unknown_tools:
            raise ValueError(
                f"{finding.role.value} cited tools outside its native MCP calls: "
                f"unknown={sorted(unknown_tools)}, "
                f"observed={sorted(observation.called_tools)}"
            )
        if not set(finding.evidence_ids).issubset(observation.evidence_ids):
            raise ValueError(
                f"{finding.role.value} cited evidence outside its native MCP results: "
                f"cited={finding.evidence_ids}, observed={sorted(observation.evidence_ids)}"
            )


def harness_evidence_allowlist(
    observations: Mapping[AgentRole, HarnessRoleObservation],
) -> str:
    """Render the same deterministic role-scoped evidence allowlist for both lanes."""

    return "\n".join(
        f"- {role.value}: {sorted(observation.evidence_ids)}"
        for role, observation in sorted(observations.items(), key=lambda item: item[0].value)
        if observation.called_tools
    )


def harness_delegation_repair_prompt(missing_roles: list[str]) -> str:
    """Return the matched repair instruction for an under-delegated coordinator."""

    return (
        "The deterministic evidence gate is not yet satisfied: fewer than two specialist "
        "roles have made real MCP calls. Delegate now to at least one of these remaining "
        f"specialists and retrieve its completed result: {missing_roles}. Do not invent the "
        "result yourself."
    )


def harness_synthesis_prompt(evidence_allowlist: str) -> str:
    """Return the matched structured-synthesis instruction for both harness lanes."""

    return (
        "Using only the completed specialist task results already in this conversation, return "
        "the HarnessProposal now. Include no finding that was not returned by a specialist. "
        "Opaque evidence IDs must be copied byte-for-byte; do not replace them with dates, "
        "descriptions, or invented aliases. The control plane observed this role-specific "
        f"evidence-ID allowlist:\n{evidence_allowlist}\nEvery finding must cite one or more "
        "exact IDs from its own role's list, and the commander synthesis must cite exact IDs "
        "from at least two included roles."
    )


def harness_provenance_correction_prompt(error: ValueError, evidence_allowlist: str) -> str:
    """Return the sole matched repair instruction after provenance validation fails."""

    return (
        "The previous structured proposal failed the deterministic provenance validator: "
        f"{error}. Return one corrected HarnessProposal. Copy only exact evidence IDs from "
        "this role-specific allowlist and keep findings scoped to their roles:\n"
        f"{evidence_allowlist}"
    )


def evaluate_harness_proposal(
    proposal: HarnessProposal,
    *,
    attempt: int = 0,
) -> HarnessEvidencePacket:
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
        attempt=attempt,
    )


def harness_task_prompt(
    public_ticket: str,
    prompt_variant: Literal["canonical", "paraphrase_a"] = "canonical",
    reviewer_enabled: bool = True,
) -> str:
    """Render the byte-identical user-authored coordinator task for both harnesses."""

    if prompt_variant == "paraphrase_a":
        delegation = (
            "at least two role-scoped specialists chosen from telemetry, change, and reliability"
            if reviewer_enabled
            else "both role-scoped telemetry and change specialists"
        )
        return (
            f"Incident ticket:\n{public_ticket}\n\n"
            "Act as the incident commander and investigate with the harness's native planning "
            f"and workspace features. Evidence gathering must be delegated to {delegation}; "
            "never do "
            "their MCP work yourself or fabricate a specialist response. Establish event order "
            "from at least two independent evidence IDs, then select the least-destructive "
            "reversible mitigation. Copy its canonical runbook action ID into "
            "proposed_mitigation_id and put the readable action in proposed_mitigation. Return "
            "one HarnessProposal containing the real findings from every consulted specialist "
            "and one commander synthesis. Approval, mitigation, and recovery belong to the "
            "outer control plane and must not be claimed here."
        )
    delegation = (
        "at least two of the role-scoped telemetry, change, and reliability specialists"
        if reviewer_enabled
        else "both role-scoped telemetry and change specialists"
    )
    return (
        f"Public ticket:\n{public_ticket}\n\n"
        "Investigate and respond as incident commander. Use the native harness planning and "
        f"workspace facilities. You must delegate evidence collection to {delegation}; do not "
        "perform their MCP "
        "work yourself and do not invent a finding for a specialist you did not call. Collect at "
        "least two independently sourced evidence identifiers, establish chronology, and choose "
        "the least-destructive reversible mitigation. Use the canonical action ID from the "
        "reversible-mitigation runbook as proposed_mitigation_id while retaining a readable "
        "description in proposed_mitigation. Return the HarnessProposal schema with "
        "each consulted specialist's actual finding and one commander synthesis. Do not claim "
        "that approval, mitigation, or recovery has occurred; the outer control plane owns those "
        "steps."
    )


def harness_specialist_roles(profile: HarnessAblationProfile) -> frozenset[AgentRole]:
    """Return the exact specialist set exposed by a matched harness ablation."""

    if profile.reviewer_enabled:
        return SPECIALIST_ROLES
    return SPECIALIST_ROLES - {AgentRole.RELIABILITY_REVIEWER}


def harness_specialist_prompt(
    role: AgentRole,
    prompt_variant: Literal["canonical", "paraphrase_a"] = "canonical",
) -> str:
    """Render the matched natural-text completion contract for harness specialists."""

    if prompt_variant == "paraphrase_a":
        return (
            f"{render_role_prompt(role)}\n\n"
            "Gather enough evidence with only your role-scoped MCP tools, then make no more "
            "tool calls. Finish with a bare JSON object—no Markdown—whose fields are exactly "
            "role, summary, claims, evidence_ids, tools_used, and uncertainties. Use your role "
            "ID for role, copy only evidence IDs observed in your own tool results, and name "
            "only MCP tools you actually invoked."
        )
    return (
        f"{render_role_prompt(role)}\n\n"
        "Use your role-scoped MCP tools to gather sufficient evidence, then stop calling tools. "
        "Your final assistant message must be one JSON object with exactly these fields: role, "
        "summary, claims, evidence_ids, tools_used, and uncertainties. Set role to your role ID; "
        "cite only evidence IDs returned by your own MCP calls; list only MCP tools you actually "
        "called. Do not wrap the JSON in Markdown."
    )


def harness_ablation_payload(profile: HarnessAblationProfile) -> dict[str, object]:
    """Return the exact matched harness prompts and bounded correction policy."""

    active_roles = harness_specialist_roles(profile)
    return {
        "schema_version": "harness-ablation/v2",
        "prompt_variant": profile.prompt_variant,
        "provenance_correction_turns": profile.provenance_correction_turns,
        "reviewer_enabled": profile.reviewer_enabled,
        "context_compaction": profile.context_compaction,
        "active_specialist_roles": sorted(role.value for role in active_roles),
        "prompts": {
            "task_template": harness_task_prompt(
                "{public_ticket}",
                profile.prompt_variant,
                profile.reviewer_enabled,
            ),
            "specialists": {
                role.value: harness_specialist_prompt(role, profile.prompt_variant)
                for role in sorted(active_roles, key=lambda item: item.value)
            },
            "delegation_repair_template": harness_delegation_repair_prompt(
                ["{remaining_specialist_roles}"]
            ),
            "synthesis_template": harness_synthesis_prompt("{role_evidence_allowlist}"),
            "provenance_correction_template": harness_provenance_correction_prompt(
                ValueError("{validator_error}"),
                "{role_evidence_allowlist}",
            ),
        },
    }


def harness_contract_payload() -> dict[str, object]:
    """Return the frozen framework-neutral opinionated-harness contract."""

    return {
        "schema_version": "0.1.0",
        "specialist_roles": sorted(role.value for role in SPECIALIST_ROLES),
        "delegation": {
            "minimum_active_specialist_roles": MIN_HARNESS_SPECIALIST_ROLES,
            "repair_turns": HARNESS_DELEGATION_REPAIR_TURNS,
            "activity_proof": "at_least_one_native_mcp_call",
            "failure_mode": "error",
        },
        "provenance": {
            "correction_turns": HARNESS_PROVENANCE_CORRECTION_TURNS,
            "supported_correction_turns": [0, 1, 2],
            "claimed_tools_must_be_observed": True,
            "claimed_evidence_ids_must_be_observed_for_same_role": True,
            "failure_mode": "error",
        },
        "phase_limits": {
            "model_calls": HARNESS_PHASE_MODEL_CALL_LIMIT,
            "delegation_recursion": HARNESS_DELEGATION_RECURSION_LIMIT,
            "synthesis_recursion": HARNESS_SYNTHESIS_RECURSION_LIMIT,
        },
        "prompts": {
            "task_template": harness_task_prompt("{public_ticket}"),
            "specialists": {
                role.value: harness_specialist_prompt(role)
                for role in sorted(SPECIALIST_ROLES, key=lambda item: item.value)
            },
            "delegation_repair_template": harness_delegation_repair_prompt(
                ["{remaining_specialist_roles}"]
            ),
            "synthesis_template": harness_synthesis_prompt("{role_evidence_allowlist}"),
            "provenance_correction_template": harness_provenance_correction_prompt(
                ValueError("{validator_error}"),
                "{role_evidence_allowlist}",
            ),
        },
        "ablation_space": {
            "prompt_variants": ["canonical", "paraphrase_a"],
            "reviewer_enabled": [True, False],
            "context_compaction": ["native", "disabled"],
            "effective_payload_schema": "harness-ablation/v2",
        },
        "schemas": {
            "proposal": HarnessProposal.model_json_schema(),
            "evidence_packet": HarnessEvidencePacket.model_json_schema(),
        },
    }
