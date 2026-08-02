import json
from pathlib import Path
from typing import Any

import pytest

from agent_lab.contracts import ModelProfile
from agent_lab.local_inference_benchmark import (
    _write_json_exclusive,
    calibrate_prompt,
    parse_vllm_metrics,
    stream_completion,
    tokenize_messages,
    verify_malformed_tool_recovery,
)


def _profile() -> ModelProfile:
    return ModelProfile(
        name="Qwen/Qwen3.5-9B",
        base_url="http://127.0.0.1:8001/v1",
        temperature=1.0,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=1.5,
        repetition_penalty=1.0,
        max_output_tokens=8192,
        context_window_tokens=32768,
        request_timeout_seconds=300,
        seed=1729,
        thinking=True,
    )


class _FakeStream(list[bytes]):
    def __init__(self, events: list[bytes]) -> None:
        super().__init__(line for event in events for line in event.splitlines(keepends=True))

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_stream_completion_measures_first_token_and_usage_without_retaining_text() -> None:
    stream = _FakeStream(
        [
            _sse({"choices": [{"delta": {"role": "assistant"}}]}),
            _sse(
                {
                    "system_fingerprint": "vllm-test",
                    "choices": [{"delta": {"reasoning_content": "private reasoning"}}],
                }
            ),
            _sse({"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}),
            _sse(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 3,
                        "total_tokens": 103,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    captured_request: dict[str, Any] = {}

    def opener(request: Any, **_kwargs: Any) -> _FakeStream:
        captured_request.update(json.loads(request.data))
        return stream

    result = stream_completion(
        _profile(),
        [{"role": "user", "content": "measure"}],
        512,
        opener=opener,
    )

    assert captured_request["max_completion_tokens"] == 512
    assert captured_request["top_k"] == 20
    assert captured_request["chat_template_kwargs"] == {"enable_thinking": True}
    assert result["status"] == "completed"
    assert result["prompt_tokens"] == 100
    assert result["completion_tokens"] == 3
    assert result["cached_prompt_tokens"] == 80
    assert result["visible_stream_events"] == 2
    assert result["system_fingerprint"] == "vllm-test"
    assert "private reasoning" not in json.dumps(result)
    assert "answer" not in json.dumps(result)


def test_stream_completion_closes_after_first_visible_delta_for_cancellation() -> None:
    stream = _FakeStream(
        [
            _sse({"choices": [{"delta": {"content": "first"}}]}),
            _sse({"choices": [{"delta": {"content": "must not be consumed"}}]}),
        ]
    )

    result = stream_completion(
        _profile(),
        [{"role": "user", "content": "cancel"}],
        512,
        opener=lambda *_args, **_kwargs: stream,
        cancel_after_first=True,
    )

    assert result["status"] == "client_cancelled"
    assert "completion_tokens" not in result


def test_prompt_calibration_uses_the_served_chat_tokenizer() -> None:
    calls = 0

    def tokenizer(_profile: ModelProfile, messages: list[dict[str, str]]) -> dict[str, int]:
        nonlocal calls
        calls += 1
        repeats = messages[-1]["content"].count("agent-lab telemetry datum ")
        return {"count": 12 + repeats * 2, "max_model_len": 32768}

    messages, count = calibrate_prompt(
        _profile(),
        100,
        marker="calibration",
        tokenizer=tokenizer,
    )

    assert count == 100
    assert messages[-1]["content"].count("agent-lab telemetry datum ") == 44
    assert calls < 20


def test_tokenize_messages_requires_server_count_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        seen.update({"url": url, "payload": payload, "timeout": timeout})
        return {"count": 42, "max_model_len": 32768, "tokens": list(range(42))}

    monkeypatch.setattr("agent_lab.local_inference_benchmark._post_json", post)
    result = tokenize_messages(_profile(), [{"role": "user", "content": "hello"}])

    assert result == {"count": 42, "max_model_len": 32768}
    assert seen["url"] == "http://127.0.0.1:8001/tokenize"
    assert seen["payload"]["add_generation_prompt"] is True


def test_prometheus_parser_aggregates_devices_and_preserves_abort_evidence() -> None:
    metrics = parse_vllm_metrics(
        """
# HELP vllm:num_requests_running running
vllm:num_requests_running{model_name="Qwen/Qwen3.5-9B",engine="0"} 1
vllm:num_requests_running{model_name="Qwen/Qwen3.5-9B",engine="1"} 2
vllm:num_requests_waiting{model_name="Qwen/Qwen3.5-9B"} 0
vllm:generation_tokens_total{model_name="Qwen/Qwen3.5-9B"} 99
vllm:request_success_total{finished_reason="stop"} 4
vllm:request_success_total{finished_reason="abort"} 2
irrelevant_metric 9000
"""
    )

    assert metrics == {
        "num_requests_running": 3.0,
        "num_requests_waiting": 0.0,
        "generation_tokens_total": 99.0,
        "request_success_total": 4.0,
        "request_abort_total": 2.0,
    }


def test_malformed_tool_recovery_requires_one_corrected_json_call() -> None:
    captured: dict[str, Any] = {}

    def requester(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        captured.update({"url": url, "payload": payload, "timeout": timeout})
        return {
            "system_fingerprint": "vllm-test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "correcting",
                        "tool_calls": [
                            {
                                "id": "corrected",
                                "type": "function",
                                "function": {
                                    "name": "lookup_incident",
                                    "arguments": '{"incident_id":"INC-42"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
        }

    result = verify_malformed_tool_recovery(_profile(), requester=requester)

    assert result["passed"] is True
    assert result["reasoning_channel_present"] is True
    assert result["corrected_calls"] == [
        {"name": "lookup_incident", "arguments": {"incident_id": "INC-42"}}
    ]
    prior = captured["payload"]["messages"][2]
    assert prior["tool_calls"][0]["function"]["arguments"] == '{"incident_id":'
    assert captured["payload"]["tool_choice"] == "required"
    assert captured["payload"]["parallel_tool_calls"] is False


def test_characterization_report_write_is_private_and_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    _write_json_exclusive(output, {"measurement_eligible": False})

    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {"measurement_eligible": False}
    with pytest.raises(FileExistsError):
        _write_json_exclusive(output, {"measurement_eligible": True})
