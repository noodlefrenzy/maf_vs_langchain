from __future__ import annotations

import pytest

from agent_lab.baseline_contracts import (
    BASELINE_PHASE_MODEL_CALL_LIMIT,
    BASELINE_SOURCE_ROLES,
    BaselineFinding,
    BaselineProposal,
    BaselineSourceObservation,
    active_baseline_sources,
    baseline_contract_payload,
    baseline_task_prompt,
    baseline_tool_names_for_source,
    evaluate_baseline_proposal,
    require_baseline_discovery,
    validate_baseline_provenance,
)
from agent_lab.contracts import AgentRole
from agent_lab.workflow_contracts import CommanderSynthesis


def _proposal() -> BaselineProposal:
    return BaselineProposal(
        findings=[
            BaselineFinding(
                role=AgentRole.TELEMETRY_INVESTIGATOR,
                summary="Retries increased after the change.",
                claims=["checkout retries are elevated"],
                evidence_ids=["ev-telemetry"],
                tools_used=["query_metric"],
            ),
            BaselineFinding(
                role=AgentRole.CHANGE_INVESTIGATOR,
                summary="The retry configuration changed immediately beforehand.",
                claims=["a retry configuration change preceded impact"],
                evidence_ids=["ev-change"],
                tools_used=["get_config_diff"],
            ),
        ],
        synthesis=CommanderSynthesis(
            root_cause="A retry configuration change amplified load.",
            chronology=["configuration changed", "retry traffic increased"],
            evidence_ids=["ev-change", "ev-telemetry"],
            proposed_mitigation_id="rollback_checkout_retry_policy",
            proposed_mitigation="Revert the checkout retry budget.",
            recovery_checks=["verify checkout health"],
            evidence_sufficient=True,
        ),
    )


def _observations() -> dict[AgentRole, BaselineSourceObservation]:
    return {
        AgentRole.TELEMETRY_INVESTIGATOR: BaselineSourceObservation(
            called_tools=frozenset({"query_metric"}),
            evidence_ids=frozenset({"ev-telemetry"}),
        ),
        AgentRole.CHANGE_INVESTIGATOR: BaselineSourceObservation(
            called_tools=frozenset({"get_config_diff"}),
            evidence_ids=frozenset({"ev-change"}),
        ),
        AgentRole.RELIABILITY_REVIEWER: BaselineSourceObservation(
            called_tools=frozenset(),
            evidence_ids=frozenset(),
        ),
    }


def test_baseline_tool_union_is_unique_and_capability_scoped() -> None:
    assignments = {role: baseline_tool_names_for_source(role) for role in BASELINE_SOURCE_ROLES}
    flattened = [name for names in assignments.values() for name in names]

    assert len(flattened) == len(set(flattened))
    assert set(flattened) == {
        "get_incident_brief",
        "get_incident_status",
        "query_service_health",
        "search_logs",
        "query_metric",
        "lookup_trace",
        "list_recent_changes",
        "get_change_detail",
        "get_config_diff",
        "get_feature_flag_diff",
        "get_schema_diff",
        "search_runbooks",
        "get_runbook",
    }
    assert "request_mitigation" not in flattened
    assert "apply_mitigation" not in flattened


def test_baseline_provenance_and_evidence_gate_accept_two_sources() -> None:
    proposal = _proposal()

    validate_baseline_provenance(proposal, _observations())
    packet = evaluate_baseline_proposal(proposal)

    assert packet.passed is True
    assert packet.independent_roles == [
        AgentRole.CHANGE_INVESTIGATOR,
        AgentRole.TELEMETRY_INVESTIGATOR,
    ]


def test_baseline_discovery_requires_two_real_source_calls() -> None:
    observations = _observations()

    require_baseline_discovery(observations)
    assert active_baseline_sources(observations) == {
        AgentRole.CHANGE_INVESTIGATOR,
        AgentRole.TELEMETRY_INVESTIGATOR,
    }
    observations[AgentRole.CHANGE_INVESTIGATOR] = BaselineSourceObservation(
        called_tools=frozenset(),
        evidence_ids=frozenset(),
    )
    with pytest.raises(ValueError, match="two-source MCP gate"):
        require_baseline_discovery(observations)


@pytest.mark.parametrize("field", ["tool", "evidence"])
def test_baseline_provenance_rejects_cross_source_claims(field: str) -> None:
    proposal = _proposal()
    update = (
        {"tools_used": ["get_config_diff"]} if field == "tool" else {"evidence_ids": ["ev-change"]}
    )
    proposal.findings[0] = proposal.findings[0].model_copy(update=update)

    with pytest.raises(ValueError, match="outside that endpoint's native"):
        validate_baseline_provenance(proposal, _observations())


def test_baseline_contract_declares_one_agent_and_no_delegation() -> None:
    payload = baseline_contract_payload()
    prompt = baseline_task_prompt("checkout is unhealthy")

    assert payload["topology"]["model_driven_agents"] == 1
    assert payload["topology"]["delegation"] is False
    assert payload["limits"]["discovery_repair_turns"] == 2
    assert payload["limits"]["phase_model_calls"] == BASELINE_PHASE_MODEL_CALL_LIMIT == 8
    assert "there are no subagents" in prompt
    assert "must not claim delegation" in prompt
