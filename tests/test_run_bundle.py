import json
from pathlib import Path

from agent_lab.contracts import (
    Budget,
    EventKind,
    FrameworkLane,
    ModelProfile,
    RunManifest,
)
from agent_lab.run_bundle import RunBundle


def _manifest() -> RunManifest:
    return RunManifest(
        experiment_id="test_experiment",
        lane=FrameworkLane.DEEP_AGENTS,
        scenario="incident_response",
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
        framework_versions={"deepagents": "test"},
        prompt_bundle_sha256="1" * 64,
        tool_catalog_sha256="2" * 64,
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
