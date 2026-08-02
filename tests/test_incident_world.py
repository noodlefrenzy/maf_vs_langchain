import json
import stat
import subprocess
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
        world.incident.correct_mitigation_id,
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


def test_canonical_action_id_is_authoritative_over_display_description() -> None:
    world = IncidentWorld.from_seed(42, fault_profile="none")
    evidence_ids = _collect_evidence(world)
    display_description = "Revert the retry-policy change, then enforce a bounded retry cap."
    request = world.request_mitigation(
        world.incident.correct_mitigation_id,
        display_description,
        "Canonical identity selects the operation; this prose is for operators.",
        evidence_ids,
    )
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )

    result = world.apply_mitigation(
        approval.request_id,
        approval.approval_token or "",
    )

    assert result["action_id"] == world.incident.correct_mitigation_id.value
    assert result["action"] == world.incident.correct_mitigation
    assert result["effective"] is True
    assert world.evaluation_state().applied_action_id == world.incident.correct_mitigation_id
    request_event = next(
        event for event in world.audit_log if event.operation == "request_mitigation"
    )
    assert request_event.arguments["proposed_action"] == display_description


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
        world.incident.forbidden_mitigation_id,
        world.incident.forbidden_mitigation,
        "Initial unsafe proposal for edit-path testing.",
        evidence_ids,
    )
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.EDIT,
        decided_by="scripted-reviewer",
        edited_action_id=world.incident.correct_mitigation_id,
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


def test_invalid_tool_arguments_are_audited_without_evidence() -> None:
    world = IncidentWorld.from_seed(100, fault_profile="none")

    with pytest.raises(WorldToolError) as invalid:
        world.get_feature_flag_diff("checkout-rollout", ["us-east"])

    assert invalid.value.error.code is ToolErrorCode.INVALID_ARGUMENTS
    assert invalid.value.error.details == {"regions": ["us-east"]}
    assert world.observed_evidence_ids == set()
    assert world.audit_log[-1].operation == "get_feature_flag_diff"
    assert world.audit_log[-1].outcome == "failed"


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


@pytest.mark.parametrize("seed", [0, 42, 101, 1742])
def test_invalid_control_operations_do_not_advance_world_time(seed: int) -> None:
    world = IncidentWorld.from_seed(seed, fault_profile="none")
    paired = IncidentWorld.from_seed(seed, fault_profile="none")

    with pytest.raises(WorldToolError) as missing_decision:
        world.decide_mitigation(
            "missing-request",
            ApprovalDecision.APPROVE,
            decided_by="scripted-reviewer",
        )
    assert missing_decision.value.error.code is ToolErrorCode.NOT_FOUND
    assert world.world_tick == 0

    with pytest.raises(WorldToolError) as missing_timeout:
        world.timeout_mitigation("missing-request")
    assert missing_timeout.value.error.code is ToolErrorCode.NOT_FOUND
    assert world.world_tick == 0

    assert world.query_service_health(world.incident.affected_service) == (
        paired.query_service_health(paired.incident.affected_service)
    )


def test_rejected_duplicate_control_operation_is_tick_neutral() -> None:
    world = IncidentWorld.from_seed(101, fault_profile="none")
    request = _request_correct_mitigation(world)
    request_id = str(request["request_id"])
    world.decide_mitigation(
        request_id,
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )
    decided_tick = world.world_tick

    with pytest.raises(WorldToolError) as duplicate:
        world.decide_mitigation(
            request_id,
            ApprovalDecision.APPROVE,
            decided_by="scripted-reviewer",
        )

    assert duplicate.value.error.code is ToolErrorCode.CONFLICT
    assert world.world_tick == decided_tick


def test_approval_timeout_is_audited_and_cannot_execute() -> None:
    world = IncidentWorld.from_seed(102, fault_profile="none")
    request = _request_correct_mitigation(world)
    request_id = str(request["request_id"])
    state_before_timeout = world.evaluation_state()

    timeout = world.timeout_mitigation(request_id)

    assert timeout["status"] == "timed_out"
    assert world.evaluation_state() == state_before_timeout
    assert world.get_incident_status()["state"] == "approval_timed_out"
    assert world.audit_log[-2].operation == "approval_timeout"
    assert world.audit_log[-2].outcome == "decided"
    with pytest.raises(WorldToolError) as timed_out:
        world.apply_mitigation(request_id, "any-token")
    assert timed_out.value.error.code is ToolErrorCode.APPROVAL_REJECTED
    assert timed_out.value.error.details == {"resolution": "timeout"}
    with pytest.raises(WorldToolError) as late_approval:
        world.decide_mitigation(
            request_id,
            ApprovalDecision.APPROVE,
            decided_by="late-reviewer",
        )
    assert late_approval.value.error.code is ToolErrorCode.CONFLICT


def test_non_ascii_approval_token_is_stale_not_internal_error() -> None:
    world = IncidentWorld.from_seed(102, fault_profile="none")
    request = _request_correct_mitigation(world)
    request_id = str(request["request_id"])
    world.decide_mitigation(
        request_id,
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )

    with pytest.raises(WorldToolError) as stale:
        world.apply_mitigation(request_id, "not-authorized-\N{EN DASH}")

    assert stale.value.error.code is ToolErrorCode.STALE_APPROVAL
    assert world.applied_action is None


def test_world_checkpoint_is_owner_only_and_immutable(tmp_path: Path) -> None:
    world = IncidentWorld.from_seed(103, fault_profile="none")
    request = _request_correct_mitigation(world)
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )
    checkpoint = tmp_path / "world-checkpoint.json"

    world.save_checkpoint(checkpoint)
    restored = IncidentWorld.load_checkpoint(checkpoint)
    result = restored.apply_mitigation(
        approval.request_id,
        approval.approval_token or "",
    )

    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert result["effective"] is True
    assert restored.audit_log[-1].sequence == len(restored.audit_log) - 1
    with pytest.raises(FileExistsError):
        world.save_checkpoint(checkpoint)


def test_checkpoint_after_approved_action_replays_idempotently(tmp_path: Path) -> None:
    world = IncidentWorld.from_seed(105, fault_profile="none")
    request = _request_correct_mitigation(world)
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )
    token = approval.approval_token or ""
    first = world.apply_mitigation(approval.request_id, token)
    checkpoint = world.save_checkpoint(tmp_path / "applied-world-checkpoint.json")

    restored = IncidentWorld.load_checkpoint(checkpoint)
    replay = restored.apply_mitigation(approval.request_id, token)

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert restored.applied_request_id == approval.request_id
    assert restored.applied_action == first["action"]
    assert restored.audit_log[-1].operation == "apply_mitigation"
    assert restored.audit_log[-1].state_changed is False


def test_approved_mitigation_resumes_in_a_fresh_process(tmp_path: Path) -> None:
    checkpoint = tmp_path / "restart-checkpoint.json"
    command = [
        "uv",
        "run",
        "--project",
        "services/incident_mcp",
        "python",
        "-m",
        "incident_lab_mcp.restart_compat",
    ]
    prepared = subprocess.run(
        [*command, "prepare", "--checkpoint", str(checkpoint), "--seed", "104"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    resumed = subprocess.run(
        [*command, "resume", "--checkpoint", str(checkpoint)],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    prepared_payload = json.loads(prepared.stdout)
    resumed_payload = json.loads(resumed.stdout)

    assert prepared_payload["pid"] != resumed_payload["pid"]
    assert prepared_payload["applied"] is False
    assert resumed_payload["applied"] is True
    assert resumed_payload["first_apply_idempotent"] is False
    assert resumed_payload["replay_idempotent"] is True
    assert resumed_payload["state_changing_apply_count"] == 1
    assert resumed_payload["recovered"] is True
    assert resumed_payload["request_id"] == prepared_payload["request_id"]
    assert resumed_payload["audit_sequences"] == list(
        range(len(resumed_payload["audit_sequences"]))
    )


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


def test_transient_failure_cadence_survives_checkpoint_resume(tmp_path: Path) -> None:
    world = IncidentWorld.from_seed(42, fault_profile="standard")
    cadence = world.incident.transient_failure_every
    for _ in range(cadence - 1):
        world.get_incident_brief()
    checkpoint = world.save_checkpoint(tmp_path / "fault-checkpoint.json")

    restored = IncidentWorld.load_checkpoint(checkpoint)

    with pytest.raises(WorldToolError) as scheduled:
        restored.get_incident_brief()
    assert scheduled.value.error.code is ToolErrorCode.TRANSIENT_TIMEOUT
    assert restored.tool_calls == cadence
    assert len(restored.audit_log) == cadence


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


def test_successful_recovery_verification_is_monotonic_across_restart(tmp_path: Path) -> None:
    world = IncidentWorld.from_seed(42, fault_profile="none")
    request = _request_correct_mitigation(world)
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.APPROVE,
        decided_by="scripted-reviewer",
    )
    world.apply_mitigation(approval.request_id, approval.approval_token or "")

    successful = world.verify_recovery([world.incident.affected_service])
    checkpoint = world.save_checkpoint(tmp_path / "recovered-world.json")
    restored = IncidentWorld.load_checkpoint(checkpoint)
    successful_ids = {
        successful["evidence_id"],
        *(check["evidence_id"] for check in successful["checks"]),
    }
    defensive = restored.verify_recovery(
        [world.incident.affected_service, world.incident.decoy_service]
    )

    assert successful["recovered"] is True
    assert successful_ids <= restored.observed_evidence_ids
    assert defensive["recovered"] is False
    assert restored.recovery_verified is True
    assert restored.evaluation_state().recovery_verified is True


def test_every_returned_recovery_evidence_id_is_citable() -> None:
    world = IncidentWorld.from_seed(42, fault_profile="none")
    recovery = world.verify_recovery(
        [world.incident.affected_service, world.incident.decoy_service]
    )
    returned_ids = {
        recovery["evidence_id"],
        *(check["evidence_id"] for check in recovery["checks"]),
    }

    assert returned_ids <= world.observed_evidence_ids
    hypothesis = world.submit_hypothesis(
        "Recovery evidence remains citable",
        [check["evidence_id"] for check in recovery["checks"]],
        world.incident.correct_mitigation,
    )
    assert hypothesis["status"] == "recorded_for_evaluation"


def test_minted_but_unreturned_evidence_is_not_observed() -> None:
    world = IncidentWorld.from_seed(42, fault_profile="none")
    minted = world._evidence_id("never-returned", world.world_tick + 1)

    with pytest.raises(WorldToolError) as invalid_result:
        world._run_tool(
            "verify_recovery",
            {"services": [world.incident.affected_service]},
            lambda _tick: {"evidence_id": minted},
        )

    assert invalid_result.value.error.code is ToolErrorCode.INTERNAL_ERROR
    assert minted not in world.observed_evidence_ids
