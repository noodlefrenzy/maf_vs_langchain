"""Dependency-free characterization against one clean, attested local server."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_lab.config import load_model_profile
from agent_lab.contracts import InferenceRuntimeProfile, ModelProfile
from agent_lab.execution_provenance import build_execution_provenance
from agent_lab.model_compat_contracts import openai_tool_schemas
from agent_lab.model_request_policy import openai_request_parameters

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PINNED_VLLM_CONTAINER_NAME = "agent-lab-qwen35-9b-vllm"
PINNED_VLLM_AMD64_IMAGE_DIGEST = (
    "sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9"
)
_PROMETHEUS_VALUE = re.compile(r"^(?P<name>[^\s{]+)(?P<labels>\{[^}]*\})?\s+(?P<value>\S+)")
_VLLM_METRICS = {
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
    "vllm:request_success_total",
}
_LLAMA_CPP_METRICS = {
    "llamacpp:prompt_tokens_total",
    "llamacpp:tokens_predicted_total",
    "llamacpp:requests_processing",
    "llamacpp:requests_deferred",
    "llamacpp:n_tokens_max",
}
_VISIBLE_DELTA_KEYS = ("content", "reasoning", "reasoning_content", "tool_calls")

OpenUrl = Callable[..., AbstractContextManager[BinaryIO]]


def _server_root_url(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/v1")


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured loopback
            value = json.load(response)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise ValueError(f"endpoint returned HTTP {exc.code}: {detail}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"endpoint returned non-object JSON: {url}")
    return value


def _get_text(url: str, timeout: float) -> str:
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - configured loopback
        return response.read().decode("utf-8")


def tokenize_messages(profile: ModelProfile, messages: list[dict[str, str]]) -> dict[str, int]:
    """Use the served tokenizer and chat template; never approximate locally."""

    payload = _post_json(
        f"{_server_root_url(profile.base_url)}/tokenize",
        {
            "model": profile.name,
            "messages": messages,
            "add_generation_prompt": True,
        },
        min(profile.request_timeout_seconds, 30),
    )
    count = payload.get("count")
    max_model_len = payload.get("max_model_len")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise ValueError("vLLM /tokenize did not return a positive token count")
    if not isinstance(max_model_len, int) or isinstance(max_model_len, bool):
        raise ValueError("vLLM /tokenize did not return max_model_len")
    return {"count": count, "max_model_len": max_model_len}


def tokenize_messages_llama_cpp(
    profile: ModelProfile,
    messages: list[dict[str, str]],
) -> dict[str, int]:
    """Apply the served llama.cpp template, then tokenize the exact prompt."""

    root = _server_root_url(profile.base_url)
    templated = _post_json(
        f"{root}/apply-template",
        {
            "messages": messages,
            "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": profile.thinking},
        },
        min(profile.request_timeout_seconds, 30),
    )
    prompt = templated.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("llama.cpp /apply-template did not return a prompt")
    tokenized = _post_json(
        f"{root}/tokenize",
        {"content": prompt, "add_special": False, "parse_special": True},
        min(profile.request_timeout_seconds, 30),
    )
    tokens = tokenized.get("tokens")
    if (
        not isinstance(tokens, list)
        or not tokens
        or any(not isinstance(item, int) for item in tokens)
    ):
        raise ValueError("llama.cpp /tokenize did not return a non-empty integer token list")
    return {"count": len(tokens), "max_model_len": profile.context_window_tokens}


def calibrate_prompt(
    profile: ModelProfile,
    target_tokens: int,
    *,
    marker: str,
    tokenizer: Callable[[ModelProfile, list[dict[str, str]]], dict[str, int]] = tokenize_messages,
    tolerance_tokens: int = 8,
) -> tuple[list[dict[str, str]], int]:
    """Find the largest deterministic served-chat prompt no larger than a target."""

    if target_tokens <= 0:
        raise ValueError("target prompt tokens must be positive")

    def messages(repeats: int) -> list[dict[str, str]]:
        body = (
            f"Benchmark marker {marker}. Continue the numbered diagnostic sequence until the "
            "output limit; do not stop early. Context follows.\n"
            + "agent-lab telemetry datum "
            * repeats
        )
        return [
            {
                "role": "system",
                "content": "This is a local inference throughput measurement. Return plain text.",
            },
            {"role": "user", "content": body},
        ]

    empty = messages(0)
    empty_count = tokenizer(profile, empty)["count"]
    if empty_count > target_tokens:
        raise ValueError(
            f"target prompt {target_tokens} is smaller than the "
            f"chat-template overhead {empty_count}"
        )
    lower = 0
    upper = max(1, target_tokens - empty_count)
    while tokenizer(profile, messages(upper))["count"] <= target_tokens:
        lower = upper
        upper *= 2
    while lower + 1 < upper:
        midpoint = (lower + upper) // 2
        if tokenizer(profile, messages(midpoint))["count"] <= target_tokens:
            lower = midpoint
        else:
            upper = midpoint
    result = messages(lower)
    count = tokenizer(profile, result)["count"]
    if target_tokens - count > tolerance_tokens:
        raise ValueError(
            f"could not calibrate served prompt within {tolerance_tokens} tokens: "
            f"target={target_tokens}, actual={count}"
        )
    return result, count


def _iter_sse_data(response: Iterable[bytes]) -> Iterable[bytes]:
    data_lines: list[bytes] = []
    for raw_line in response:
        line = raw_line.rstrip(b"\r\n")
        if not line:
            if data_lines:
                yield b"\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield b"\n".join(data_lines)


def _visible_delta(payload: dict[str, Any]) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
            continue
        delta = choice["delta"]
        if any(delta.get(key) not in (None, "", []) for key in _VISIBLE_DELTA_KEYS):
            return True
    return False


def stream_completion(
    profile: ModelProfile,
    messages: list[dict[str, str]],
    output_cap: int,
    *,
    opener: OpenUrl = urlopen,
    cancel_after_first: bool = False,
) -> dict[str, Any]:
    """Measure one raw streaming request without retaining generated text or reasoning."""

    body = {
        **openai_request_parameters(profile),
        "messages": messages,
        "max_completion_tokens": output_cap,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request = Request(
        f"{profile.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agent-Lab-Framework": "characterization",
            "X-Agent-Lab-Actor": "throughput_probe",
        },
        method="POST",
    )
    started_ns = time.perf_counter_ns()
    first_visible_ns: int | None = None
    finished_ns = started_ns
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    fingerprint: str | None = None
    response_sha256 = hashlib.sha256()
    visible_events = 0
    with opener(request, timeout=profile.request_timeout_seconds) as response:
        for data in _iter_sse_data(response):
            if data == b"[DONE]":
                break
            response_sha256.update(data)
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise ValueError("stream returned a non-object SSE payload")
            fingerprint = payload.get("system_fingerprint") or fingerprint
            if _visible_delta(payload):
                visible_events += 1
                if first_visible_ns is None:
                    first_visible_ns = time.perf_counter_ns()
                    if cancel_after_first:
                        break
            if isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
            choices = payload.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                        finish_reason = str(choice["finish_reason"])
        finished_ns = time.perf_counter_ns()

    if first_visible_ns is None:
        raise ValueError("stream completed without a visible model delta")
    if cancel_after_first:
        return {
            "status": "client_cancelled",
            "started_monotonic_ns": started_ns,
            "first_visible_monotonic_ns": first_visible_ns,
            "finished_monotonic_ns": finished_ns,
            "time_to_first_token_seconds": (first_visible_ns - started_ns) / 1e9,
            "client_disconnect_seconds": (finished_ns - started_ns) / 1e9,
            "response_prefix_sha256": response_sha256.hexdigest(),
        }
    if usage is None:
        raise ValueError("stream completed without final usage metadata")
    prompt_tokens = _usage_int(usage, "prompt_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens")
    decode_seconds = max(0.0, (finished_ns - first_visible_ns) / 1e9)
    decode_tokens = max(0, completion_tokens - 1)
    return {
        "status": "completed",
        "started_monotonic_ns": started_ns,
        "first_visible_monotonic_ns": first_visible_ns,
        "finished_monotonic_ns": finished_ns,
        "time_to_first_token_seconds": (first_visible_ns - started_ns) / 1e9,
        "end_to_end_seconds": (finished_ns - started_ns) / 1e9,
        "decode_seconds": decode_seconds,
        "decode_tokens_per_second": decode_tokens / decode_seconds if decode_seconds else None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": _usage_int(usage, "total_tokens"),
        "cached_prompt_tokens": _cached_prompt_tokens(usage),
        "visible_stream_events": visible_events,
        "finish_reason": finish_reason,
        "system_fingerprint": fingerprint,
        "response_sha256": response_sha256.hexdigest(),
    }


def _usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"stream usage did not provide non-negative {key}")
    return value


def _cached_prompt_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return None
    value = details.get("cached_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_vllm_metrics(text: str) -> dict[str, float]:
    """Aggregate the small stable metric subset used as cancellation/cache evidence."""

    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _PROMETHEUS_VALUE.match(line)
        if match is None or match.group("name") not in _VLLM_METRICS:
            continue
        name = match.group("name").removeprefix("vllm:")
        labels = match.group("labels") or ""
        if name == "request_success_total" and 'finished_reason="abort"' in labels:
            name = "request_abort_total"
        elif name == "request_success_total":
            name = "request_success_total"
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isfinite(value):
            values[name] = values.get(name, 0.0) + value
    return values


def read_vllm_metrics(profile: ModelProfile) -> dict[str, float]:
    return parse_vllm_metrics(
        _get_text(
            f"{_server_root_url(profile.base_url)}/metrics",
            min(profile.request_timeout_seconds, 10),
        )
    )


def parse_llama_cpp_metrics(text: str) -> dict[str, float]:
    """Normalize llama.cpp counters to the characterization metric vocabulary."""

    normalized_names = {
        "prompt_tokens_total": "prompt_tokens_total",
        "tokens_predicted_total": "generation_tokens_total",
        "requests_processing": "num_requests_running",
        "requests_deferred": "num_requests_waiting",
        "n_tokens_max": "max_context_tokens_observed",
    }
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _PROMETHEUS_VALUE.match(line)
        if match is None or match.group("name") not in _LLAMA_CPP_METRICS:
            continue
        name = normalized_names[match.group("name").removeprefix("llamacpp:")]
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isfinite(value):
            values[name] = values.get(name, 0.0) + value
    return values


def read_llama_cpp_metrics(profile: ModelProfile) -> dict[str, float]:
    return parse_llama_cpp_metrics(
        _get_text(
            f"{_server_root_url(profile.base_url)}/metrics",
            min(profile.request_timeout_seconds, 10),
        )
    )


@dataclass
class ResourceSampler:
    """Sample system/GPU and the full process tree without extra Python packages."""

    root_pid: int | None
    container_name: str | None = None
    interval_seconds: float = 0.1
    samples: list[dict[str, Any]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("resource sampler is already running")
        self._sample()
        self._thread = threading.Thread(target=self._run, name="resource-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 5))
        self._sample()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        sample: dict[str, Any] = {"monotonic_ns": time.perf_counter_ns()}
        sample.update(_host_memory_sample())
        sample.update(_gpu_sample())
        if self.root_pid is not None:
            sample["server_process_tree_rss_mib"] = _process_tree_rss_mib(self.root_pid)
        if self.container_name is not None:
            sample.update(_container_memory_sample(self.container_name))
        self.samples.append(sample)

    def summary(self, start_ns: int | None = None, end_ns: int | None = None) -> dict[str, Any]:
        selected = [
            sample
            for sample in self.samples
            if (start_ns is None or sample["monotonic_ns"] >= start_ns)
            and (end_ns is None or sample["monotonic_ns"] <= end_ns)
        ]
        if not selected:
            return {"sample_count": 0}
        keys = (
            "gpu_memory_used_mib",
            "gpu_utilization_percent",
            "host_memory_used_mib",
            "server_process_tree_rss_mib",
            "server_container_memory_used_mib",
        )
        result: dict[str, Any] = {"sample_count": len(selected)}
        for key in keys:
            values = [
                sample[key] for sample in selected if isinstance(sample.get(key), int | float)
            ]
            result[f"peak_{key}"] = max(values) if values else None
        return result


def _host_memory_sample() -> dict[str, float]:
    fields: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, _, remainder = line.partition(":")
        value = remainder.strip().split(maxsplit=1)[0]
        if value.isdigit():
            fields[key] = int(value)
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if total is None or available is None:
        return {}
    return {"host_memory_used_mib": (total - available) / 1024}


def _nvidia_smi_path() -> str | None:
    discovered = shutil.which("nvidia-smi")
    if discovered:
        return discovered
    wsl_path = Path("/usr/lib/wsl/lib/nvidia-smi")
    return str(wsl_path) if wsl_path.is_file() else None


def _gpu_sample() -> dict[str, float]:
    executable = _nvidia_smi_path()
    if executable is None:
        return {}
    result = subprocess.run(
        [
            executable,
            "--query-gpu=memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    fields = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
    if len(fields) != 2:
        return {}
    try:
        return {
            "gpu_memory_used_mib": float(fields[0]),
            "gpu_utilization_percent": float(fields[1]),
        }
    except ValueError:
        return {}


def _container_memory_sample(container_name: str) -> dict[str, float]:
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return {}
    used = result.stdout.partition("/")[0].strip()
    match = re.fullmatch(r"([0-9.]+)\s*([KMGT]i?B|B)", used, flags=re.IGNORECASE)
    if match is None:
        return {}
    multipliers = {
        "b": 1 / (1024 * 1024),
        "kb": 1 / 1024,
        "kib": 1 / 1024,
        "mb": 1,
        "mib": 1,
        "gb": 1024,
        "gib": 1024,
        "tb": 1024 * 1024,
        "tib": 1024 * 1024,
    }
    return {
        "server_container_memory_used_mib": float(match.group(1))
        * multipliers[match.group(2).lower()]
    }


def discover_process_pid(process_name: str) -> int:
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if argv and Path(os.fsdecode(argv[0])).name == process_name:
            matches.append(int(entry.name))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {process_name} process; found {len(matches)}")
    return matches[0]


def _process_tree_rss_mib(root_pid: int) -> float | None:
    parent_by_pid: dict[int, int] = {}
    rss_kib: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            close_paren = stat.rfind(")")
            fields = stat[close_paren + 2 :].split()
            parent_by_pid[int(entry.name)] = int(fields[1])
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, IndexError):
            continue
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss_kib[int(entry.name)] = int(line.split()[1])
                break
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parent_by_pid.items():
            if parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    values = [rss_kib[pid] for pid in selected if pid in rss_kib]
    return sum(values) / 1024 if values else None


def _metrics_are_idle(metrics: dict[str, float]) -> bool:
    return metrics.get("num_requests_running") == 0 and metrics.get("num_requests_waiting") == 0


def verify_cancellation(
    profile: ModelProfile,
    messages: list[dict[str, str]],
    *,
    prompt_tokens: int,
    metrics_reader: Callable[[ModelProfile], dict[str, float]] = read_vllm_metrics,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    output_cap = min(
        profile.max_output_tokens,
        profile.context_window_tokens - prompt_tokens,
    )
    if output_cap <= 0:
        raise ValueError("cancellation prompt leaves no room for generated tokens")
    before = metrics_reader(profile)
    request_result = stream_completion(
        profile,
        messages,
        output_cap,
        cancel_after_first=True,
    )
    deadline = time.monotonic() + timeout_seconds
    after = metrics_reader(profile)
    while not _metrics_are_idle(after) and time.monotonic() < deadline:
        time.sleep(0.1)
        after = metrics_reader(profile)
    health_ok = False
    try:
        _get_text(f"{_server_root_url(profile.base_url)}/health", 5)
        health_ok = True
    except OSError:
        pass
    before_abort = before.get("request_abort_total")
    after_abort = after.get("request_abort_total")
    abort_delta = (
        after_abort - before_abort if before_abort is not None and after_abort is not None else None
    )
    drained = _metrics_are_idle(after)
    return {
        "passed": request_result["status"] == "client_cancelled" and drained and health_ok,
        "prompt_tokens": prompt_tokens,
        "output_cap": output_cap,
        "request": request_result,
        "server_drained": drained,
        "server_healthy": health_ok,
        "abort_counter_delta": abort_delta,
        "counter_evidence_gap": (
            None
            if abort_delta is not None and abort_delta > 0
            else "abort counter did not increase"
        ),
        "metrics_before": before,
        "metrics_after": after,
    }


def verify_malformed_tool_recovery(
    profile: ModelProfile,
    *,
    requester: Callable[[str, dict[str, Any], float], dict[str, Any]] = _post_json,
) -> dict[str, Any]:
    """Feed inert malformed-call evidence plus a correction and require a valid call."""

    malformed_arguments = '{"incident_id":'
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "Use the supplied tool. Correct invalid arguments before retrying.",
        },
        {"role": "user", "content": "Look up incident INC-42."},
        {
            "role": "assistant",
            "content": (
                "Rejected malformed tool output: "
                f'{{"name":"lookup_incident","arguments":{malformed_arguments}'
            ),
        },
        {
            "role": "user",
            "content": (
                "The prior tool output could not be parsed because its arguments were invalid "
                "JSON. Retry lookup_incident once with valid JSON for incident INC-42."
            ),
        },
    ]
    payload = {
        **openai_request_parameters(profile),
        "messages": messages,
        "tools": openai_tool_schemas(),
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "max_completion_tokens": min(512, profile.max_output_tokens),
    }
    response = requester(
        f"{profile.base_url.rstrip('/')}/chat/completions",
        payload,
        profile.request_timeout_seconds,
    )
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("malformed recovery returned an invalid choices envelope")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
        raise ValueError("malformed recovery did not return tool calls")
    calls = message["tool_calls"]
    corrected: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ValueError("malformed recovery returned an invalid tool call")
        function = call["function"]
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ValueError("malformed recovery tool arguments were not a JSON string")
        corrected.append({"name": function.get("name"), "arguments": json.loads(arguments)})
    passed = corrected == [{"name": "lookup_incident", "arguments": {"incident_id": "INC-42"}}]
    return {
        "passed": passed,
        "corrected_calls": corrected,
        "malformed_evidence_sha256": hashlib.sha256(
            malformed_arguments.encode("utf-8")
        ).hexdigest(),
        "reasoning_channel_present": bool(
            message.get("reasoning") or message.get("reasoning_content")
        ),
        "response_sha256": hashlib.sha256(
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "usage": response.get("usage"),
        "system_fingerprint": response.get("system_fingerprint"),
    }


def _source_identity(repository_root: Path) -> tuple[str, str]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout.split(b"\0")
    digest = hashlib.sha256()
    for raw_path in sorted(filter(None, files)):
        path = repository_root / os.fsdecode(raw_path)
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"[MISSING]")
        digest.update(b"\0")
    return commit, digest.hexdigest()


def _run_batch(
    profile: ModelProfile,
    messages: list[dict[str, str]],
    output_cap: int,
    concurrency: int,
) -> dict[str, Any]:
    started_ns = time.perf_counter_ns()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(stream_completion, profile, messages, output_cap)
            for _ in range(concurrency)
        ]
        requests = [future.result() for future in futures]
    finished_ns = time.perf_counter_ns()
    batch_seconds = (finished_ns - started_ns) / 1e9
    tokens = sum(result["completion_tokens"] for result in requests)
    return {
        "started_monotonic_ns": started_ns,
        "finished_monotonic_ns": finished_ns,
        "batch_seconds": batch_seconds,
        "aggregate_decode_tokens_per_second": tokens / batch_seconds if batch_seconds else None,
        "requests": requests,
    }


def run_characterization(
    *,
    profile: ModelProfile,
    context_tokens: int,
    output_caps: tuple[int, ...] = (512, 2048, 8192),
    concurrencies: tuple[int, ...] = (1, 2),
    repository_root: Path = REPOSITORY_ROOT,
    server_kind: Literal["llama_cpp", "vllm"] = "vllm",
    process_name: str = "vllm",
    server_pid: int | None = None,
    container_name: str | None = PINNED_VLLM_CONTAINER_NAME,
    container_image_digest: str | None = PINNED_VLLM_AMD64_IMAGE_DIGEST,
    restart_phase: Literal["before_restart", "after_restart"] = "before_restart",
) -> dict[str, Any]:
    """Run one server-context slice against a fresh local serving process."""

    if context_tokens <= 0 or any(value <= 0 for value in (*output_caps, *concurrencies)):
        raise ValueError("context, output caps, and concurrencies must be positive")
    effective_profile = profile.model_copy(update={"context_window_tokens": context_tokens})
    source_commit, source_tree_sha256 = _source_identity(repository_root)
    runtime_profile = InferenceRuntimeProfile(
        server_kind=server_kind,
        compute_backend="nvidia",
        process_name=process_name,
        container_name=container_name,
        container_image_digest=container_image_digest,
    )
    provenance = build_execution_provenance(
        repository_root=repository_root,
        execution_mode="native_workflow",
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
        model=effective_profile,
        runtime_profile=runtime_profile,
    )
    served_context = provenance["model_runtime"]["server"]["endpoint_flags"][
        "context_window_tokens"
    ]
    if served_context != context_tokens:
        raise ValueError(
            f"benchmark context {context_tokens} differs from served max-model-len {served_context}"
        )
    resolved_pid = (
        server_pid
        if server_pid is not None
        else None
        if container_name is not None
        else discover_process_pid(process_name)
    )
    metrics_reader = read_vllm_metrics if server_kind == "vllm" else read_llama_cpp_metrics
    tokenizer = tokenize_messages if server_kind == "vllm" else tokenize_messages_llama_cpp
    metrics_before = metrics_reader(effective_profile)
    cold_precondition = (
        metrics_before.get("generation_tokens_total", 0) == 0
        and metrics_before.get("request_success_total", 0) == 0
        and _metrics_are_idle(metrics_before)
    )
    if not cold_precondition:
        raise ValueError("cold characterization requires a fresh vLLM server with zero requests")

    sampler = ResourceSampler(resolved_pid, container_name=container_name)
    sampler.start()
    state_probes: list[dict[str, Any]] = []
    matrix: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        state_cap = min(output_caps)
        state_target = min(int(context_tokens * 0.75), context_tokens - state_cap - 64)
        state_messages, state_prompt_tokens = calibrate_prompt(
            effective_profile,
            state_target,
            marker=f"context-{context_tokens}-prefix-a",
            tokenizer=tokenizer,
        )
        for state, messages in (
            ("cold_first_request", state_messages),
            (
                "warm_model",
                calibrate_prompt(
                    effective_profile,
                    state_target,
                    marker=f"context-{context_tokens}-prefix-b",
                    tokenizer=tokenizer,
                )[0],
            ),
            ("warm_prefix_cache", state_messages),
        ):
            result = _run_batch(effective_profile, messages, state_cap, 1)
            result.update(
                {
                    "state": state,
                    "prompt_tokens_target": state_target,
                    "prompt_tokens_calibrated": state_prompt_tokens,
                }
            )
            state_probes.append(result)

        for output_cap in output_caps:
            prompt_target = min(int(context_tokens * 0.75), context_tokens - output_cap - 64)
            if prompt_target < 128:
                skipped.append(
                    {
                        "output_cap": output_cap,
                        "reason": "output cap leaves fewer than 128 prompt tokens",
                    }
                )
                continue
            messages, prompt_tokens = calibrate_prompt(
                effective_profile,
                prompt_target,
                marker=f"context-{context_tokens}-output-{output_cap}",
                tokenizer=tokenizer,
            )
            for concurrency in concurrencies:
                result = _run_batch(effective_profile, messages, output_cap, concurrency)
                result.update(
                    {
                        "state": "warm_matrix",
                        "output_cap": output_cap,
                        "concurrency": concurrency,
                        "prompt_tokens_target": prompt_target,
                        "prompt_tokens_calibrated": prompt_tokens,
                    }
                )
                matrix.append(result)
        cancel_target = min(2048, context_tokens - 1024)
        cancellation_messages, cancellation_prompt_tokens = calibrate_prompt(
            effective_profile,
            cancel_target,
            marker=f"context-{context_tokens}-cancellation",
            tokenizer=tokenizer,
        )
        cancellation = verify_cancellation(
            effective_profile,
            cancellation_messages,
            prompt_tokens=cancellation_prompt_tokens,
            metrics_reader=metrics_reader,
        )
        malformed_tool_recovery = verify_malformed_tool_recovery(effective_profile)
    finally:
        sampler.stop()

    for result in [*state_probes, *matrix]:
        result["resources"] = sampler.summary(
            result["started_monotonic_ns"], result["finished_monotonic_ns"]
        )
    metrics_after = metrics_reader(effective_profile)
    report = {
        "schema_version": "local-inference-characterization/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "measurement_eligible": cancellation["passed"] and malformed_tool_recovery["passed"],
        "profile": effective_profile.model_dump(mode="json"),
        "context_tokens": context_tokens,
        "restart_phase": restart_phase,
        "source_and_runtime_provenance": provenance,
        "cold_precondition": cold_precondition,
        "server_pid": resolved_pid,
        "server_container": container_name,
        "server_kind": server_kind,
        "telemetry_scope": {
            "gpu": "whole NVIDIA device",
            "host_memory": "whole host used memory",
            "server_memory": (
                "Docker container memory usage"
                if container_name is not None
                else "server root process and descendants RSS"
            ),
        },
        "baseline_resources": sampler.samples[0] if sampler.samples else None,
        "peak_resources": sampler.summary(),
        "state_probes": state_probes,
        "matrix": matrix,
        "skipped": skipped,
        "cancellation": cancellation,
        "malformed_tool_recovery": malformed_tool_recovery,
        "metrics_before": metrics_before,
        "metrics_after": metrics_after,
    }
    return report


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-profile",
        type=Path,
        default=Path("configs/models/qwen35_9b_vllm.toml"),
    )
    parser.add_argument("--context-tokens", type=int, choices=(8192, 16384, 32768), required=True)
    parser.add_argument("--output-caps", type=int, nargs="+", default=(512, 2048, 8192))
    parser.add_argument("--concurrencies", type=int, nargs="+", default=(1, 2))
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--server-kind", choices=("llama_cpp", "vllm"), default="vllm")
    parser.add_argument("--process-name", default="vllm")
    parser.add_argument("--server-pid", type=int)
    parser.add_argument(
        "--restart-phase",
        choices=("before_restart", "after_restart"),
        required=True,
    )
    parser.add_argument("--container-name")
    parser.add_argument(
        "--container-image-digest",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    container_name = args.container_name
    container_image_digest = args.container_image_digest
    if args.server_kind == "vllm" and container_name is None:
        container_name = PINNED_VLLM_CONTAINER_NAME
    if args.server_kind == "vllm" and container_image_digest is None:
        container_image_digest = PINNED_VLLM_AMD64_IMAGE_DIGEST
    report = run_characterization(
        profile=load_model_profile(args.model_profile),
        context_tokens=args.context_tokens,
        output_caps=tuple(args.output_caps),
        concurrencies=tuple(args.concurrencies),
        repository_root=args.repository_root.resolve(),
        server_kind=args.server_kind,
        process_name=args.process_name,
        server_pid=args.server_pid,
        container_name=container_name,
        container_image_digest=container_image_digest,
        restart_phase=args.restart_phase,
    )
    _write_json_exclusive(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if report["measurement_eligible"] else 1)


if __name__ == "__main__":
    main()
