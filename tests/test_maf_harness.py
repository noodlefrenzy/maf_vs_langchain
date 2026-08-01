import json
import subprocess
from pathlib import Path

from agent_lab.model_compat_stub import CompatibilityStub

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_maf_harness_counts_each_native_model_dispatch() -> None:
    with CompatibilityStub() as stub:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(REPOSITORY_ROOT / "runners/maf"),
                "--no-sync",
                "python",
                "-m",
                "maf_runner.harness_dispatch_probe",
                "--base-url",
                stub.base_url,
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        records = stub.records

    payload = json.loads(result.stdout)
    assert payload["text"] == "multi-turn complete"
    assert payload["model_calls"] == 2
    assert payload["input_tokens"] == 22
    assert payload["output_tokens"] == 14
    assert len(payload["model_starts"]) == 2
    assert len(payload["model_finishes"]) == 2
    assert len(records) == 2
    assert all(record["fixture"] == "multi_turn" for record in records)


def test_maf_harness_enforces_budget_before_next_native_dispatch() -> None:
    with CompatibilityStub() as stub:
        result = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(REPOSITORY_ROOT / "runners/maf"),
                "--no-sync",
                "python",
                "-m",
                "maf_runner.harness_dispatch_probe",
                "--base-url",
                stub.base_url,
                "--model-call-limit",
                "1",
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        records = stub.records

    payload = json.loads(result.stdout)
    assert payload["text"] is None
    assert payload["error"] == "model_call_budget_exhausted"
    assert payload["model_calls"] == 1
    assert len(payload["model_starts"]) == 1
    assert len(payload["model_finishes"]) == 1
    assert len(records) == 1
