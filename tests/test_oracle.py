import json
from pathlib import Path

from incident_lab_mcp.oracle import run_oracle


def test_scripted_oracle_writes_complete_successful_bundle(tmp_path: Path) -> None:
    bundle_path, score = run_oracle(
        seed=42,
        runs_dir=tmp_path,
        experiment_path=Path("configs/experiments/smoke.toml"),
    )

    assert score.total_score == 1.0
    assert score.task_success is True
    assert {
        "manifest.json",
        "events.jsonl",
        "scores.json",
        "terminal.json",
        "artifacts",
        "checkpoints",
        "native",
        "metrics",
    } <= {path.name for path in bundle_path.iterdir()}
    assert {path.name for path in (bundle_path / "artifacts").iterdir()} == {
        "command-audit.json",
        "evaluation-state.json",
        "incident-report.json",
        "evidence-ledger.json",
    }

    events = [
        json.loads(line)
        for line in (bundle_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert [event["kind"] for event in events].count("run.started") == 1
    assert [event["kind"] for event in events].count("run.finished") == 1
    assert any(event["kind"] == "approval.requested" for event in events)
    assert any(event["kind"] == "approval.decided" for event in events)
    assert any(event["kind"] == "checkpoint.saved" for event in events)

    terminal = json.loads((bundle_path / "terminal.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle_path / "manifest.json").read_text(encoding="utf-8"))
    assert terminal["status"] == "completed"
    assert terminal["run_id"] == manifest["run_id"]
    assert manifest["lane"] == "scripted_oracle"
