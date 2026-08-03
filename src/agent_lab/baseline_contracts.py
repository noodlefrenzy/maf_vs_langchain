"""Shared contract for the paired single-agent baseline adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import AgentRole
from agent_lab.tool_catalog import tool_definitions_for_role
from agent_lab.workflow_contracts import (
    CommanderSynthesis,
    EvidencePacket,
    SpecialistFinding,
)

BASELINE_SOURCE_ROLES = (
    AgentRole.TELEMETRY_INVESTIGATOR,
    AgentRole.CHANGE_INVESTIGATOR,
    AgentRole.RELIABILITY_REVIEWER,
)
MIN_BASELINE_SOURCE_ROLES = 2
BASELINE_DISCOVERY_REPAIR_TURNS = 2
BASELINE_PROVENANCE_CORRECTION_TURNS = 1
BASELINE_PHASE_MODEL_CALL_LIMIT = 8
BASELINE_RECURSION_LIMIT = 128


@dataclass(frozen=True)
class BaselineSourceObservation:
    """Native tool evidence observed through one capability-scoped endpoint."""

    called_tools: frozenset[str]
    evidence_ids: frozenset[str]


class BaselineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BaselineFinding(SpecialistFinding):
    """One observation group labeled by capability source, never agent identity."""

    role: Literal[
        AgentRole.TELEMETRY_INVESTIGATOR,
        AgentRole.CHANGE_INVESTIGATOR,
        AgentRole.RELIABILITY_REVIEWER,
    ]


class BaselineProposal(BaselineModel):
    """One agent's synthesis, grouped only by the source of each observation."""

    findings: list[BaselineFinding] = Field(min_length=2, max_length=3)
    synthesis: CommanderSynthesis

    @model_validator(mode="after")
    def validate_source_roles(self) -> BaselineProposal:
        roles = [finding.role for finding in self.findings]
        if any(role not in BASELINE_SOURCE_ROLES for role in roles):
            raise ValueError("baseline findings must use capability-source roles")
        if len(roles) != len(set(roles)):
            raise ValueError("baseline findings must use unique capability-source roles")
        return self


def baseline_tool_names_for_source(role: AgentRole) -> tuple[str, ...]:
    """Assign each read-only specialist tool to exactly one source endpoint."""

    if role not in BASELINE_SOURCE_ROLES:
        raise ValueError(f"unsupported baseline source role {role.value}")
    names = []
    for definition in tool_definitions_for_role(role):
        if definition.mutates_world or definition.approval_required:
            continue
        if (
            AgentRole.INCIDENT_COMMANDER in definition.allowed_roles
            and role is not AgentRole.TELEMETRY_INVESTIGATOR
        ):
            continue
        names.append(definition.name)
    return tuple(names)


def baseline_evidence_allowlist(
    observations: Mapping[AgentRole, BaselineSourceObservation],
) -> str:
    """Render a stable per-source evidence allowlist for correction prompts."""

    return "\n".join(
        f"- {role.value}: {sorted(observation.evidence_ids)}"
        for role, observation in sorted(observations.items(), key=lambda item: item[0].value)
        if observation.called_tools
    )


def active_baseline_sources(
    observations: Mapping[AgentRole, BaselineSourceObservation],
) -> set[AgentRole]:
    """Return capability sources proven active by a native MCP call."""

    return {role for role, observation in observations.items() if observation.called_tools}


def require_baseline_discovery(
    observations: Mapping[AgentRole, BaselineSourceObservation],
) -> None:
    """Fail closed unless the one agent used at least two capability sources."""

    if len(active_baseline_sources(observations)) < MIN_BASELINE_SOURCE_ROLES:
        raise ValueError("single-agent discovery did not satisfy the two-source MCP gate")


def validate_baseline_provenance(
    proposal: BaselineProposal,
    observations: Mapping[AgentRole, BaselineSourceObservation],
) -> None:
    """Reject output that attributes tools or evidence to the wrong endpoint."""

    for finding in proposal.findings:
        observation = observations.get(finding.role)
        if observation is None or not observation.called_tools:
            raise ValueError(f"no MCP calls were made through {finding.role.value}")
        unknown_tools = set(finding.tools_used) - observation.called_tools
        if unknown_tools:
            raise ValueError(
                f"{finding.role.value} cited tools outside that endpoint's native calls: "
                f"unknown={sorted(unknown_tools)}, "
                f"observed={sorted(observation.called_tools)}"
            )
        if not set(finding.evidence_ids).issubset(observation.evidence_ids):
            raise ValueError(
                f"{finding.role.value} cited evidence outside that endpoint's native results: "
                f"cited={finding.evidence_ids}, observed={sorted(observation.evidence_ids)}"
            )


def evaluate_baseline_proposal(proposal: BaselineProposal) -> EvidencePacket:
    """Apply the held-constant evidence-quality gate without implying delegation."""

    findings = sorted(proposal.findings, key=lambda finding: finding.role.value)
    available_ids = {evidence_id for finding in findings for evidence_id in finding.evidence_ids}
    cited_ids = set(proposal.synthesis.evidence_ids)
    cited_roles = sorted(
        {finding.role for finding in findings if cited_ids.intersection(finding.evidence_ids)},
        key=lambda role: role.value,
    )
    reasons: list[str] = []
    if len(findings) < MIN_BASELINE_SOURCE_ROLES:
        reasons.append("at least two capability sources must report")
    if len(available_ids) < 2:
        reasons.append("at least two distinct evidence identifiers are required")
    unknown_ids = sorted(cited_ids - available_ids)
    if unknown_ids:
        reasons.append(f"single-agent synthesis cited unknown evidence identifiers: {unknown_ids}")
    if len(cited_roles) < MIN_BASELINE_SOURCE_ROLES:
        reasons.append("single-agent synthesis must cite at least two capability sources")
    if not proposal.synthesis.evidence_sufficient:
        reasons.append("single agent marked the evidence insufficient")
    return EvidencePacket(
        passed=not reasons,
        findings=findings,
        synthesis=proposal.synthesis,
        evidence_ids=sorted(available_ids),
        independent_roles=cited_roles,
        gate_reasons=reasons,
    )


def baseline_task_prompt(public_ticket: str) -> str:
    """Return the byte-identical task used by both single-agent adapters."""

    catalogs = "\n".join(
        f"- {role.value}: {list(baseline_tool_names_for_source(role))}"
        for role in BASELINE_SOURCE_ROLES
    )
    return (
        f"Public ticket:\n{public_ticket}\n\n"
        "Act as one incident-response agent. You have the union of the specialist discovery "
        "tools; there are no subagents and you must not claim delegation. Tool source roles "
        "below identify capability and evidence provenance only. Investigate through at least "
        "two source roles, collect at least two exact evidence IDs, establish chronology, and "
        "choose the least-destructive reversible mitigation. Investigate now using the tools. Do "
        "not return the structured proposal until a later message requests it, and never claim "
        "approval, mitigation, or recovery. The outer control plane owns those steps.\n\n"
        f"Source-role tool catalog:\n{catalogs}"
    )


def baseline_discovery_repair_prompt(missing_roles: list[str]) -> str:
    """Request real tool use when fewer than two source endpoints were consulted."""

    return (
        "The deterministic source-coverage gate is not yet satisfied: fewer than two "
        "capability sources have made real MCP calls. Continue the same one-agent investigation "
        f"using at least one tool from a remaining source: {missing_roles}. Do not invent results "
        "and do not return the structured proposal yet."
    )


def baseline_synthesis_prompt(allowlist: str) -> str:
    """Request grounded structured output after discovery has completed."""

    return (
        "Return one BaselineProposal now using only observations from your completed MCP calls. "
        "Group findings by endpoint source role, not by agent identity; there were no subagents. "
        "List only tools actually called and copy evidence IDs byte-for-byte. The control plane "
        f"observed this source-specific evidence-ID allowlist:\n{allowlist}\nThe synthesis must "
        "cite exact IDs from at least two included source roles. Do not claim approval, "
        "mitigation, or recovery."
    )


def baseline_provenance_correction_prompt(error: ValueError, allowlist: str) -> str:
    """Return the sole matched repair instruction after provenance validation fails."""

    return (
        "The previous BaselineProposal failed deterministic provenance validation: "
        f"{error}. Return one corrected BaselineProposal. Do not call more tools. Group findings "
        "by capability-source role and copy only exact evidence IDs from this allowlist:\n"
        f"{allowlist}"
    )


def baseline_contract_payload() -> dict[str, object]:
    """Return the frozen framework-neutral single-agent baseline contract."""

    return {
        "schema_version": "single-agent-baseline-contract/v1",
        "topology": {
            "model_driven_agents": 1,
            "delegation": False,
            "source_roles_are_provenance_labels": True,
            "outer_control_stages": ["approval", "mitigation", "recovery_verification"],
        },
        "source_roles": [role.value for role in BASELINE_SOURCE_ROLES],
        "source_tool_assignment": {
            role.value: list(baseline_tool_names_for_source(role)) for role in BASELINE_SOURCE_ROLES
        },
        "limits": {
            "minimum_source_roles": MIN_BASELINE_SOURCE_ROLES,
            "discovery_repair_turns": BASELINE_DISCOVERY_REPAIR_TURNS,
            "provenance_correction_turns": BASELINE_PROVENANCE_CORRECTION_TURNS,
            "phase_model_calls": BASELINE_PHASE_MODEL_CALL_LIMIT,
            "langchain_recursion_limit": BASELINE_RECURSION_LIMIT,
            "model_and_tool_calls": "manifest_budget_enforced_by_outer_runner",
        },
        "prompts": {
            "system_role": AgentRole.INCIDENT_COMMANDER.value,
            "task_template": baseline_task_prompt("{public_ticket}"),
            "discovery_repair_template": baseline_discovery_repair_prompt(
                ["{missing_source_roles}"]
            ),
            "synthesis_template": baseline_synthesis_prompt("{source_evidence_allowlist}"),
            "provenance_correction_template": baseline_provenance_correction_prompt(
                ValueError("{validation_error}"),
                "{source_evidence_allowlist}",
            ),
        },
        "schemas": {
            "proposal": BaselineProposal.model_json_schema(),
            "evidence_packet": EvidencePacket.model_json_schema(),
        },
    }
