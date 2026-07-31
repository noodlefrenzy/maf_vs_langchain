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
    RunManifest,
    RunTerminal,
    TerminalStatus,
)
from agent_lab.run_bundle import RunBundle


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


def _rewrite_manifest(bundle_path: Path, body: dict[str, object]) -> None:
    manifest_bytes = (json.dumps(body, indent=2) + "\n").encode()
    (bundle_path / "manifest.json").write_bytes(manifest_bytes)
    (bundle_path / "manifest.sha256").write_text(
        hashlib.sha256(manifest_bytes).hexdigest() + "\n",
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


def test_pair_fails_closed_on_held_constant_drift(tmp_path: Path) -> None:
    left = _cancelled_bundle(tmp_path / "left", FrameworkLane.MAF_WORKFLOW)
    right = _cancelled_bundle(tmp_path / "right", FrameworkLane.LANGGRAPH)
    manifest = json.loads((right / "manifest.json").read_text(encoding="utf-8"))
    manifest["tool_catalog_sha256"] = "9" * 64
    _rewrite_manifest(right, manifest)

    result = validate_pair(left, right)

    assert result.valid is False
    assert "pair_provenance_drift" in _finding_codes(result)


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
