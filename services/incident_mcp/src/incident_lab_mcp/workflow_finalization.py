"""Build frozen scorer artifacts after a native workflow has terminated."""

from __future__ import annotations

import json
from typing import Any, Literal

from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import (
    ApprovalDecision,
    EvidenceItem,
    EvidenceLedger,
    IncidentReport,
)
from agent_lab.evaluator import score_incident
from agent_lab.run_bundle import RunBundle
from agent_lab.runner_protocol import FinalizationResult, FinalizedArtifact, RunRequest
from incident_lab_mcp.world import IncidentWorld


async def finalize_workflow_bundle(
    request: RunRequest,
    world: IncidentWorld,
) -> FinalizationResult:
    """Join public workflow output with terminal-only world evidence and score it."""

    state_path = request.paths.state / "workflow-execution.json"
    result = json.loads(state_path.read_text(encoding="utf-8"))
    execution = _mapping(result.get("execution"), "execution")
    evidence_packet = _mapping(execution.get("evidence"), "execution.evidence")
    synthesis = _mapping(evidence_packet.get("synthesis"), "execution.evidence.synthesis")
    evidence = _evidence_items(evidence_packet, synthesis, world)
    uncertainties = [
        str(value)
        for value in synthesis.get("uncertainties", [])
        if isinstance(value, str) and value.strip()
    ]
    report = IncidentReport(
        case_id=str(_mapping(execution["approval_request"], "approval_request")["case_id"]),
        root_cause=str(synthesis["root_cause"]),
        confidence=max(0.5, 0.95 - 0.05 * len(uncertainties)),
        evidence=evidence,
        mitigation_action_id=str(execution["action_id_to_execute"]),
        mitigation=str(execution.get("action_to_execute") or synthesis["proposed_mitigation"]),
        mitigation_request_id=str(
            _mapping(execution["approval_request"], "approval_request")["request_id"]
        ),
        approval_decision=ApprovalDecision(str(execution["approval_decision"])),
        recovery_checks=[str(value) for value in synthesis.get("recovery_checks", [])],
        customer_impact=request.public_ticket,
        residual_risk=(
            "; ".join(uncertainties)
            if uncertainties
            else "Continue monitoring the reverted service and dependent systems for recurrence."
        ),
    )
    ledger = EvidenceLedger(case_id=report.case_id, items=evidence)
    world_state = world.evaluation_state()
    score = score_incident(world.incident, report, world.audit_log, world_state)
    artifacts: dict[str, Any] = {
        "incident-report.json": report,
        "evidence-ledger.json": ledger,
        "command-audit.json": [event.model_dump(mode="json") for event in world.audit_log],
        "evaluation-state.json": world_state,
        "world-manifest.json": world.incident,
    }
    bundle = RunBundle(request.paths.bundle, request.manifest)
    finalized: list[FinalizedArtifact] = []
    for name, artifact in artifacts.items():
        bundle.write_artifact(name, artifact)
        body = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else artifact
        finalized.append(
            FinalizedArtifact(
                path=f"artifacts/{name}",
                sha256=sha256_payload(body),
            )
        )
    bundle.write_scores(score.model_dump(mode="json"))
    return FinalizationResult(
        artifacts=finalized,
        task_success=score.task_success,
        total_score=score.total_score,
    )


def _evidence_items(
    evidence_packet: dict[str, Any],
    synthesis: dict[str, Any],
    world: IncidentWorld,
) -> list[EvidenceItem]:
    audit_by_evidence = {
        event.evidence_id: event
        for event in world.audit_log
        if event.outcome == "succeeded" and event.evidence_id is not None
    }
    chronology = [str(value) for value in synthesis.get("chronology", [])]
    items: list[EvidenceItem] = []
    for finding_value in evidence_packet.get("findings", []):
        finding = _mapping(finding_value, "evidence finding")
        role = str(finding["role"])
        claims = [str(value) for value in finding.get("claims", [])]
        supports: Literal["root_cause", "mitigation", "recovery"] = (
            "mitigation" if role == "reliability_reviewer" else "root_cause"
        )
        for index, evidence_id_value in enumerate(finding.get("evidence_ids", [])):
            evidence_id = str(evidence_id_value)
            audit = audit_by_evidence.get(evidence_id)
            if audit is None or audit.result_sha256 is None:
                raise ValueError(f"workflow evidence lacks audit provenance: {evidence_id}")
            claim = claims[min(index, len(claims) - 1)] if claims else str(finding["summary"])
            chronology_item = chronology[index % len(chronology)] if chronology else claim
            items.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    source_role=role,
                    source_tool=audit.operation,
                    claim=claim,
                    observation=f"{claim}; chronology: {chronology_item}",
                    world_tick=audit.world_tick,
                    result_sha256=audit.result_sha256,
                    supports=supports,
                )
            )
    if len(items) < 2:
        raise ValueError("workflow finalization requires at least two evidence items")
    return items


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value
