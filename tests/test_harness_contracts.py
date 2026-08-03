import pytest
from pydantic import ValidationError

from agent_lab.contracts import AgentRole, HarnessAblationProfile
from agent_lab.harness_contracts import (
    HarnessCommanderSynthesis,
    HarnessProposal,
    HarnessRoleObservation,
    HarnessSpecialistFinding,
    evaluate_harness_proposal,
    harness_ablation_payload,
    harness_contract_payload,
    harness_evidence_allowlist,
    harness_specialist_roles,
    require_harness_delegation,
    validate_harness_role_provenance,
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


def _observations() -> dict[AgentRole, HarnessRoleObservation]:
    return {
        AgentRole.TELEMETRY_INVESTIGATOR: HarnessRoleObservation(
            called_tools=frozenset({"search_logs"}),
            evidence_ids=frozenset({"ev-telemetry"}),
        ),
        AgentRole.CHANGE_INVESTIGATOR: HarnessRoleObservation(
            called_tools=frozenset({"get_recent_deploys"}),
            evidence_ids=frozenset({"ev-change"}),
        ),
        AgentRole.RELIABILITY_REVIEWER: HarnessRoleObservation(
            called_tools=frozenset(),
            evidence_ids=frozenset(),
        ),
    }


def test_harness_native_provenance_accepts_only_observed_tools_and_evidence() -> None:
    proposal = HarnessProposal(
        findings=[
            _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry").model_copy(
                update={"tools_used": ["search_logs"]}
            ),
            _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change").model_copy(
                update={"tools_used": ["get_recent_deploys"]}
            ),
        ],
        synthesis=_synthesis(["ev-telemetry", "ev-change"]),
    )

    require_harness_delegation(_observations())
    validate_harness_role_provenance(proposal, _observations())
    assert harness_evidence_allowlist(_observations()) == (
        "- change_investigator: ['ev-change']\n- telemetry_investigator: ['ev-telemetry']"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("tools_used", ["read_file"], "tools outside its native MCP calls"),
        ("evidence_ids", ["ev-invented"], "evidence outside its native MCP results"),
    ],
)
def test_harness_native_provenance_rejects_unsupported_claims(
    field: str,
    value: list[str],
    message: str,
) -> None:
    telemetry = _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry").model_copy(
        update={"tools_used": ["search_logs"], field: value}
    )
    proposal = HarnessProposal(
        findings=[
            telemetry,
            _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change").model_copy(
                update={"tools_used": ["get_recent_deploys"]}
            ),
        ],
        synthesis=_synthesis(["ev-telemetry", "ev-change"]),
    )

    with pytest.raises(ValueError, match=message):
        validate_harness_role_provenance(proposal, _observations())


def test_harness_delegation_fails_closed_with_one_active_specialist() -> None:
    observations = _observations()
    observations[AgentRole.CHANGE_INVESTIGATOR] = HarnessRoleObservation(
        called_tools=frozenset(),
        evidence_ids=frozenset(),
    )

    with pytest.raises(ValueError, match="two-specialist MCP gate"):
        require_harness_delegation(observations)


def test_harness_contract_freezes_symmetric_repairs_and_limits() -> None:
    payload = harness_contract_payload()

    assert payload["delegation"] == {
        "minimum_active_specialist_roles": 2,
        "repair_turns": 2,
        "activity_proof": "at_least_one_native_mcp_call",
        "failure_mode": "error",
    }
    assert payload["provenance"] == {
        "correction_turns": 1,
        "supported_correction_turns": [0, 1, 2],
        "claimed_tools_must_be_observed": True,
        "claimed_evidence_ids_must_be_observed_for_same_role": True,
        "failure_mode": "error",
    }
    assert payload["phase_limits"]["model_calls"] == 6
    assert payload["ablation_space"]["prompt_variants"] == ["canonical", "paraphrase_a"]
    assert payload["ablation_space"]["reviewer_enabled"] == [True, False]
    assert payload["ablation_space"]["context_compaction"] == ["native", "disabled"]


def test_harness_ablation_payload_freezes_paraphrase_and_correction_policy() -> None:
    canonical = harness_ablation_payload(HarnessAblationProfile())
    ablated = harness_ablation_payload(
        HarnessAblationProfile(
            prompt_variant="paraphrase_a",
            provenance_correction_turns=0,
        )
    )

    assert canonical["prompt_variant"] == "canonical"
    assert canonical["provenance_correction_turns"] == 1
    assert canonical["reviewer_enabled"] is True
    assert canonical["context_compaction"] == "native"
    assert ablated["prompt_variant"] == "paraphrase_a"
    assert ablated["provenance_correction_turns"] == 0
    assert canonical["prompts"]["task_template"] != ablated["prompts"]["task_template"]
    assert canonical["prompts"]["specialists"] != ablated["prompts"]["specialists"]


def test_harness_ablation_payload_freezes_no_review_and_no_compaction() -> None:
    no_review_profile = HarnessAblationProfile(reviewer_enabled=False)
    no_review = harness_ablation_payload(no_review_profile)
    no_compaction = harness_ablation_payload(HarnessAblationProfile(context_compaction="disabled"))

    assert harness_specialist_roles(no_review_profile) == {
        AgentRole.CHANGE_INVESTIGATOR,
        AgentRole.TELEMETRY_INVESTIGATOR,
    }
    assert no_review["active_specialist_roles"] == [
        "change_investigator",
        "telemetry_investigator",
    ]
    assert set(no_review["prompts"]["specialists"]) == {
        "change_investigator",
        "telemetry_investigator",
    }
    assert (
        "both role-scoped telemetry and change specialists" in no_review["prompts"]["task_template"]
    )
    assert no_compaction["context_compaction"] == "disabled"


def test_harness_evidence_packet_records_provenance_correction_attempt() -> None:
    proposal = HarnessProposal(
        findings=[
            _finding(AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry"),
            _finding(AgentRole.CHANGE_INVESTIGATOR, "ev-change"),
        ],
        synthesis=_synthesis(["ev-telemetry", "ev-change"]),
    )

    assert evaluate_harness_proposal(proposal, attempt=1).attempt == 1
