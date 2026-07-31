"""Scripted oracle that validates the simulator, scorer, and bundle pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, TypeVar

from agent_lab.config import load_experiment, load_model_profile
from agent_lab.contract_bundle import (
    canonical_json,
    contract_bundle_hashes,
    sha256_payload,
)
from agent_lab.contracts import (
    ApprovalDecision,
    EventKind,
    EvidenceItem,
    EvidenceLedger,
    FrameworkLane,
    IncidentReport,
    RunManifest,
    RunTerminal,
    TerminalStatus,
    ToolErrorCode,
)
from agent_lab.evaluator import DeterministicScore, score_incident
from agent_lab.run_bundle import RunBundle
from incident_lab_mcp.world import IncidentWorld, WorldToolError

_T = TypeVar("_T")


def run_oracle(
    *,
    seed: int,
    runs_dir: Path,
    experiment_path: Path,
    repetition: int = 0,
) -> tuple[Path, DeterministicScore]:
    """Execute a deterministic successful policy and persist a complete bundle."""

    experiment = load_experiment(experiment_path)
    model = load_model_profile(Path(experiment.model_profile))
    world = IncidentWorld.from_seed(seed, fault_profile=experiment.fault_profile)
    hashes = contract_bundle_hashes()
    manifest = RunManifest(
        experiment_id=f"{experiment.experiment_id}_oracle",
        lane=FrameworkLane.SCRIPTED_ORACLE,
        scenario=experiment.scenario,
        scenario_generator_version=world.incident.generator_version,
        scenario_seed=seed,
        repetition=repetition,
        model=model,
        budget=experiment.budget,
        source_commit=_source_commit(),
        source_tree_sha256=_source_tree_sha256(),
        framework_versions={
            "agent-framework-field-lab": _package_version("agent-framework-field-lab"),
            "incident-lab-mcp": _package_version("incident-lab-mcp"),
            "scripted-oracle": "0.1.0",
        },
        dependency_lock_sha256=_file_sha256(Path("services/incident_mcp/uv.lock")),
        prompt_bundle_sha256=hashes["prompt_bundle_sha256"],
        tool_catalog_sha256=hashes["tool_catalog_sha256"],
        world_manifest_sha256=sha256_payload(world.incident.model_dump(mode="json")),
        host_fingerprint={
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
    )
    bundle = RunBundle.create(runs_dir, manifest)
    started = time.monotonic()
    bundle.emit(
        EventKind.RUN_STARTED,
        actor="scripted-oracle",
        payload={
            "scenario_seed": seed,
            "world_manifest_sha256": manifest.world_manifest_sha256,
        },
    )
    bundle.emit(EventKind.AGENT_STARTED, actor="scripted-oracle")

    def call_tool(
        name: str,
        actor: str,
        operation: Callable[[], _T],
    ) -> _T:
        while True:
            if world.tool_calls >= experiment.budget.tool_calls:
                raise RuntimeError("scripted oracle exhausted its tool-call budget")
            requested = bundle.emit(
                EventKind.TOOL_REQUESTED,
                actor=actor,
                payload={"tool_name": name},
            )
            bundle.emit(
                EventKind.TOOL_STARTED,
                actor=actor,
                parent_event_id=requested.event_id,
                payload={"tool_name": name},
            )
            try:
                result = operation()
            except WorldToolError as exc:
                bundle.emit(
                    EventKind.TOOL_FAILED,
                    actor=actor,
                    parent_event_id=requested.event_id,
                    payload={
                        "tool_name": name,
                        "error": exc.error.model_dump(mode="json"),
                    },
                )
                if exc.error.code is ToolErrorCode.TRANSIENT_TIMEOUT:
                    continue
                raise
            audit = world.audit_log[-1]
            bundle.emit(
                EventKind.TOOL_FINISHED,
                actor=actor,
                parent_event_id=requested.event_id,
                payload={
                    "tool_name": name,
                    "world_tick": audit.world_tick,
                    "result_sha256": audit.result_sha256,
                    "result_size_bytes": audit.result_size_bytes,
                    "evidence_id": audit.evidence_id,
                },
            )
            return result

    try:
        metric = call_tool(
            "query_metric",
            "telemetry_investigator",
            lambda: world.query_metric(
                world.incident.affected_service,
                "causal_signal",
            ),
        )
        changes = call_tool(
            "list_recent_changes",
            "change_investigator",
            lambda: world.list_recent_changes(world.incident.affected_service),
        )
        runbook = call_tool(
            "search_runbooks",
            "reliability_reviewer",
            lambda: world.search_runbooks("reversible mitigation"),
        )
        evidence = [
            _evidence_item(
                world,
                metric,
                source_role="telemetry_investigator",
                source_tool="query_metric",
                claim=world.incident.required_evidence[0],
                supports="root_cause",
            ),
            _evidence_item(
                world,
                changes,
                source_role="change_investigator",
                source_tool="list_recent_changes",
                claim=(f"{world.incident.required_evidence[1]} before customer errors at tick 4"),
                supports="root_cause",
            ),
            _evidence_item(
                world,
                runbook,
                source_role="reliability_reviewer",
                source_tool="search_runbooks",
                claim=(
                    "Policy requires two independent sources before an approved reversible action."
                ),
                supports="mitigation",
            ),
        ]
        evidence_ids = [item.evidence_id for item in evidence]
        root_cause = (
            f"{world.incident.variant.value} in {world.incident.affected_service}: "
            f"{world.incident.required_evidence[0]}; "
            f"{world.incident.required_evidence[1]}"
        )
        call_tool(
            "submit_hypothesis",
            "incident_commander",
            lambda: world.submit_hypothesis(
                root_cause,
                evidence_ids,
                world.incident.correct_mitigation,
            ),
        )
        request = call_tool(
            "request_mitigation",
            "incident_commander",
            lambda: world.request_mitigation(
                world.incident.correct_mitigation,
                "Independent telemetry, change, and policy evidence support this action.",
                evidence_ids,
            ),
        )
        bundle.emit(
            EventKind.APPROVAL_REQUESTED,
            actor="incident_commander",
            payload={
                "request_id": request["request_id"],
                "proposed_action": request["proposed_action"],
                "world_tick": request["created_tick"],
            },
        )
        bundle.emit(
            EventKind.CHECKPOINT_SAVED,
            actor="scripted-oracle",
            payload={"reason": "awaiting_approval"},
        )
        approval = world.decide_mitigation(
            request["request_id"],
            ApprovalDecision.APPROVE,
            decided_by="scripted-reviewer",
        )
        bundle.emit(
            EventKind.APPROVAL_DECIDED,
            actor="scripted-reviewer",
            payload={
                "request_id": approval.request_id,
                "decision": approval.decision.value,
                "world_tick": approval.decided_tick,
            },
        )
        mitigation = call_tool(
            "apply_mitigation",
            "incident_commander",
            lambda: world.apply_mitigation(
                approval.request_id,
                approval.approval_token or "",
            ),
        )
        bundle.emit(
            EventKind.STATE_UPDATED,
            actor="incident_commander",
            payload={
                "phase": "mitigation",
                "request_id": mitigation["request_id"],
                "effective": mitigation["effective"],
            },
        )
        recovery = call_tool(
            "verify_recovery",
            "incident_commander",
            lambda: world.verify_recovery([world.incident.affected_service]),
        )
        report = IncidentReport(
            case_id=world.incident.case_id,
            root_cause=root_cause,
            confidence=1.0,
            evidence=evidence,
            mitigation=world.incident.correct_mitigation,
            mitigation_request_id=approval.request_id,
            approval_decision=approval.decision,
            recovery_checks=[
                f"{check['service']} error rate, p95, and saturation returned to baseline"
                for check in recovery["checks"]
            ],
            customer_impact=("Checkout success fell from 99.4% to 71% in two regions."),
            residual_risk=("Monitor the reverted change and dependent services for recurrence."),
        )
        ledger = EvidenceLedger(case_id=world.incident.case_id, items=evidence)
        score = score_incident(
            world.incident,
            report,
            world.audit_log,
            world.evaluation_state(),
        )
        artifacts = {
            "incident-report.json": report.model_dump(mode="json"),
            "evidence-ledger.json": ledger.model_dump(mode="json"),
            "command-audit.json": [event.model_dump(mode="json") for event in world.audit_log],
            "evaluation-state.json": world.evaluation_state().model_dump(mode="json"),
            "world-manifest.json": world.incident.model_dump(mode="json"),
        }
        for name, artifact in artifacts.items():
            bundle.write_artifact(name, artifact)
            bundle.emit(
                EventKind.ARTIFACT_WRITTEN,
                actor="scripted-oracle",
                payload={
                    "artifact": f"artifacts/{name}",
                    "sha256": _artifact_hash(artifact),
                },
            )
        bundle.write_scores(score.model_dump(mode="json"))
        bundle.emit(EventKind.AGENT_FINISHED, actor="scripted-oracle")
        elapsed = time.monotonic() - started
        bundle.emit(
            EventKind.RUN_FINISHED,
            actor="scripted-oracle",
            payload={
                "status": TerminalStatus.COMPLETED.value,
                "task_success": score.task_success,
                "total_score": score.total_score,
            },
        )
        bundle.write_terminal(
            RunTerminal(
                run_id=manifest.run_id,
                status=TerminalStatus.COMPLETED,
                reason="scripted oracle completed",
                used_model_calls=0,
                used_tool_calls=world.tool_calls,
                used_output_tokens=0,
                elapsed_seconds=elapsed,
            )
        )
        return bundle.path, score
    except Exception as exc:
        elapsed = time.monotonic() - started
        bundle.emit(
            EventKind.ERROR,
            actor="scripted-oracle",
            payload={"error_type": type(exc).__name__, "message": str(exc)},
        )
        bundle.emit(
            EventKind.RUN_FINISHED,
            actor="scripted-oracle",
            payload={"status": TerminalStatus.RUNNER_FAILURE.value},
        )
        bundle.write_terminal(
            RunTerminal(
                run_id=manifest.run_id,
                status=TerminalStatus.RUNNER_FAILURE,
                reason=str(exc),
                used_model_calls=0,
                used_tool_calls=world.tool_calls,
                used_output_tokens=0,
                elapsed_seconds=elapsed,
            )
        )
        raise


def _evidence_item(
    world: IncidentWorld,
    result: dict[str, Any],
    *,
    source_role: str,
    source_tool: str,
    claim: str,
    supports: Literal["root_cause", "mitigation", "recovery"],
) -> EvidenceItem:
    audit = next(
        event for event in reversed(world.audit_log) if event.evidence_id == result["evidence_id"]
    )
    if audit.result_sha256 is None:
        raise RuntimeError("evidence result is missing its audit digest")
    return EvidenceItem(
        evidence_id=result["evidence_id"],
        source_role=source_role,
        source_tool=source_tool,
        claim=claim,
        observation=claim,
        world_tick=result["world_tick"],
        result_sha256=audit.result_sha256,
        supports=supports,
    )


def _source_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _source_tree_sha256() -> str:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        timeout=10,
    )
    digest = hashlib.sha256()
    for raw_path in sorted(filter(None, result.stdout.split(b"\0"))):
        path = Path(raw_path.decode())
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"[MISSING]")
        digest.update(b"\0")
    return digest.hexdigest()


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "source"


def _artifact_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repetition", type=int, default=0)
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument(
        "--experiment",
        type=Path,
        default=Path("configs/experiments/smoke.toml"),
    )
    args = parser.parse_args()
    bundle_path, score = run_oracle(
        seed=args.seed,
        repetition=args.repetition,
        runs_dir=args.runs_dir,
        experiment_path=args.experiment,
    )
    print(
        json.dumps(
            {
                "bundle": str(bundle_path),
                "score": score.total_score,
                "task_success": score.task_success,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
