"""Deterministic OpenAI Chat Completions stub used by the compatibility suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

from agent_lab.model_compat_contracts import (
    MAX_OUTPUT_TOKENS,
    MIN_P,
    PRESENCE_PENALTY,
    REPETITION_PENALTY,
    SEED,
    TEMPERATURE,
    THINKING,
    TOP_K,
    TOP_P,
    ModelFixture,
)

_FIXTURE_PATTERN = re.compile(r"\[model-wire-fixture:([a-z_]+)]")
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "access_token",
    "bearer_token",
    "password",
    "secret",
    "token",
}
_CREATED_AT = 1_700_000_000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if str(key).lower() in _SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _shape(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if value is None:
        return "null"
    return type(value).__name__


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
            for item in content
        )
    return str(content)


def _fixture_from_body(body: Mapping[str, Any]) -> ModelFixture:
    for message in body.get("messages", []):
        if not isinstance(message, Mapping):
            continue
        match = _FIXTURE_PATTERN.search(_message_text(message))
        if match:
            return ModelFixture(match.group(1))
    raise ValueError("request messages do not contain a model-wire fixture marker")


def _validation_errors(body: Mapping[str, Any]) -> list[str]:
    expected = {
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "top_k": TOP_K,
        "min_p": MIN_P,
        "presence_penalty": PRESENCE_PENALTY,
        "repetition_penalty": REPETITION_PENALTY,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "seed": SEED,
        "chat_template_kwargs": {"enable_thinking": THINKING},
    }
    errors = [
        f"{key}={body.get(key)!r}; expected {value!r}"
        for key, value in expected.items()
        if body.get(key) != value
    ]
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        errors.append("messages must be a non-empty list")
    if not isinstance(body.get("model"), str) or not body["model"]:
        errors.append("model must be a non-empty string")
    return errors


def _expand_server_defaults(body: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    effective = dict(body)
    expansion: dict[str, Any] = {}
    if "stream" not in effective:
        effective["stream"] = False
        expansion["stream"] = False
    if effective.get("tools"):
        if "tool_choice" not in effective:
            effective["tool_choice"] = "auto"
            expansion["tool_choice"] = "auto"
        if "parallel_tool_calls" not in effective:
            effective["parallel_tool_calls"] = True
            expansion["parallel_tool_calls"] = True
    if effective.get("stream") and "stream_options" not in effective:
        effective["stream_options"] = {"include_usage": False}
        expansion["stream_options"] = {"include_usage": False}
    return effective, expansion


def _usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def _metrics() -> dict[str, float]:
    return {
        "queue_time_ms": 1.25,
        "time_to_first_token_ms": 12.5,
        "generation_time_ms": 25.0,
        "tokens_per_second": 280.0,
    }


def _tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _response_message(fixture: ModelFixture, body: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if fixture is ModelFixture.PLAIN:
        return {"role": "assistant", "content": "plain response"}, "stop"
    if fixture is ModelFixture.STRUCTURED:
        return {
            "role": "assistant",
            "content": _canonical_json({"severity": 1, "summary": "stable"}),
        }, "stop"
    if fixture is ModelFixture.STREAMING:
        return {"role": "assistant", "content": "stream response"}, "stop"
    if fixture is ModelFixture.ONE_TOOL:
        calls = [_tool_call("call_lookup", "lookup_incident", '{"incident_id":"INC-42"}')]
        return {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
    if fixture is ModelFixture.MULTI_TOOL:
        calls = [
            _tool_call("call_lookup", "lookup_incident", '{"incident_id":"INC-42"}'),
            _tool_call("call_services", "list_services", '{"team":"platform"}'),
        ]
        return {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
    if fixture is ModelFixture.MALFORMED_TOOL:
        calls = [_tool_call("call_bad", "lookup_incident", "{not-json")]
        return {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"
    has_tool_result = any(
        isinstance(message, Mapping) and message.get("role") == "tool"
        for message in body.get("messages", [])
    )
    if has_tool_result:
        return {"role": "assistant", "content": "multi-turn complete"}, "stop"
    calls = [_tool_call("call_loop", "lookup_incident", '{"incident_id":"INC-42"}')]
    return {"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls"


class _CompatibilityServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], audit_path: Path | None) -> None:
        super().__init__(address, _CompatibilityHandler)
        self.audit_path = audit_path
        self.records: list[dict[str, Any]] = []
        self.records_lock = threading.Lock()

    def record(self, item: dict[str, Any]) -> None:
        with self.records_lock:
            self.records.append(item)
            if self.audit_path is not None:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_path.open("a", encoding="utf-8") as handle:
                    handle.write(_canonical_json(item) + "\n")


class _CompatibilityHandler(BaseHTTPRequestHandler):
    server: _CompatibilityServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _write_json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        encoded = _canonical_json(payload).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/v1/models":
            self._write_json(
                HTTPStatus.OK,
                {"object": "list", "data": [{"id": "agent-lab-protocol-stub", "object": "model"}]},
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("invalid request body length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            fixture = _fixture_from_body(body)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc)}})
            return

        errors = _validation_errors(body)
        if errors:
            response = {"error": {"message": "; ".join(errors), "type": "client_serialization"}}
            self._record_exchange(body, fixture, response, errors)
            self._write_json(HTTPStatus.UNPROCESSABLE_ENTITY, response)
            return

        message, finish_reason = _response_message(fixture, body)
        if body.get("stream"):
            chunks = self._stream_chunks(message, finish_reason)
            response = {"content_type": "text/event-stream", "chunks": chunks, "done": True}
            self._record_exchange(body, fixture, response, errors)
            self._write_stream(chunks)
            return
        response = {
            "id": f"chatcmpl-{fixture.value}",
            "object": "chat.completion",
            "created": _CREATED_AT,
            "model": str(body["model"]),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                    "logprobs": None,
                }
            ],
            "usage": _usage(),
            "metrics": _metrics(),
        }
        self._record_exchange(body, fixture, response, errors)
        self._write_json(HTTPStatus.OK, response)

    def _record_exchange(
        self,
        body: Mapping[str, Any],
        fixture: ModelFixture,
        response: Mapping[str, Any],
        errors: list[str],
    ) -> None:
        framework = self.headers.get("X-Agent-Lab-Framework", "unknown")
        actor = self.headers.get("X-Agent-Lab-Actor")
        redacted_body = _redact(body)
        effective_body, server_defaults = _expand_server_defaults(redacted_body)
        redacted_response = _redact(response)
        self.server.record(
            {
                "framework": framework,
                "actor": actor,
                "fixture": fixture.value,
                "request_index": sum(
                    1
                    for item in self.server.records
                    if item["framework"] == framework and item["fixture"] == fixture.value
                ),
                "wire_request_sha256": hashlib.sha256(
                    _canonical_json(redacted_body).encode()
                ).hexdigest(),
                "effective_request_sha256": hashlib.sha256(
                    _canonical_json(effective_body).encode()
                ).hexdigest(),
                "wire_response_sha256": hashlib.sha256(
                    _canonical_json(redacted_response).encode()
                ).hexdigest(),
                "request_shape": _shape(redacted_body),
                "response_shape": _shape(redacted_response),
                "redacted_request": redacted_body,
                "redacted_response": redacted_response,
                "server_default_expansion": server_defaults,
                "header_names": sorted(name.lower() for name in self.headers),
                "validation_errors": errors,
            }
        )

    def _stream_chunks(
        self, message: Mapping[str, Any], finish_reason: str
    ) -> list[dict[str, Any]]:
        text = str(message.get("content", ""))
        chunks: list[dict[str, Any]] = []
        for index, part in enumerate((text[:7], text[7:])):
            delta: dict[str, Any] = {"content": part}
            if index == 0:
                delta["role"] = "assistant"
            chunks.append(
                {
                    "id": "chatcmpl-streaming",
                    "object": "chat.completion.chunk",
                    "created": _CREATED_AT,
                    "model": "agent-lab-protocol-stub",
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None, "logprobs": None}
                    ],
                }
            )
        chunks.append(
            {
                "id": "chatcmpl-streaming",
                "object": "chat.completion.chunk",
                "created": _CREATED_AT,
                "model": "agent-lab-protocol-stub",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": finish_reason, "logprobs": None}
                ],
            }
        )
        chunks.append(
            {
                "id": "chatcmpl-streaming",
                "object": "chat.completion.chunk",
                "created": _CREATED_AT,
                "model": "agent-lab-protocol-stub",
                "choices": [],
                "usage": _usage(),
            }
        )
        return chunks

    def _write_stream(self, chunks: list[dict[str, Any]]) -> None:
        payload = (
            "".join(f"data: {_canonical_json(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        )
        encoded = payload.encode()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()


class CompatibilityStub(AbstractContextManager["CompatibilityStub"]):
    """Own a loopback-only compatibility server and its captured requests."""

    def __init__(self, *, audit_path: Path | None = None) -> None:
        self._server = _CompatibilityServer(("127.0.0.1", 0), audit_path)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._server.records_lock:
            return list(self._server.records)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--audit-path", type=Path)
    args = parser.parse_args()
    server = _CompatibilityServer(("127.0.0.1", args.port), args.audit_path)
    try:
        host, port = server.server_address
        print(_canonical_json({"base_url": f"http://{host}:{port}/v1"}), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
