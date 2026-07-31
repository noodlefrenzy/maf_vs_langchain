import json
from pathlib import Path

import pytest
from incident_lab_mcp.world import IncidentWorld, WorldToolError

from agent_lab.contracts import ApprovalDecision, ToolErrorCode
from agent_lab.scenario import IncidentVariant, generate_incident


def _collect_evidence(world: IncidentWorld) -> list[str]:
    telemetry = world.query_service_health(world.incident.affected_service)
    changes = world.list_recent_changes(world.incident.affected_service)
    runbook = world.search_runbooks("reversible mitigation")
    return [
        telemetry["evidence_id"],
        changes["evidence_id"],
        runbook["evidence_id"],
    ]


def _request_correct_mitigation(world: IncidentWorld) -> dict[str, object]:
    evidence_ids = _collect_evidence(world)
    world.submit_hypothesis(
        f"{world.incident.variant.value} in {world.incident.affected_service}",
        evidence_ids,
        world.incident.correct_mitigation,
    )
    return world.request_mitigation(
        world.incident.correct_mitigation,
        "Independent telemetry, change, and policy evidence support this action.",
        evidence_ids,
    )


def test_mitigation_requires_approval_and_is_idempotent(tmp_path: Path) -> None:
    audit_path = tmp_path / "world-audit.jsonl"
    world = IncidentWorld.from_seed(42, fault_profile="none", audit_path=audit_path)
    request = _request_correct_mitigation(world)
    request_id = str(request["request_id"])

    assert world.applied_action is None
    with pytest.raises(WorldToolError) as pending:
        world.apply_mitigation(request_id, "not-authorized")
    assert pending.value.error.code is ToolErrorCode.APPROVAL_REQUIRED
    assert world.applied_action is None

    approval = world.decide_mitigation(
        request_id,
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )
    assert approval.approval_token is not None

    with pytest.raises(WorldToolError) as stale:
        world.apply_mitigation(request_id, "wrong-token")
    assert stale.value.error.code is ToolErrorCode.STALE_APPROVAL
    assert world.applied_action is None

    first = world.apply_mitigation(request_id, approval.approval_token)
    second = world.apply_mitigation(request_id, approval.approval_token)
    recovery = world.verify_recovery([world.incident.affected_service])

    assert first["effective"] is True
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert recovery["recovered"] is True
    assert world.recovery_verified is True
    assert [event.sequence for event in world.audit_log] == list(range(len(world.audit_log)))
    assert all(
        event.arguments.get("approval_token") in {None, "[REDACTED]"} for event in world.audit_log
    )
    persisted = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(persisted) == len(world.audit_log)
    assert all(json.loads(line)["schema_version"] == "0.1.0" for line in persisted)


def test_rejected_request_cannot_execute() -> None:
    world = IncidentWorld.from_seed(73, fault_profile="none")
    request = _request_correct_mitigation(world)
    request_id = str(request["request_id"])
    rejection = world.decide_mitigation(
        request_id,
        ApprovalDecision.REJECT,
        decided_by="scripted-reviewer",
    )

    assert rejection.approval_token is None
    with pytest.raises(WorldToolError) as rejected:
        world.apply_mitigation(request_id, "any-token")
    assert rejected.value.error.code is ToolErrorCode.APPROVAL_REJECTED
    assert world.applied_action is None


def test_edited_approval_only_authorizes_edited_action() -> None:
    world = IncidentWorld.from_seed(99, fault_profile="none")
    evidence_ids = _collect_evidence(world)
    request = world.request_mitigation(
        world.incident.forbidden_mitigation,
        "Initial unsafe proposal for edit-path testing.",
        evidence_ids,
    )
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.EDIT,
        decided_by="scripted-reviewer",
        edited_action=world.incident.correct_mitigation,
    )

    assert approval.action_to_execute == world.incident.correct_mitigation
    result = world.apply_mitigation(
        approval.request_id,
        approval.approval_token or "",
    )
    assert result["action"] == world.incident.correct_mitigation
    assert result["effective"] is True
    assert world.safety_violation is False


def test_duplicate_decision_is_audited_conflict() -> None:
    world = IncidentWorld.from_seed(101, fault_profile="none")
    request = _request_correct_mitigation(world)
    request_id = str(request["request_id"])
    world.decide_mitigation(
        request_id,
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )

    with pytest.raises(WorldToolError) as duplicate:
        world.decide_mitigation(
            request_id,
            ApprovalDecision.APPROVE,
            decided_by="scripted-reviewer",
        )

    assert duplicate.value.error.code is ToolErrorCode.CONFLICT
    assert world.audit_log[-1].operation == "approval_decision"
    assert world.audit_log[-1].outcome == "failed"


def test_seeded_transient_failure_schedule_replays_exactly() -> None:
    first = IncidentWorld.from_seed(42, fault_profile="standard")
    second = IncidentWorld.from_seed(42, fault_profile="standard")
    cadence = first.incident.transient_failure_every

    def outcomes(world: IncidentWorld) -> list[str]:
        observed = []
        for _ in range(cadence * 2):
            try:
                world.get_incident_brief()
            except WorldToolError as exc:
                observed.append(exc.error.code.value)
            else:
                observed.append("succeeded")
        return observed

    first_outcomes = outcomes(first)
    assert first_outcomes == outcomes(second)
    assert first_outcomes[cadence - 1] == ToolErrorCode.TRANSIENT_TIMEOUT.value
    assert first_outcomes[(cadence * 2) - 1] == ToolErrorCode.TRANSIENT_TIMEOUT.value


def test_every_incident_family_recovers_under_correct_mitigation() -> None:
    seeds_by_variant: dict[IncidentVariant, int] = {}
    for seed in range(1000):
        incident = generate_incident(seed)
        seeds_by_variant.setdefault(incident.variant, seed)
        if len(seeds_by_variant) == len(IncidentVariant):
            break

    assert set(seeds_by_variant) == set(IncidentVariant)
    for seed in seeds_by_variant.values():
        world = IncidentWorld.from_seed(seed, fault_profile="none")
        request = _request_correct_mitigation(world)
        approval = world.decide_mitigation(
            str(request["request_id"]),
            ApprovalDecision.APPROVE,
            decided_by="scripted-reviewer",
        )
        result = world.apply_mitigation(
            approval.request_id,
            approval.approval_token or "",
        )
        recovery = world.verify_recovery([world.incident.affected_service])

        assert result["effective"] is True
        assert recovery["recovered"] is True
