import json
from pathlib import Path

import pytest

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


def _manifest() -> RunManifest:
    return RunManifest(
        experiment_id="test_experiment",
        lane=FrameworkLane.DEEP_AGENTS,
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
        source_tree_sha256="5" * 64,
        framework_versions={"deepagents": "test"},
        dependency_lock_sha256="4" * 64,
        prompt_bundle_sha256="1" * 64,
        tool_catalog_sha256="2" * 64,
        effective_request_policy_sha256="6" * 64,
        execution_provenance_sha256="7" * 64,
        world_manifest_sha256="3" * 64,
    )


def test_run_bundle_assigns_ordered_sequence_numbers(tmp_path: Path) -> None:
    bundle = RunBundle.create(tmp_path, _manifest())
    first = bundle.emit(EventKind.RUN_STARTED)
    second = bundle.emit(EventKind.AGENT_STARTED, actor="commander")
    bundle.write_scores({"task_success": 1})

    assert first.sequence == 0
    assert second.sequence == 1

    lines = (bundle.path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [event["sequence"] for event in events] == [0, 1]
    assert json.loads((bundle.path / "scores.json").read_text())["task_success"] == 1


def test_run_bundle_writes_immutable_artifact_and_terminal(tmp_path: Path) -> None:
    manifest = _manifest()
    bundle = RunBundle.create(tmp_path, manifest)
    artifact = bundle.write_artifact("evidence-ledger.json", {"items": []})
    terminal = RunTerminal(
        run_id=manifest.run_id,
        status=TerminalStatus.COMPLETED,
        reason="scripted fixture completed",
        used_model_calls=0,
        used_tool_calls=3,
        used_output_tokens=0,
        elapsed_seconds=0.1,
    )
    terminal_path = bundle.write_terminal(terminal)

    assert json.loads(artifact.read_text(encoding="utf-8")) == {"items": []}
    assert json.loads(terminal_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert (bundle.path / "checkpoints").is_dir()
    assert (bundle.path / "native").is_dir()
    assert (bundle.path / "metrics").is_dir()
    assert len((bundle.path / "manifest.sha256").read_text().strip()) == 64

    with pytest.raises(FileExistsError):
        bundle.write_artifact("evidence-ledger.json", {"items": []})
    with pytest.raises(FileExistsError):
        bundle.write_terminal(terminal)


def test_run_bundle_rejects_artifact_path_traversal(tmp_path: Path) -> None:
    bundle = RunBundle.create(tmp_path, _manifest())

    with pytest.raises(ValueError):
        bundle.write_artifact("../outside.json", {})
