"""Exercise hidden-world approval resume across separate service processes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agent_lab.contracts import ApprovalDecision
from incident_lab_mcp.world import IncidentWorld


def prepare(checkpoint: Path, *, seed: int) -> dict[str, object]:
    world = IncidentWorld.from_seed(seed, fault_profile="none")
    evidence = [
        world.query_service_health(world.incident.affected_service)["evidence_id"],
        world.list_recent_changes(world.incident.affected_service)["evidence_id"],
        world.search_runbooks("reversible mitigation")["evidence_id"],
    ]
    request = world.request_mitigation(
        world.incident.correct_mitigation,
        "Independent telemetry, change, and policy evidence support this action.",
        evidence,
    )
    approval = world.decide_mitigation(
        str(request["request_id"]),
        ApprovalDecision.APPROVE,
        decided_by="restart-compat-reviewer",
    )
    world.save_checkpoint(checkpoint)
    return {
        "phase": "prepared",
        "pid": os.getpid(),
        "request_id": approval.request_id,
        "world_tick": world.world_tick,
        "applied": world.applied_request_id is not None,
    }


def resume(checkpoint: Path) -> dict[str, object]:
    world = IncidentWorld.load_checkpoint(checkpoint)
    if len(world.approval_records) != 1:
        raise RuntimeError("restart fixture requires exactly one approval record")
    approval = next(iter(world.approval_records.values()))
    result = world.apply_mitigation(
        approval.request_id,
        approval.approval_token or "",
    )
    recovery = world.verify_recovery([world.incident.affected_service])
    return {
        "phase": "resumed",
        "pid": os.getpid(),
        "request_id": approval.request_id,
        "world_tick": world.world_tick,
        "applied": result["effective"],
        "recovered": recovery["recovered"],
        "audit_sequences": [event.sequence for event in world.audit_log],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--checkpoint", type=Path, required=True)
    prepare_parser.add_argument("--seed", type=int, default=42)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()

    if args.phase == "prepare":
        result = prepare(args.checkpoint, seed=args.seed)
    else:
        result = resume(args.checkpoint)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
