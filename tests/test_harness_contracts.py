import pytest
from pydantic import ValidationError

from agent_lab.contracts import AgentRole
from agent_lab.harness_contracts import (
    HarnessCommanderSynthesis,
    HarnessProposal,
    HarnessSpecialistFinding,
    evaluate_harness_proposal,
)
from agent_lab.scenario import MitigationActionId


def _finding(role: AgentRole, evidence_id: str) -> HarnessSpecialistFinding:
    return HarnessSpecialistFinding(
        role=role,
        summary=f"finding from {role.value}",
        claims=[f"claim from {role.value}"],
        evidence_ids=[evidence_id],
        tools_used=[f"{role.value}_tool"],
    )


def _synthesis(evidence_ids: list[str]) -> HarnessCommanderSynthesis:
    return HarnessCommanderSynthesis(
        root_cause="retry amplification after a checkout deployment",
        chronology=["deployment changed retries", "customer errors rose"],
        evidence_ids=evidence_ids,
        proposed_mitigation_id=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
        proposed_mitigation="roll back the checkout deployment",
        recovery_checks=["verify checkout error rate"],
        evidence_sufficient=True,
    )


def test_harness_gate_accepts_two_independent_specialists() -> None:
    proposal = HarnessProposal(
        findings=[
            _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry"),
            _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change"),
        ],
        synthesis=_synthesis(["ev-telemetry", "ev-change"]),
    )

    packet = evaluate_harness_proposal(proposal)

    assert packet.passed
    assert packet.gate_reasons == []
    assert packet.independent_roles == [
        AgentRole.CHANGE_INVESTIGATOR,
        AgentRole.TELEMETRY_INVESTIGATOR,
    ]


def test_harness_gate_rejects_unknown_or_single_role_citations() -> None:
    proposal = HarnessProposal(
        findings=[
            _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry"),
            _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change"),
        ],
        synthesis=_synthesis(["ev-telemetry", "ev-invented"]),
    )

    packet = evaluate_harness_proposal(proposal)

    assert not packet.passed
    assert packet.gate_reasons == [
        "commander cited unknown evidence identifiers: ['ev-invented']",
        "commander synthesis must cite at least two specialist roles",
    ]


def test_harness_proposal_rejects_duplicate_specialist_roles() -> None:
    with pytest.raises(ValidationError, match="unique specialist roles"):
        HarnessProposal(
            findings=[
                _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-one"),
                _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-two"),
            ],
            synthesis=_synthesis(["ev-one", "ev-two"]),
        )
