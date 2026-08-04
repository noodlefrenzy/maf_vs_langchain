import json
import subprocess
from pathlib import Path

from agent_lab.model_compat_stub import CompatibilityStub

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run_probe(base_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "runners/langgraph"),
            "--no-sync",
            "python",
            "-m",
            "langgraph_runner.harness_dispatch_probe",
            "--base-url",
            base_url,
            *arguments,
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_deep_harness_counts_each_native_model_dispatch_and_stream_fallback() -> None:
    with CompatibilityStub() as stub:
        result = _run_probe(stub.base_url)
        records = stub.records

    payload = json.loads(result.stdout)
    assert payload["text"] == "multi-turn complete"
    assert payload["stream_text"] == "stream response"
    assert payload["model_calls"] == 3
    assert payload["input_tokens"] == 33
    assert payload["output_tokens"] == 21
    assert len(payload["model_starts"]) == 3
    assert len(payload["model_finishes"]) == 3
    nonstreaming = payload["model_finishes"][:2]
    assert all(
        finish["provider_request_id"] == "chatcmpl-multi_turn"
        and finish["queue_seconds"] == 0.00125
        and finish["time_to_first_token_seconds"] == 0.0125
        and finish["generation_seconds"] == 0.025
        and finish["cached_input_tokens"] == 3
        and finish["reasoning_tokens"] == 2
        for finish in nonstreaming
    )
    assert payload["model_finishes"][-1]["provider_request_id"] == "chatcmpl-streaming"
    assert payload["model_finishes"][-1]["time_to_first_token_seconds"] == 0.0125
    assert len(records) == 3
    assert [record["fixture"] for record in records] == [
        "multi_turn",
        "multi_turn",
        "streaming",
    ]
    assert records[-1]["redacted_request"].get("stream", False) is False
    assert payload["subagent_phase_limit"] == {
        "error": "ModelCallLimitExceededError",
        "parent_dispatches": 1,
        "specialist_dispatches": 6,
        "tool_calls": 6,
    }
    assert payload["subagent_completion_boundary"] == {
        "parent_dispatches": 2,
        "specialist_dispatches": 5,
        "tool_calls": 4,
        "text": "delegation complete",
    }
    assert payload["subagent_task_drain"] == {
        "cancelled": True,
        "result": None,
    }


def test_deep_harness_enforces_budget_before_next_native_dispatch() -> None:
    with CompatibilityStub() as stub:
        result = _run_probe(stub.base_url, "--model-call-limit", "1")
        records = stub.records

    payload = json.loads(result.stdout)
    assert payload["text"] is None
    assert payload["stream_text"] is None
    assert payload["error"] == "model_call_budget_exhausted"
    assert payload["model_calls"] == 1
    assert len(payload["model_starts"]) == 1
    assert len(payload["model_finishes"]) == 1
    assert len(records) == 1
