import json
import stat
import subprocess
from pathlib import Path

from agent_lab.bundle_validation import validate_bundle

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _experiment(path: Path) -> Path:
    model_profile = REPOSITORY_ROOT / "configs/models/qwen35_9b_vllm.toml"
    path.write_text(
        "\n".join(
            [
                'schema_version = "0.1.0"',
                'experiment_id = "control_probe_test"',
                'scenario = "incident_response"',
                'lanes = ["maf_workflow", "langgraph"]',
                "seeds = [42]",
                "repetitions = 1",
                'order = "randomized"',
                f"model_profile = {json.dumps(str(model_profile))}",
                'fault_profile = "none"',
                "capture_sensitive = false",
                "capture_reasoning = false",
                "",
                "[budget]",
                "wall_clock_seconds = 10",
                "model_calls = 2",
                "tool_calls = 4",
                "output_tokens = 100",
                "approval_requests = 1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _run_probe(
    experiment: Path,
    runs_dir: Path,
    *,
    order_seed: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.experiment_control",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment",
            str(experiment),
            "--runs-dir",
            str(runs_dir),
            "--order-seed",
            str(order_seed),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_paired_native_probe_uses_equal_isolated_worlds(tmp_path: Path) -> None:
    experiment = _experiment(tmp_path / "experiment.toml")
    runs_dir = tmp_path / "runs"

    result = _run_probe(experiment, runs_dir, order_seed=731)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["status"] == "ok"
    assert summary["execution_mode"] == "protocol_probe"
    assert len(summary["runs"]) == 2
    assert {run["lane"] for run in summary["runs"]} == {
        "maf_workflow",
        "langgraph",
    }
    assert len({run["world_manifest_sha256"] for run in summary["runs"]}) == 1
    assert len({run["run_id"] for run in summary["runs"]}) == 2
    assert len({run["bundle"] for run in summary["runs"]}) == 2
    for run in summary["runs"]:
        bundle = Path(run["bundle"])
        checkpoint = Path(run["world_checkpoint"])
        assert checkpoint.is_file()
        assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
        assert json.loads((bundle / "terminal.json").read_text())["status"] == "completed"
        request = (bundle / "native" / "run-request.redacted.json").read_text()
        assert '"token": "[REDACTED]"' in request
        assert "v1." not in request
        validation = validate_bundle(bundle)
        assert validation.valid is False
        assert validation.inference_status == "invalid_provenance"
        assert {finding.code for finding in validation.findings} >= {
            "scores_missing",
            "completed_artifacts_missing",
        }
    assert Path(summary["index"]).is_file()


def test_protocol_probe_refuses_to_overwrite_index(tmp_path: Path) -> None:
    experiment = _experiment(tmp_path / "experiment.toml")
    runs_dir = tmp_path / "runs"
    first = _run_probe(experiment, runs_dir, order_seed=991)
    assert first.returncode == 0, first.stderr

    second = _run_probe(experiment, runs_dir, order_seed=991)
    assert second.returncode != 0
    assert "already exists" in second.stderr
