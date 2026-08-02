"""Exercise hidden-world approval resume across separate service processes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent_lab.contracts import ApprovalDecision
from incident_lab_mcp.world import IncidentWorld


def prepare(checkpoint: Path, *, seed: int, pending: bool = False) -> dict[str, object]:
    world = IncidentWorld.from_seed(seed, fault_profile="none")
    evidence = [
        world.query_service_health(world.incident.affected_service)["evidence_id"],
        world.list_recent_changes(world.incident.affected_service)["evidence_id"],
        world.search_runbooks("reversible mitigation")["evidence_id"],
    ]
    request = world.request_mitigation(
        world.incident.correct_mitigation_id,
        world.incident.correct_mitigation,
        "Independent telemetry, change, and policy evidence support this action.",
        evidence,
    )
    request_id = str(request["request_id"])
    approval = (
        None
        if pending
        else world.decide_mitigation(
            request_id,
            ApprovalDecision.APPROVE,
            decided_by="restart-compat-reviewer",
        )
    )
    world.save_checkpoint(checkpoint)
    return {
        "phase": "prepared",
        "pid": os.getpid(),
        "request_id": request_id,
        "approval_state": "pending" if approval is None else approval.decision.value,
        "world_tick": world.world_tick,
        "applied": world.applied_request_id is not None,
    }


def resume(checkpoint: Path) -> dict[str, object]:
    world = IncidentWorld.load_checkpoint(checkpoint)
    if len(world.approval_requests) != 1 or len(world.approval_records) > 1:
        raise RuntimeError("restart fixture requires exactly one mitigation request")
    request_id = next(iter(world.approval_requests))
    approval = world.approval_records.get(request_id)
    if approval is None:
        approval = world.decide_mitigation(
            request_id,
            ApprovalDecision.APPROVE,
            decided_by="restart-compat-reviewer",
        )
    result = world.apply_mitigation(
        approval.request_id,
        approval.approval_token or "",
    )
    replay = world.apply_mitigation(
        approval.request_id,
        approval.approval_token or "",
    )
    recovery = world.verify_recovery([world.incident.affected_service])
    state_changing_applies = [
        event
        for event in world.audit_log
        if event.operation == "apply_mitigation" and event.state_changed
    ]
    return {
        "phase": "resumed",
        "pid": os.getpid(),
        "request_id": approval.request_id,
        "approval_state": approval.decision.value,
        "world_tick": world.world_tick,
        "applied": result["effective"],
        "first_apply_idempotent": result["idempotent_replay"],
        "replay_idempotent": replay["idempotent_replay"],
        "state_changing_apply_count": len(state_changing_applies),
        "recovered": recovery["recovered"],
        "audit_sequences": [event.sequence for event in world.audit_log],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--checkpoint", type=Path, required=True)
    prepare_parser.add_argument("--seed", type=int, default=42)
    prepare_parser.add_argument("--pending", action="store_true")
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()

    if args.phase == "prepare":
        result = prepare(args.checkpoint, seed=args.seed, pending=args.pending)
    else:
        result = resume(args.checkpoint)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
