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
    approval_request = _mapping(execution.get("approval_request"), "execution.approval_request")
    evidence = _evidence_items(evidence_packet, synthesis, world)
    uncertainties = _string_list(
        synthesis.get("uncertainties", []),
        "execution.evidence.synthesis.uncertainties",
        allow_empty=True,
    )
    report = IncidentReport(
        case_id=_required_string(approval_request, "case_id", "execution.approval_request"),
        root_cause=_required_string(synthesis, "root_cause", "execution.evidence.synthesis"),
        confidence=max(0.5, 0.95 - 0.05 * len(uncertainties)),
        evidence=evidence,
        mitigation_action_id=_required_string(execution, "action_id_to_execute", "execution"),
        mitigation=(
            _optional_string(execution.get("action_to_execute"))
            or _required_string(
                synthesis,
                "proposed_mitigation",
                "execution.evidence.synthesis",
            )
        ),
        mitigation_request_id=_required_string(
            approval_request,
            "request_id",
            "execution.approval_request",
        ),
        approval_decision=ApprovalDecision(
            _required_string(execution, "approval_decision", "execution")
        ),
        recovery_checks=_string_list(
            synthesis.get("recovery_checks"),
            "execution.evidence.synthesis.recovery_checks",
        ),
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
    chronology = _string_list(
        synthesis.get("chronology", []),
        "execution.evidence.synthesis.chronology",
        allow_empty=True,
    )
    items: list[EvidenceItem] = []
    findings = evidence_packet.get("findings")
    if not isinstance(findings, list):
        raise ValueError("execution.evidence.findings must be a list")
    for finding_value in findings:
        finding = _mapping(finding_value, "evidence finding")
        role = _required_string(finding, "role", "evidence finding")
        claims = _string_list(
            finding.get("claims", []),
            "evidence finding.claims",
            allow_empty=True,
        )
        supports: Literal["root_cause", "mitigation", "recovery"] = (
            "mitigation" if role == "reliability_reviewer" else "root_cause"
        )
        evidence_ids = _string_list(
            finding.get("evidence_ids"),
            "evidence finding.evidence_ids",
        )
        for index, evidence_id in enumerate(evidence_ids):
            audit = audit_by_evidence.get(evidence_id)
            if audit is None or audit.result_sha256 is None:
                raise ValueError(f"workflow evidence lacks audit provenance: {evidence_id}")
            claim = (
                claims[min(index, len(claims) - 1)]
                if claims
                else _required_string(finding, "summary", "evidence finding")
            )
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


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    return value
