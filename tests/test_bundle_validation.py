import hashlib
import json
from pathlib import Path

from incident_lab_mcp.oracle import run_oracle
from typer.testing import CliRunner

from agent_lab.bundle_validation import validate_bundle, validate_pair
from agent_lab.cli import app
from agent_lab.contract_bundle import canonical_json
from agent_lab.contracts import (
    Budget,
    EventKind,
    FrameworkLane,
    ModelProfile,
    NativeTracingProfile,
    NormalizedEvent,
    RunManifest,
    RunTerminal,
    TerminalStatus,
)
from agent_lab.run_bundle import RunBundle
from agent_lab.trace_contracts import (
    NativeSpanRecord,
    NativeTraceLink,
    NativeTraceProfileRecord,
    TraceCollectorStatus,
    TraceOperation,
    compute_trace_completeness,
)


def _oracle_bundle(tmp_path: Path) -> Path:
    bundle_path, _score = run_oracle(
        seed=42,
        runs_dir=tmp_path,
        experiment_path=Path("configs/experiments/smoke.toml"),
    )
    return bundle_path


def _paired_manifest(lane: FrameworkLane) -> RunManifest:
    return RunManifest(
        experiment_id="pair_fixture",
        lane=lane,
        scenario="incident_response",
        scenario_generator_version="0.1.0",
        scenario_seed=42,
        repetition=0,
        model=ModelProfile(
            name="local-test",
            base_url="http://127.0.0.1:8000/v1",
            temperature=0.6,
            top_p=0.95,
            max_output_tokens=1000,
            context_window_tokens=8192,
            request_timeout_seconds=60,
        ),
        budget=Budget(
            wall_clock_seconds=300,
            model_calls=10,
            tool_calls=20,
            output_tokens=5000,
            approval_requests=1,
        ),
        source_commit="0" * 40,
        source_tree_sha256="c" * 64,
        framework_versions={lane.value: "test"},
        dependency_lock_sha256=("a" if lane is FrameworkLane.MAF_WORKFLOW else "b") * 64,
        prompt_bundle_sha256="1" * 64,
        tool_catalog_sha256="2" * 64,
        world_manifest_sha256="3" * 64,
    )


def _cancelled_bundle(root: Path, lane: FrameworkLane) -> Path:
    manifest = _paired_manifest(lane)
    bundle = RunBundle.create(root, manifest)
    bundle.emit(EventKind.RUN_STARTED)
    bundle.emit(
        EventKind.RUN_FINISHED,
        payload={"status": TerminalStatus.CANCELLED.value},
    )
    bundle.write_terminal(
        RunTerminal(
            run_id=manifest.run_id,
            status=TerminalStatus.CANCELLED,
            reason="pair fixture",
            used_model_calls=0,
            used_tool_calls=0,
            used_output_tokens=0,
            elapsed_seconds=0.1,
        )
    )
    return bundle.path


def _incomplete_operational_bundle(
    root: Path,
    lane: FrameworkLane,
    status: TerminalStatus,
) -> Path:
    manifest = _paired_manifest(lane)
    bundle = RunBundle.create(root, manifest)
    bundle.emit(EventKind.RUN_STARTED)
    bundle.write_terminal(
        RunTerminal(
            run_id=manifest.run_id,
            status=status,
            reason=f"test_{status.value}",
            used_model_calls=0,
            used_tool_calls=0,
            used_output_tokens=0,
            elapsed_seconds=0.1,
            event_stream_complete=False,
        )
    )
    return bundle.path


def _rewrite_manifest(bundle_path: Path, body: dict[str, object]) -> None:
    manifest_bytes = (json.dumps(body, indent=2) + "\n").encode()
    (bundle_path / "manifest.json").write_bytes(manifest_bytes)
    (bundle_path / "manifest.sha256").write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\n",
        encoding="utf-8",
    )


def _write_trace_profile(
    bundle_path: Path,
    manifest: RunManifest,
    tracing: NativeTracingProfile,
) -> None:
    profile = NativeTraceProfileRecord(
        run_id=manifest.run_id,
        tracing=tracing,
    )
    profile_bytes = (profile.model_dump_json(indent=2) + "\n").encode()
    (bundle_path / "native" / "tracing-profile.json").write_bytes(profile_bytes)
    (bundle_path / "native" / "tracing-profile.sha256").write_text(
        hashlib.sha256(profile_bytes).hexdigest() + "\n",
        encoding="utf-8",
    )


def _finding_codes(result) -> set[str]:
    return {finding.code for finding in result.findings}


def test_oracle_bundle_passes_and_writes_eligibility_mark(tmp_path: Path) -> None:
    bundle_path = _oracle_bundle(tmp_path)

    result = validate_bundle(bundle_path, mark=True)

    assert result.valid is True
    assert result.inference_status == "eligible"
    sidecar = json.loads((bundle_path / "validation.json").read_text(encoding="utf-8"))
    assert sidecar["valid"] is True


def test_manifest_mutation_is_detected_without_deleting_bundle(tmp_path: Path) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    manifest_path = bundle_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["experiment_id"] = "tampered"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    result = validate_bundle(bundle_path, mark=True)

    assert result.valid is False
    assert "manifest_digest_mismatch" in _finding_codes(result)
    assert result.inference_status == "invalid_provenance"
    assert manifest_path.exists()


def test_event_corruption_reports_sequence_lane_and_missing_finish(
    tmp_path: Path,
) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    event_path = bundle_path / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    events[1]["sequence"] = 99
    events[2]["lane"] = "langgraph"
    events = events[:-1]
    event_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = validate_bundle(bundle_path)

    codes = _finding_codes(result)
    assert "event_sequence_noncontiguous" in codes
    assert "event_lane_mismatch" in codes
    assert "run_finish_count" in codes


def test_event_identity_corruption_reports_duplicate_and_dropped_parent(
    tmp_path: Path,
) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    event_path = bundle_path / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    events[2]["event_id"] = events[1]["event_id"]
    events[3]["parent_event_id"] = "00000000-0000-0000-0000-000000000001"
    event_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = validate_bundle(bundle_path)

    codes = _finding_codes(result)
    assert "event_id_duplicate" in codes
    assert "event_parent_not_prior" in codes


def test_artifact_digest_and_terminal_usage_are_reconciled(tmp_path: Path) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    report_path = bundle_path / "artifacts" / "incident-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["root_cause"] = "tampered after scoring"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    terminal_path = bundle_path / "terminal.json"
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal["used_tool_calls"] += 1
    terminal_path.write_text(json.dumps(terminal, indent=2) + "\n", encoding="utf-8")

    result = validate_bundle(bundle_path)

    codes = _finding_codes(result)
    assert "artifact_digest_mismatch" in codes
    assert "terminal_usage_mismatch" in codes


def test_schema_valid_score_tampering_is_recomputed_and_rejected(tmp_path: Path) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    scores_path = bundle_path / "scores.json"
    scores = json.loads(scores_path.read_text(encoding="utf-8"))
    scores["total_score"] = 0.123456
    scores["task_success"] = False
    scores_path.write_text(json.dumps(scores, indent=2) + "\n", encoding="utf-8")

    result = validate_bundle(bundle_path)

    assert result.valid is False
    assert "score_recomputation_mismatch" in _finding_codes(result)


def test_artifact_event_rejects_lexical_path_traversal(tmp_path: Path) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    event_path = bundle_path / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    artifact_event = next(event for event in events if event["kind"] == "artifact.written")
    artifact_event["payload"]["artifact"] = "artifacts/../manifest.json"
    event_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = validate_bundle(bundle_path)

    assert result.valid is False
    assert "artifact_event_path_invalid" in _finding_codes(result)


def test_incomplete_budget_bundle_preserves_bounded_partial_evidence(tmp_path: Path) -> None:
    manifest = _paired_manifest(FrameworkLane.MAF_WORKFLOW)
    bundle = RunBundle.create(tmp_path, manifest)
    bundle.emit(EventKind.RUN_STARTED)
    bundle.emit(EventKind.MODEL_REQUESTED, payload={"model": "local-test"})
    bundle.write_artifact("partial-diagnosis.json", {"status": "interrupted"})
    bundle.write_terminal(
        RunTerminal(
            run_id=manifest.run_id,
            status=TerminalStatus.BUDGET_EXHAUSTED,
            reason="model_call_budget_exhausted",
            used_model_calls=2,
            used_tool_calls=0,
            used_output_tokens=0,
            elapsed_seconds=0.1,
            event_stream_complete=False,
        )
    )

    result = validate_bundle(bundle.path)

    assert result.valid is True
    codes = _finding_codes(result)
    assert "event_stream_incomplete" in codes
    assert "terminal_usage_partially_observed" in codes
    assert "partial_artifacts_unattested" in codes


def test_incomplete_stream_cannot_underreport_observed_usage(tmp_path: Path) -> None:
    manifest = _paired_manifest(FrameworkLane.MAF_WORKFLOW)
    bundle = RunBundle.create(tmp_path, manifest)
    bundle.emit(EventKind.RUN_STARTED)
    bundle.emit(EventKind.MODEL_REQUESTED, payload={"model": "local-test"})
    bundle.write_terminal(
        RunTerminal(
            run_id=manifest.run_id,
            status=TerminalStatus.CANCELLED,
            reason="control_plane_cancelled",
            used_model_calls=0,
            used_tool_calls=0,
            used_output_tokens=0,
            elapsed_seconds=0.1,
            event_stream_complete=False,
        )
    )

    result = validate_bundle(bundle.path)

    assert result.valid is False
    assert "terminal_usage_mismatch" in _finding_codes(result)


def test_pre_start_runner_failure_with_empty_event_stream_is_valid(tmp_path: Path) -> None:
    manifest = _paired_manifest(FrameworkLane.MAF_WORKFLOW)
    bundle = RunBundle.create(tmp_path, manifest)
    bundle.write_terminal(
        RunTerminal(
            run_id=manifest.run_id,
            status=TerminalStatus.RUNNER_FAILURE,
            reason="runner_launch_failed:FileNotFoundError",
            used_model_calls=0,
            used_tool_calls=0,
            used_output_tokens=0,
            elapsed_seconds=0.1,
            execution_started=False,
            event_stream_complete=False,
        )
    )

    result = validate_bundle(bundle.path)

    assert result.valid is True
    assert "event_stream_incomplete" in _finding_codes(result)


def test_world_manifest_linkage_survives_self_consistent_artifact_tampering(
    tmp_path: Path,
) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    world_path = bundle_path / "artifacts" / "world-manifest.json"
    world = json.loads(world_path.read_text(encoding="utf-8"))
    world["seed"] += 1
    world_path.write_text(json.dumps(world, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(canonical_json(world)).hexdigest()
    event_path = bundle_path / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    artifact_event = next(
        event
        for event in events
        if event["kind"] == "artifact.written"
        and event["payload"]["artifact"] == "artifacts/world-manifest.json"
    )
    artifact_event["payload"]["sha256"] = digest
    event_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    result = validate_bundle(bundle_path)

    assert "world_manifest_digest_mismatch" in _finding_codes(result)


def test_matched_pair_passes_with_lane_specific_dependency_locks(
    tmp_path: Path,
) -> None:
    left = _cancelled_bundle(tmp_path / "left", FrameworkLane.MAF_WORKFLOW)
    right = _cancelled_bundle(tmp_path / "right", FrameworkLane.LANGGRAPH)

    result = validate_pair(left, right, mark=True)

    assert result.valid is True
    assert result.track == "explicit_workflow"
    assert (left / "pair-validation.json").is_file()
    assert (right / "pair-validation.json").is_file()


def test_pair_validation_accepts_one_or_both_incomplete_operational_runs(
    tmp_path: Path,
) -> None:
    left = _incomplete_operational_bundle(
        tmp_path / "one-incomplete-left",
        FrameworkLane.MAF_WORKFLOW,
        TerminalStatus.BUDGET_EXHAUSTED,
    )
    right_complete = _cancelled_bundle(
        tmp_path / "one-incomplete-right",
        FrameworkLane.LANGGRAPH,
    )

    one_incomplete = validate_pair(left, right_complete)

    assert one_incomplete.valid is True

    right_incomplete = _incomplete_operational_bundle(
        tmp_path / "both-incomplete-right",
        FrameworkLane.LANGGRAPH,
        TerminalStatus.CANCELLED,
    )
    both_incomplete = validate_pair(left, right_incomplete)

    assert both_incomplete.valid is True


def test_pair_fails_closed_on_held_constant_drift(tmp_path: Path) -> None:
    left = _cancelled_bundle(tmp_path / "left", FrameworkLane.MAF_WORKFLOW)
    right = _cancelled_bundle(tmp_path / "right", FrameworkLane.LANGGRAPH)
    manifest = json.loads((right / "manifest.json").read_text(encoding="utf-8"))
    manifest["tool_catalog_sha256"] = "9" * 64
    _rewrite_manifest(right, manifest)

    result = validate_pair(left, right)

    assert result.valid is False
    assert "pair_provenance_drift" in _finding_codes(result)


def test_pair_fails_closed_on_tracing_profile_drift(tmp_path: Path) -> None:
    left = _cancelled_bundle(tmp_path / "left", FrameworkLane.MAF_WORKFLOW)
    right = _cancelled_bundle(tmp_path / "right", FrameworkLane.LANGGRAPH)
    left_manifest = RunManifest.model_validate_json(
        (left / "manifest.json").read_text(encoding="utf-8")
    )
    right_manifest = RunManifest.model_validate_json(
        (right / "manifest.json").read_text(encoding="utf-8")
    )
    _write_trace_profile(left, left_manifest, NativeTracingProfile())
    _write_trace_profile(
        right,
        right_manifest,
        NativeTracingProfile(exporter_timeout_seconds=2),
    )

    result = validate_pair(left, right)

    assert result.valid is False
    assert "pair_provenance_drift" in _finding_codes(result)


def test_native_trace_ledger_is_reconciled_and_tampering_fails_closed(
    tmp_path: Path,
) -> None:
    bundle_path = _cancelled_bundle(tmp_path, FrameworkLane.MAF_WORKFLOW)
    manifest = RunManifest.model_validate_json(
        (bundle_path / "manifest.json").read_text(encoding="utf-8")
    )
    events = [
        NormalizedEvent.model_validate_json(line)
        for line in (bundle_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    span = NativeSpanRecord(
        run_id=manifest.run_id,
        lane=manifest.lane,
        trace_id="1" * 32,
        span_id="2" * 16,
        name="agent-lab.run",
        span_kind="internal",
        status_code="unset",
        started_unix_nano=1,
        ended_unix_nano=2,
        instrumentation_scope="test",
        operation=TraceOperation.RUN,
    )
    ledger_path = bundle_path / "native" / "traces" / "spans.jsonl"
    ledger_path.parent.mkdir()
    ledger_bytes = (span.model_dump_json() + "\n").encode()
    ledger_path.write_bytes(ledger_bytes)
    tracing = NativeTracingProfile()
    _write_trace_profile(bundle_path, manifest, tracing)
    link = NativeTraceLink(
        run_id=manifest.run_id,
        lane=manifest.lane,
        enabled=True,
        capture_sensitive=False,
        collector_endpoint=tracing.collector_endpoint,
        collector_status=TraceCollectorStatus.UNAVAILABLE,
        phoenix_base_url=tracing.phoenix_base_url,
        phoenix_trace_url=(f"{tracing.phoenix_base_url}/redirects/traces/{span.trace_id}"),
        project_name="agent-lab-maf_workflow",
        primary_trace_id=span.trace_id,
        trace_ids=[span.trace_id],
        span_ledger_path="native/traces/spans.jsonl",
        span_ledger_sha256=hashlib.sha256(ledger_bytes).hexdigest(),
        span_count=1,
        completeness=compute_trace_completeness(events, [span]),
    )
    (bundle_path / "native" / "trace-link.json").write_text(
        link.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    valid = validate_bundle(bundle_path)

    assert valid.valid is True
    assert "native_trace_collector_degraded" in _finding_codes(valid)

    ledger_path.write_text(
        span.model_copy(update={"name": "tampered"}).model_dump_json() + "\n",
        encoding="utf-8",
    )
    invalid = validate_bundle(bundle_path)

    assert invalid.valid is False
    assert "native_span_ledger_digest_mismatch" in _finding_codes(invalid)


def test_cli_provides_json_and_nonzero_invalid_result(tmp_path: Path) -> None:
    bundle_path = _oracle_bundle(tmp_path)
    runner = CliRunner()
    valid = runner.invoke(
        app,
        ["bundle", "validate", str(bundle_path), "--json", "--no-mark"],
    )
    assert valid.exit_code == 0
    assert json.loads(valid.stdout)["valid"] is True

    (bundle_path / "manifest.sha256").write_text("bad\n", encoding="utf-8")
    invalid = runner.invoke(
        app,
        ["bundle", "validate", str(bundle_path), "--no-mark"],
    )
    assert invalid.exit_code == 1
    assert "INVALID_PROVENANCE" in invalid.stdout
