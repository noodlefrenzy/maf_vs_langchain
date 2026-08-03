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
    assert all(
        finish
        == {
            "actor": "dispatch_probe",
            "model": "agent-lab-protocol-stub",
            "input_tokens": 11,
            "output_tokens": 7,
            "cached_input_tokens": 3,
            "reasoning_tokens": 2,
            "parser_outcome": "provider_response_accepted",
            "provider_request_id": "chatcmpl-multi_turn",
            "queue_seconds": 0.00125,
            "time_to_first_token_seconds": 0.0125,
            "generation_seconds": 0.025,
        }
        for finish in payload["model_finishes"]
    )
    assert len(records) == 2
    assert all(record["fixture"] == "multi_turn" for record in records)
    assert payload["specialist_max_tool_enabled_responses"] == 4
    assert payload["background_task_drain"] == {
        "drain_waited": True,
        "result": "finished",
    }


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
    assert payload["error"] == "model_call_phase_limit_exhausted"
    assert payload["model_calls"] == 1
    assert len(payload["model_starts"]) == 1
    assert len(payload["model_finishes"]) == 1
    assert len(records) == 1
    assert payload["specialist_max_tool_enabled_responses"] == 4
