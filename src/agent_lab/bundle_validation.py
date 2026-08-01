"""Fail-closed validation for individual and paired run bundles."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_lab.contract_bundle import canonical_json
from agent_lab.contracts import (
    SCHEMA_VERSION,
    EventKind,
    EvidenceLedger,
    FrameworkLane,
    IncidentReport,
    NormalizedEvent,
    RunManifest,
    RunTerminal,
    TerminalStatus,
    ToolAuditEvent,
    WorldEvaluationState,
)
from agent_lab.evaluator import DeterministicScore
from agent_lab.scenario import IncidentCase
from agent_lab.trace_contracts import (
    NativeSpanRecord,
    NativeTraceLink,
    NativeTraceProfileRecord,
    TraceCollectorStatus,
    compute_trace_completeness,
)


class FindingLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: FindingLevel
    code: str
    message: str
    location: str | None = None


class BundleValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    bundle_path: str
    run_id: UUID | None = None
    valid: bool
    inference_status: Literal["eligible", "invalid_provenance"]
    findings: list[ValidationFinding]


class PairValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.1.0"] = SCHEMA_VERSION
    left_bundle: str
    right_bundle: str
    valid: bool
    inference_status: Literal["eligible", "invalid_provenance"]
    track: Literal["explicit_workflow", "opinionated_harness"] | None = None
    findings: list[ValidationFinding]
    left: BundleValidationResult
    right: BundleValidationResult


def validate_bundle(path: Path, *, mark: bool = False) -> BundleValidationResult:
    """Validate one bundle without modifying evidence files."""

    path = path.resolve()
    findings: list[ValidationFinding] = []
    manifest = _load_manifest(path, findings)
    _validate_manifest_digest(path, findings)
    events = _load_events(path, findings)
    terminal = _load_terminal(path, findings)

    if manifest is not None:
        _validate_event_stream(manifest, events, findings)
    if manifest is not None and terminal is not None:
        _validate_terminal(manifest, terminal, events, findings)
    _validate_artifacts(path, events, terminal, findings)
    if manifest is not None and terminal is not None:
        _validate_scorer_inputs(path, manifest, terminal, findings)
    if manifest is not None:
        _validate_native_trace(path, manifest, events, findings)

    valid = not any(finding.level is FindingLevel.ERROR for finding in findings)
    result = BundleValidationResult(
        bundle_path=str(path),
        run_id=manifest.run_id if manifest is not None else None,
        valid=valid,
        inference_status="eligible" if valid else "invalid_provenance",
        findings=findings,
    )
    if mark:
        _write_validation_sidecar(path, result)
    return result


def validate_pair(
    left_path: Path,
    right_path: Path,
    *,
    mark: bool = False,
) -> PairValidationResult:
    """Validate two bundles and every input required to be held constant."""

    left = validate_bundle(left_path, mark=mark)
    right = validate_bundle(right_path, mark=mark)
    findings: list[ValidationFinding] = []
    if not left.valid:
        _error(findings, "left_bundle_invalid", "Left bundle failed individual validation.")
    if not right.valid:
        _error(findings, "right_bundle_invalid", "Right bundle failed individual validation.")

    left_manifest = _parse_manifest_file(Path(left.bundle_path))
    right_manifest = _parse_manifest_file(Path(right.bundle_path))
    track: Literal["explicit_workflow", "opinionated_harness"] | None = None
    if left_manifest is not None and right_manifest is not None:
        track = _validate_pair_manifests(left_manifest, right_manifest, findings)
        _validate_pair_trace_profiles(
            Path(left.bundle_path),
            Path(right.bundle_path),
            findings,
        )

    valid = not any(finding.level is FindingLevel.ERROR for finding in findings)
    result = PairValidationResult(
        left_bundle=left.bundle_path,
        right_bundle=right.bundle_path,
        valid=valid,
        inference_status="eligible" if valid else "invalid_provenance",
        track=track,
        findings=findings,
        left=left,
        right=right,
    )
    if mark:
        _write_pair_sidecars(result)
    return result


def render_validation(result: BundleValidationResult | PairValidationResult) -> str:
    """Render a compact human-readable validation report."""

    status = "VALID" if result.valid else "INVALID_PROVENANCE"
    if isinstance(result, BundleValidationResult):
        lines = [f"{status}: {result.bundle_path}"]
    else:
        track = result.track or "unmatched"
        lines = [
            f"{status}: {result.left_bundle}",
            f"             {result.right_bundle}",
            f"track: {track}",
        ]
    if not result.findings:
        lines.append("No validation findings.")
    for finding in result.findings:
        location = f" [{finding.location}]" if finding.location else ""
        lines.append(f"{finding.level.value.upper()} {finding.code}{location}: {finding.message}")
    return "\n".join(lines)


def _load_manifest(
    path: Path,
    findings: list[ValidationFinding],
) -> RunManifest | None:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        _error(findings, "manifest_missing", "manifest.json is missing.", "manifest.json")
        return None
    try:
        return RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        _error(
            findings,
            "manifest_invalid",
            f"manifest.json does not match the frozen schema: {exc}",
            "manifest.json",
        )
        return None


def _parse_manifest_file(path: Path) -> RunManifest | None:
    try:
        return RunManifest.model_validate_json((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


def _validate_manifest_digest(
    path: Path,
    findings: list[ValidationFinding],
) -> None:
    manifest_path = path / "manifest.json"
    digest_path = path / "manifest.sha256"
    if not digest_path.is_file():
        _error(
            findings,
            "manifest_digest_missing",
            "manifest.sha256 is missing.",
            "manifest.sha256",
        )
        return
    try:
        expected = digest_path.read_text(encoding="utf-8").strip()
        actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError as exc:
        _error(
            findings,
            "manifest_digest_unreadable",
            str(exc),
            "manifest.sha256",
        )
        return
    if re.fullmatch(r"[a-f0-9]{64}", expected) is None:
        _error(
            findings,
            "manifest_digest_invalid",
            "manifest.sha256 must contain one lowercase SHA-256 digest.",
            "manifest.sha256",
        )
    elif expected != actual:
        _error(
            findings,
            "manifest_digest_mismatch",
            f"Expected {expected}, observed {actual}.",
            "manifest.json",
        )


def _load_events(
    path: Path,
    findings: list[ValidationFinding],
) -> list[NormalizedEvent]:
    event_path = path / "events.jsonl"
    if not event_path.is_file():
        _error(findings, "events_missing", "events.jsonl is missing.", "events.jsonl")
        return []
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _error(findings, "events_unreadable", str(exc), "events.jsonl")
        return []
    if not lines:
        _error(findings, "events_empty", "events.jsonl contains no events.", "events.jsonl")
        return []

    events: list[NormalizedEvent] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            _error(
                findings,
                "event_blank_line",
                "Blank event lines are not permitted.",
                f"events.jsonl:{line_number}",
            )
            continue
        try:
            events.append(NormalizedEvent.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            _error(
                findings,
                "event_invalid",
                str(exc),
                f"events.jsonl:{line_number}",
            )
    return events


def _load_terminal(
    path: Path,
    findings: list[ValidationFinding],
) -> RunTerminal | None:
    terminal_path = path / "terminal.json"
    if not terminal_path.is_file():
        _error(
            findings,
            "terminal_missing",
            "terminal.json is missing.",
            "terminal.json",
        )
        return None
    try:
        return RunTerminal.model_validate_json(terminal_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        _error(
            findings,
            "terminal_invalid",
            f"terminal.json does not match the frozen schema: {exc}",
            "terminal.json",
        )
        return None


def _validate_event_stream(
    manifest: RunManifest,
    events: list[NormalizedEvent],
    findings: list[ValidationFinding],
) -> None:
    sequences = [event.sequence for event in events]
    if sequences != list(range(len(events))):
        _error(
            findings,
            "event_sequence_noncontiguous",
            f"Observed sequences {sequences}; expected 0..{max(len(events) - 1, 0)}.",
            "events.jsonl",
        )
    for event in events:
        if event.run_id != manifest.run_id:
            _error(
                findings,
                "event_run_mismatch",
                f"Sequence {event.sequence} has run_id {event.run_id}.",
                "events.jsonl",
            )
        if event.lane is not manifest.lane:
            _error(
                findings,
                "event_lane_mismatch",
                f"Sequence {event.sequence} has lane {event.lane.value}.",
                "events.jsonl",
            )

    starts = [event for event in events if event.kind is EventKind.RUN_STARTED]
    finishes = [event for event in events if event.kind is EventKind.RUN_FINISHED]
    if len(starts) != 1:
        _error(
            findings,
            "run_start_count",
            f"Expected one run.started event, observed {len(starts)}.",
            "events.jsonl",
        )
    elif starts[0].sequence != 0:
        _error(
            findings,
            "run_start_not_first",
            "run.started must be sequence 0.",
            "events.jsonl",
        )
    if len(finishes) != 1:
        _error(
            findings,
            "run_finish_count",
            f"Expected one run.finished event, observed {len(finishes)}.",
            "events.jsonl",
        )
    elif events and finishes[0].sequence != events[-1].sequence:
        _error(
            findings,
            "run_finish_not_last",
            "run.finished must be the final normalized event.",
            "events.jsonl",
        )

    seen_event_ids = set()
    for event in events:
        if event.parent_event_id is not None and event.parent_event_id not in seen_event_ids:
            _error(
                findings,
                "event_parent_not_prior",
                f"Sequence {event.sequence} references a missing or future parent.",
                "events.jsonl",
            )
        seen_event_ids.add(event.event_id)


def _validate_terminal(
    manifest: RunManifest,
    terminal: RunTerminal,
    events: list[NormalizedEvent],
    findings: list[ValidationFinding],
) -> None:
    if terminal.run_id != manifest.run_id:
        _error(
            findings,
            "terminal_run_mismatch",
            f"terminal run_id {terminal.run_id} differs from manifest {manifest.run_id}.",
            "terminal.json",
        )

    model_calls = sum(event.kind is EventKind.MODEL_REQUESTED for event in events)
    tool_calls = sum(event.kind is EventKind.TOOL_REQUESTED for event in events)
    output_tokens = 0
    for event in events:
        if event.kind is not EventKind.MODEL_RESPONDED:
            continue
        value = event.payload.get("output_tokens", 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _error(
                findings,
                "model_output_tokens_invalid",
                f"Sequence {event.sequence} has invalid output_tokens {value!r}.",
                "events.jsonl",
            )
        else:
            output_tokens += value

    observed_usage = {
        "used_model_calls": model_calls,
        "used_tool_calls": tool_calls,
        "used_output_tokens": output_tokens,
    }
    for field_name, observed in observed_usage.items():
        recorded = getattr(terminal, field_name)
        if recorded != observed:
            _error(
                findings,
                "terminal_usage_mismatch",
                f"{field_name} records {recorded}, events derive {observed}.",
                "terminal.json",
            )

    budget_usage = {
        "model_calls": terminal.used_model_calls,
        "tool_calls": terminal.used_tool_calls,
        "output_tokens": terminal.used_output_tokens,
        "wall_clock_seconds": terminal.elapsed_seconds,
        "approval_requests": sum(event.kind is EventKind.APPROVAL_REQUESTED for event in events),
    }
    for field_name, used in budget_usage.items():
        limit = getattr(manifest.budget, field_name)
        if used > limit and terminal.status is not TerminalStatus.BUDGET_EXHAUSTED:
            _error(
                findings,
                "budget_exceeded_without_status",
                f"{field_name} used {used}, limit {limit}.",
                "terminal.json",
            )

    finishes = [event for event in events if event.kind is EventKind.RUN_FINISHED]
    if len(finishes) == 1:
        event_status = finishes[0].payload.get("status")
        if event_status != terminal.status.value:
            _error(
                findings,
                "terminal_event_status_mismatch",
                f"run.finished reports {event_status!r}, terminal reports "
                f"{terminal.status.value!r}.",
                "events.jsonl",
            )


def _validate_artifacts(
    path: Path,
    events: list[NormalizedEvent],
    terminal: RunTerminal | None,
    findings: list[ValidationFinding],
) -> None:
    artifact_root = path / "artifacts"
    if not artifact_root.is_dir():
        _error(
            findings,
            "artifact_directory_missing",
            "artifacts/ is missing.",
            "artifacts",
        )
        return

    declared: dict[str, str] = {}
    for event in events:
        if event.kind is not EventKind.ARTIFACT_WRITTEN:
            continue
        relative = event.payload.get("artifact")
        digest = event.payload.get("sha256")
        if not isinstance(relative, str) or not relative.startswith("artifacts/"):
            _error(
                findings,
                "artifact_event_path_invalid",
                f"Sequence {event.sequence} has invalid artifact path {relative!r}.",
                "events.jsonl",
            )
            continue
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            _error(
                findings,
                "artifact_event_digest_invalid",
                f"Sequence {event.sequence} has invalid SHA-256 {digest!r}.",
                "events.jsonl",
            )
            continue
        if relative in declared:
            _error(
                findings,
                "artifact_event_duplicate",
                f"{relative} has multiple artifact.written events.",
                "events.jsonl",
            )
        declared[relative] = digest

    actual_files = {
        str(file.relative_to(path)) for file in artifact_root.rglob("*") if file.is_file()
    }
    for relative, expected_digest in declared.items():
        destination = (path / relative).resolve()
        if path not in destination.parents or not destination.is_file():
            _error(
                findings,
                "artifact_missing",
                f"Declared artifact {relative} is missing or outside the bundle.",
                relative,
            )
            continue
        try:
            actual_digest = _artifact_digest(destination)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _error(
                findings,
                "artifact_unreadable",
                str(exc),
                relative,
            )
            continue
        if actual_digest != expected_digest:
            _error(
                findings,
                "artifact_digest_mismatch",
                f"Expected {expected_digest}, observed {actual_digest}.",
                relative,
            )

    undeclared = sorted(actual_files - set(declared))
    if undeclared:
        _error(
            findings,
            "artifact_event_missing",
            f"Artifacts have no artifact.written event: {undeclared}.",
            "artifacts",
        )
    if terminal is not None and terminal.status is TerminalStatus.COMPLETED:
        required = {
            "artifacts/incident-report.json",
            "artifacts/evidence-ledger.json",
            "artifacts/command-audit.json",
            "artifacts/evaluation-state.json",
            "artifacts/world-manifest.json",
        }
        missing = sorted(required - actual_files)
        if missing:
            _error(
                findings,
                "completed_artifacts_missing",
                f"Completed bundle lacks required artifacts: {missing}.",
                "artifacts",
            )


def _validate_scorer_inputs(
    path: Path,
    manifest: RunManifest,
    terminal: RunTerminal,
    findings: list[ValidationFinding],
) -> None:
    if terminal.status is not TerminalStatus.COMPLETED:
        return
    scores_path = path / "scores.json"
    if not scores_path.is_file():
        _error(
            findings,
            "scores_missing",
            "Completed bundle is missing scores.json.",
            "scores.json",
        )
        return

    parsed: dict[str, Any] = {}
    models: dict[str, type[BaseModel]] = {
        "incident-report.json": IncidentReport,
        "evidence-ledger.json": EvidenceLedger,
        "evaluation-state.json": WorldEvaluationState,
        "world-manifest.json": IncidentCase,
    }
    for name, model in models.items():
        artifact_path = path / "artifacts" / name
        if not artifact_path.is_file():
            continue
        try:
            parsed[name] = model.model_validate_json(artifact_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            _error(
                findings,
                "scorer_artifact_invalid",
                f"{name}: {exc}",
                f"artifacts/{name}",
            )

    audit_path = path / "artifacts" / "command-audit.json"
    if audit_path.is_file():
        try:
            audit_raw = json.loads(audit_path.read_text(encoding="utf-8"))
            if not isinstance(audit_raw, list):
                raise ValueError("command audit must be a JSON array")
            parsed["command-audit.json"] = [
                ToolAuditEvent.model_validate(item) for item in audit_raw
            ]
        except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
            _error(
                findings,
                "command_audit_invalid",
                str(exc),
                "artifacts/command-audit.json",
            )

    try:
        score = DeterministicScore.model_validate_json(scores_path.read_text(encoding="utf-8"))
        parsed["scores.json"] = score
    except (OSError, ValidationError, ValueError) as exc:
        _error(
            findings,
            "scores_invalid",
            str(exc),
            "scores.json",
        )

    case_ids = {value.case_id for value in parsed.values() if hasattr(value, "case_id")}
    if len(case_ids) > 1:
        _error(
            findings,
            "scorer_case_mismatch",
            f"Scorer inputs contain different case identifiers: {sorted(case_ids)}.",
            "artifacts",
        )

    report = parsed.get("incident-report.json")
    ledger = parsed.get("evidence-ledger.json")
    if (
        isinstance(report, IncidentReport)
        and isinstance(ledger, EvidenceLedger)
        and report.evidence != ledger.items
    ):
        _error(
            findings,
            "evidence_ledger_mismatch",
            "Incident report evidence differs from evidence-ledger.json.",
            "artifacts/evidence-ledger.json",
        )

    world_manifest = parsed.get("world-manifest.json")
    if isinstance(world_manifest, IncidentCase):
        observed_digest = hashlib.sha256(
            canonical_json(world_manifest.model_dump(mode="json"))
        ).hexdigest()
        if observed_digest != manifest.world_manifest_sha256:
            _error(
                findings,
                "world_manifest_digest_mismatch",
                f"Expected {manifest.world_manifest_sha256}, observed {observed_digest}.",
                "artifacts/world-manifest.json",
            )


def _validate_native_trace(
    path: Path,
    manifest: RunManifest,
    events: list[NormalizedEvent],
    findings: list[ValidationFinding],
) -> None:
    trace_profile = _load_native_trace_profile(path, manifest, findings)
    link_path = path / "native" / "trace-link.json"
    if not link_path.is_file():
        _warning(
            findings,
            "native_trace_missing",
            "No native trace-link sidecar is present; trace coverage is unavailable.",
            "native/trace-link.json",
        )
        return
    try:
        link = NativeTraceLink.model_validate_json(link_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        _error(
            findings,
            "native_trace_link_invalid",
            str(exc),
            "native/trace-link.json",
        )
        return

    if trace_profile is None:
        _error(
            findings,
            "native_trace_profile_missing",
            "A trace-link requires its immutable tracing-profile sidecar.",
            "native/tracing-profile.json",
        )
        return
    expected_profile = trace_profile.tracing
    identity_checks = {
        "run_id": (link.run_id, manifest.run_id),
        "lane": (link.lane, manifest.lane),
        "enabled": (link.enabled, expected_profile.enabled),
        "capture_sensitive": (
            link.capture_sensitive,
            trace_profile.capture_sensitive,
        ),
        "capture_reasoning": (
            link.capture_reasoning,
            trace_profile.capture_reasoning,
        ),
        "collector_endpoint": (
            link.collector_endpoint,
            expected_profile.collector_endpoint if expected_profile.enabled else None,
        ),
        "phoenix_base_url": (
            link.phoenix_base_url,
            expected_profile.phoenix_base_url if expected_profile.enabled else None,
        ),
        "project_name": (
            link.project_name,
            f"agent-lab-{manifest.lane.value}" if expected_profile.enabled else None,
        ),
    }
    for field_name, (observed, expected) in identity_checks.items():
        if observed != expected:
            _error(
                findings,
                "native_trace_profile_mismatch",
                f"Trace-link {field_name} is {observed!r}; tracing profile records {expected!r}.",
                "native/trace-link.json",
            )

    spans: list[NativeSpanRecord] = []
    expected_ledger_path = "native/traces/spans.jsonl"
    if link.span_ledger_path is not None:
        if link.span_ledger_path != expected_ledger_path:
            _error(
                findings,
                "native_span_ledger_path_invalid",
                f"Span ledger must be {expected_ledger_path}.",
                "native/trace-link.json",
            )
        else:
            ledger_path = path / expected_ledger_path
            try:
                ledger_bytes = ledger_path.read_bytes()
                observed_digest = hashlib.sha256(ledger_bytes).hexdigest()
                if observed_digest != link.span_ledger_sha256:
                    _error(
                        findings,
                        "native_span_ledger_digest_mismatch",
                        f"Expected {link.span_ledger_sha256}, observed {observed_digest}.",
                        expected_ledger_path,
                    )
                for line_number, line in enumerate(ledger_bytes.splitlines(), start=1):
                    if not line.strip():
                        raise ValueError(f"blank line at {line_number}")
                    spans.append(NativeSpanRecord.model_validate_json(line))
            except (OSError, ValidationError, ValueError) as exc:
                _error(
                    findings,
                    "native_span_ledger_invalid",
                    str(exc),
                    expected_ledger_path,
                )
    if len(spans) != link.span_count:
        _error(
            findings,
            "native_span_count_mismatch",
            f"Trace-link records {link.span_count} spans; ledger contains {len(spans)}.",
            "native/trace-link.json",
        )
    for span in spans:
        if span.run_id != manifest.run_id or span.lane is not manifest.lane:
            _error(
                findings,
                "native_span_identity_mismatch",
                f"Span {span.span_id} has a different run or lane identity.",
                expected_ledger_path,
            )
    observed_trace_ids = sorted({span.trace_id for span in spans})
    if observed_trace_ids != sorted(link.trace_ids):
        _error(
            findings,
            "native_trace_ids_mismatch",
            f"Trace-link IDs {link.trace_ids} differ from ledger IDs {observed_trace_ids}.",
            "native/trace-link.json",
        )
    span_contexts = {(span.trace_id, span.span_id) for span in spans}
    if len(span_contexts) != len(spans):
        _error(
            findings,
            "native_span_duplicate",
            "Native span ledger repeats a trace/span identity.",
            expected_ledger_path,
        )
    if link.primary_trace_id is not None and link.phoenix_base_url is not None:
        expected_trace_url = (
            f"{link.phoenix_base_url.rstrip('/')}/redirects/traces/{link.primary_trace_id}"
        )
        if link.phoenix_trace_url != expected_trace_url:
            _error(
                findings,
                "native_trace_url_mismatch",
                f"Expected Phoenix deep link {expected_trace_url!r}.",
                "native/trace-link.json",
            )

    for event in events:
        if (event.trace_id is None) != (event.span_id is None):
            _error(
                findings,
                "event_trace_context_incomplete",
                f"Sequence {event.sequence} has only part of a trace context.",
                "events.jsonl",
            )
        elif event.trace_id is not None and (event.trace_id, event.span_id) not in span_contexts:
            _error(
                findings,
                "event_trace_context_missing",
                f"Sequence {event.sequence} references a span absent from the native ledger.",
                "events.jsonl",
            )

    recomputed = compute_trace_completeness(events, spans)
    if recomputed != link.completeness:
        _error(
            findings,
            "native_trace_completeness_mismatch",
            "Recorded trace completeness does not match normalized events and native spans.",
            "native/trace-link.json",
        )
    if link.collector_status in {
        TraceCollectorStatus.UNAVAILABLE,
        TraceCollectorStatus.EXPORT_FAILED,
    }:
        _warning(
            findings,
            "native_trace_collector_degraded",
            f"Phoenix collector status is {link.collector_status.value}; local evidence remains.",
            "native/trace-link.json",
        )
    for diagnostic in link.diagnostics:
        _warning(
            findings,
            "native_trace_diagnostic",
            diagnostic,
            "native/trace-link.json",
        )


def _load_native_trace_profile(
    path: Path,
    manifest: RunManifest,
    findings: list[ValidationFinding],
) -> NativeTraceProfileRecord | None:
    profile_path = path / "native" / "tracing-profile.json"
    digest_path = path / "native" / "tracing-profile.sha256"
    if not profile_path.exists() and not digest_path.exists():
        return None
    if not profile_path.is_file() or not digest_path.is_file():
        _error(
            findings,
            "native_trace_profile_incomplete",
            "Tracing profile and digest must both be present.",
            "native/tracing-profile.json",
        )
        return None
    try:
        profile_bytes = profile_path.read_bytes()
        expected_digest = digest_path.read_text(encoding="utf-8").strip()
        observed_digest = hashlib.sha256(profile_bytes).hexdigest()
        if re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None:
            raise ValueError("tracing-profile.sha256 is not a lowercase SHA-256 digest")
        if expected_digest != observed_digest:
            _error(
                findings,
                "native_trace_profile_digest_mismatch",
                f"Expected {expected_digest}, observed {observed_digest}.",
                "native/tracing-profile.json",
            )
        profile = NativeTraceProfileRecord.model_validate_json(profile_bytes)
    except (OSError, ValidationError, ValueError) as exc:
        _error(
            findings,
            "native_trace_profile_invalid",
            str(exc),
            "native/tracing-profile.json",
        )
        return None
    if profile.run_id != manifest.run_id:
        _error(
            findings,
            "native_trace_profile_run_mismatch",
            f"Tracing profile run_id {profile.run_id} differs from manifest {manifest.run_id}.",
            "native/tracing-profile.json",
        )
    return profile


def _validate_pair_manifests(
    left: RunManifest,
    right: RunManifest,
    findings: list[ValidationFinding],
) -> Literal["explicit_workflow", "opinionated_harness"] | None:
    track = _pair_track(left.lane, right.lane)
    if track is None:
        _error(
            findings,
            "pair_lane_mismatch",
            f"{left.lane.value} and {right.lane.value} are not matched comparison lanes.",
            "manifest.json",
        )
    if left.run_id == right.run_id:
        _error(
            findings,
            "pair_run_reused",
            "Paired bundles must have distinct run identifiers.",
            "manifest.json",
        )

    held_constant: dict[str, tuple[Any, Any]] = {
        "experiment_id": (left.experiment_id, right.experiment_id),
        "scenario": (left.scenario, right.scenario),
        "scenario_generator_version": (
            left.scenario_generator_version,
            right.scenario_generator_version,
        ),
        "scenario_seed": (left.scenario_seed, right.scenario_seed),
        "repetition": (left.repetition, right.repetition),
        "model": (left.model.model_dump(mode="json"), right.model.model_dump(mode="json")),
        "budget": (
            left.budget.model_dump(mode="json"),
            right.budget.model_dump(mode="json"),
        ),
        "source_commit": (left.source_commit, right.source_commit),
        "source_tree_sha256": (
            left.source_tree_sha256,
            right.source_tree_sha256,
        ),
        "prompt_bundle_sha256": (
            left.prompt_bundle_sha256,
            right.prompt_bundle_sha256,
        ),
        "tool_catalog_sha256": (
            left.tool_catalog_sha256,
            right.tool_catalog_sha256,
        ),
        "world_manifest_sha256": (
            left.world_manifest_sha256,
            right.world_manifest_sha256,
        ),
    }
    for field_name, (left_value, right_value) in held_constant.items():
        if canonical_json(left_value) != canonical_json(right_value):
            _error(
                findings,
                "pair_provenance_drift",
                f"Held-constant field {field_name} differs within the pair.",
                "manifest.json",
            )

    # Dependency locks are intentionally lane-specific. Schema validation above
    # proves both immutable digests are present; equality would be invalid for
    # the process-isolated framework environments.
    return track


def _validate_pair_trace_profiles(
    left_path: Path,
    right_path: Path,
    findings: list[ValidationFinding],
) -> None:
    left = _parse_trace_profile_file(left_path)
    right = _parse_trace_profile_file(right_path)
    if left is None and right is None:
        return
    if left is None or right is None:
        _error(
            findings,
            "pair_trace_profile_missing",
            "Both paired bundles must record the tracing profile when either one does.",
            "native/tracing-profile.json",
        )
        return
    left_payload = left.model_dump(mode="json", exclude={"run_id"})
    right_payload = right.model_dump(mode="json", exclude={"run_id"})
    if canonical_json(left_payload) != canonical_json(right_payload):
        _error(
            findings,
            "pair_provenance_drift",
            "Held-constant field tracing_profile differs within the pair.",
            "native/tracing-profile.json",
        )


def _parse_trace_profile_file(path: Path) -> NativeTraceProfileRecord | None:
    try:
        return NativeTraceProfileRecord.model_validate_json(
            (path / "native" / "tracing-profile.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError):
        return None


def _pair_track(
    left: FrameworkLane,
    right: FrameworkLane,
) -> Literal["explicit_workflow", "opinionated_harness"] | None:
    lanes = {left, right}
    if lanes == {FrameworkLane.MAF_WORKFLOW, FrameworkLane.LANGGRAPH}:
        return "explicit_workflow"
    if lanes == {FrameworkLane.MAF_HARNESS, FrameworkLane.DEEP_AGENTS}:
        return "opinionated_harness"
    return None


def _artifact_digest(path: Path) -> str:
    if path.suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return hashlib.sha256(canonical_json(value)).hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_validation_sidecar(
    path: Path,
    result: BundleValidationResult,
) -> None:
    destination = path / "validation.json"
    temporary = path / ".validation.json.tmp"
    temporary.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _write_pair_sidecars(result: PairValidationResult) -> None:
    for raw_path, peer in (
        (result.left_bundle, result.right_bundle),
        (result.right_bundle, result.left_bundle),
    ):
        path = Path(raw_path)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "peer_bundle": peer,
            "valid": result.valid,
            "inference_status": result.inference_status,
            "track": result.track,
            "findings": [finding.model_dump(mode="json") for finding in result.findings],
        }
        destination = path / "pair-validation.json"
        temporary = path / ".pair-validation.json.tmp"
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)


def _error(
    findings: list[ValidationFinding],
    code: str,
    message: str,
    location: str | None = None,
) -> None:
    findings.append(
        ValidationFinding(
            level=FindingLevel.ERROR,
            code=code,
            message=message,
            location=location,
        )
    )


def _warning(
    findings: list[ValidationFinding],
    code: str,
    message: str,
    location: str | None = None,
) -> None:
    findings.append(
        ValidationFinding(
            level=FindingLevel.WARNING,
            code=code,
            message=message,
            location=location,
        )
    )
