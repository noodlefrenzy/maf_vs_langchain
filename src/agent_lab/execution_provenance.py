"""Measured-run source, model-server, and compute-runtime attestation."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import InferenceRuntimeProfile, ModelProfile

ExecutionMode = Literal["protocol_probe", "native_workflow"]
CommandRunner = Callable[[Sequence[str], float], str]

_SNAPSHOT_REVISION = re.compile(r"/snapshots/([a-f0-9]{40})/")
_VULKAN_FIELDS = {
    "apiVersion",
    "driverVersion",
    "vendorID",
    "deviceID",
    "deviceType",
    "deviceName",
    "driverID",
    "driverName",
    "driverInfo",
    "conformanceVersion",
    "deviceUUID",
    "driverUUID",
}
_SERVE_FLAGS_WITH_VALUES = {
    "-hf",
    "-ngl",
    "--cache-reuse",
    "--chat-template",
    "--ctx-size",
    "--host",
    "--model",
    "--n-gpu-layers",
    "--parallel",
    "--port",
    "--reasoning-format",
}
_SERVE_BOOLEAN_FLAGS = {
    "--jinja",
    "--metrics",
    "--no-context-shift",
    "--slots",
}
_SENSITIVE_SERVE_FLAGS = {
    "--api-key",
    "--api-key-file",
    "--ssl-key-file",
}


def build_execution_provenance(
    *,
    repository_root: Path,
    execution_mode: ExecutionMode,
    source_commit: str,
    source_tree_sha256: str,
    model: ModelProfile,
    runtime_profile: InferenceRuntimeProfile,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Build one shared, hashed provenance snapshot before either lane starts."""

    worktree = attest_source_worktree(
        repository_root,
        execution_mode=execution_mode,
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
    )
    if execution_mode == "native_workflow":
        model_runtime = attest_model_runtime(
            model,
            runtime_profile,
            command_runner=command_runner,
        )
    else:
        model_runtime = {
            "collection_status": "not_collected_protocol_probe",
            "measurement_eligible": False,
        }
    snapshot = {
        "schema_version": "execution-provenance/v1",
        "execution_mode": execution_mode,
        "source_worktree": worktree,
        "model_runtime": model_runtime,
    }
    return {**snapshot, "provenance_sha256": sha256_payload(snapshot)}


def verify_execution_provenance(
    expected: dict[str, Any],
    *,
    repository_root: Path,
    execution_mode: ExecutionMode,
    source_commit: str,
    source_tree_sha256: str,
    model: ModelProfile,
    runtime_profile: InferenceRuntimeProfile,
) -> None:
    """Re-attest immediately before a lane starts and reject runtime drift."""

    observed = build_execution_provenance(
        repository_root=repository_root,
        execution_mode=execution_mode,
        source_commit=source_commit,
        source_tree_sha256=source_tree_sha256,
        model=model,
        runtime_profile=runtime_profile,
    )
    if observed["provenance_sha256"] != expected["provenance_sha256"]:
        raise ValueError("execution provenance changed after paired preflight")


def attest_source_worktree(
    repository_root: Path,
    *,
    execution_mode: ExecutionMode,
    source_commit: str,
    source_tree_sha256: str,
) -> dict[str, Any]:
    """Require clean measured sources and explicitly label dirty protocol probes."""

    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    entries = [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]
    clean = not entries
    if execution_mode == "native_workflow" and not clean:
        preview = ", ".join(entries[:5])
        raise ValueError(f"measured native workflow requires a clean Git worktree: {preview}")
    return {
        "source_commit": source_commit,
        "source_tree_sha256": source_tree_sha256,
        "clean": clean,
        "measurement_eligible": execution_mode == "native_workflow" and clean,
        "dirty_protocol_probe": execution_mode == "protocol_probe" and not clean,
        "status_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "status_entries": entries,
    }


def attest_model_runtime(
    model: ModelProfile,
    runtime_profile: InferenceRuntimeProfile,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Collect fail-closed llama.cpp endpoint and serving-host identity."""

    props = _fetch_json(_props_url(model.base_url), model.request_timeout_seconds)
    models = _fetch_json(f"{model.base_url.rstrip('/')}/models", model.request_timeout_seconds)
    data = models.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("model endpoint must expose exactly one OpenAI model")
    served = data[0]
    served_id = served.get("id")
    aliases = served.get("aliases", [])
    if model.name != served_id and model.name not in aliases:
        raise ValueError(f"requested model {model.name!r} differs from served model {served_id!r}")

    model_path = _required_string(props, "model_path")
    revision_match = _SNAPSHOT_REVISION.search(model_path)
    if revision_match is None:
        raise ValueError("served model path does not contain an immutable snapshot revision")
    meta = served.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("model endpoint did not expose tokenizer/model metadata")
    n_ctx = _required_int(meta, "n_ctx")
    if model.context_window_tokens > n_ctx:
        raise ValueError(
            f"requested context window {model.context_window_tokens} exceeds served {n_ctx}"
        )

    chat_template = _required_string(props, "chat_template")
    defaults = props.get("default_generation_settings")
    if not isinstance(defaults, dict) or not isinstance(defaults.get("params"), dict):
        raise ValueError("model endpoint did not expose parser defaults")
    parser_params = defaults["params"]
    parser = {
        key: parser_params[key]
        for key in ("chat_format", "reasoning_format", "reasoning_in_content")
        if key in parser_params
    }
    if set(parser) != {"chat_format", "reasoning_format", "reasoning_in_content"}:
        raise ValueError("model endpoint parser configuration is incomplete")

    runner = command_runner or _run_host_command(runtime_profile.host)
    host_runtime = _attest_compute_runtime(runtime_profile, runner)
    tokenizer = {
        "vocab_type": _required_int(meta, "vocab_type"),
        "vocab_size": _required_int(meta, "n_vocab"),
        "bos_token_sha256": _hash_json_value(props.get("bos_token")),
        "eos_token_sha256": _hash_json_value(props.get("eos_token")),
    }
    artifact = {
        "model_id": served_id,
        "aliases": aliases,
        "model_alias": _required_string(props, "model_alias"),
        "artifact_path": model_path,
        "artifact_name": Path(model_path).name,
        "revision": revision_match.group(1),
        "format": (served.get("details") or {}).get("format", "gguf"),
        "quantization": _required_string(props, "model_ftype"),
        "size_bytes": _required_int(meta, "size"),
        "parameter_count": _required_int(meta, "n_params"),
    }
    endpoint_flags = {
        "context_window_tokens": n_ctx,
        "training_context_window_tokens": _required_int(meta, "n_ctx_train"),
        "total_slots": _required_int(props, "total_slots"),
        "endpoint_metrics": bool(props.get("endpoint_metrics")),
        "endpoint_props": bool(props.get("endpoint_props")),
        "endpoint_slots": bool(props.get("endpoint_slots")),
        "cors_proxy_enabled": bool(props.get("cors_proxy_enabled")),
        "ui_enabled": bool(props.get("ui")),
        "modalities": props.get("modalities"),
    }
    attestation = {
        "collection_status": "complete",
        "measurement_eligible": True,
        "endpoint": _redacted_endpoint(model.base_url),
        "artifact": artifact,
        "tokenizer": {**tokenizer, "tokenizer_sha256": sha256_payload(tokenizer)},
        "chat_template": {
            "sha256": hashlib.sha256(chat_template.encode("utf-8")).hexdigest(),
            "capabilities": props.get("chat_template_caps"),
        },
        "server": {
            "build": _required_string(props, "build_info"),
            "parser": parser,
            "endpoint_flags": endpoint_flags,
            "serve_process": host_runtime.pop("serve_process"),
        },
        "compute_runtime": host_runtime,
        "endpoint_payload_sha256": {
            "props": sha256_payload(props),
            "models": sha256_payload(models),
        },
    }
    return {**attestation, "attestation_sha256": sha256_payload(attestation)}


def _attest_compute_runtime(
    runtime_profile: InferenceRuntimeProfile,
    runner: CommandRunner,
) -> dict[str, Any]:
    timeout = runtime_profile.command_timeout_seconds
    kernel = runner(["uname", "-a"], timeout).strip()
    process_output = runner(["ps", "-eo", "pid=,args="], timeout)
    process_lines = [
        line.strip()
        for line in process_output.splitlines()
        if _process_matches(line, runtime_profile.process_name)
    ]
    if len(process_lines) != 1:
        raise ValueError(
            f"expected exactly one {runtime_profile.process_name} serving process; "
            f"found {len(process_lines)}"
        )
    process_tokens = shlex.split(process_lines[0], posix=True)
    if process_tokens and process_tokens[0].isdigit():
        process_tokens = process_tokens[1:]
    redacted_process_tokens = _redact_process_tokens(process_tokens)
    serve_process = {
        "executable": process_tokens[0] if process_tokens else runtime_profile.process_name,
        "command_sha256": sha256_payload(redacted_process_tokens),
        "flags": _selected_serve_flags(process_tokens[1:]),
    }

    backend = runtime_profile.compute_backend
    if backend in {"auto", "nvidia"}:
        try:
            nvidia = _nvidia_attestation(runner, timeout)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            if backend == "nvidia":
                raise
        else:
            return {
                "host": runtime_profile.host or "local",
                "kernel": kernel,
                "backend": "nvidia",
                "devices": nvidia["devices"],
                "driver_runtime": nvidia["driver_runtime"],
                "serve_process": serve_process,
            }
    vulkan = _vulkan_attestation(runner, timeout)
    return {
        "host": runtime_profile.host or "local",
        "kernel": kernel,
        "backend": "vulkan",
        "devices": vulkan["devices"],
        "driver_runtime": vulkan["driver_runtime"],
        "serve_process": serve_process,
    }


def _nvidia_attestation(runner: CommandRunner, timeout: float) -> dict[str, Any]:
    query = runner(
        [
            "nvidia-smi",
            "--query-gpu=name,uuid,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout,
    )
    devices = []
    for line in query.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 4:
            raise ValueError("unexpected nvidia-smi query output")
        devices.append(
            {
                "name": fields[0],
                "uuid": fields[1],
                "driver_version": fields[2],
                "memory_mib": int(fields[3]),
            }
        )
    if not devices:
        raise ValueError("nvidia-smi reported no GPU devices")
    summary = runner(["nvidia-smi"], timeout)
    runtime_match = re.search(r"(?:CUDA|CUDA UMD) Version:\s*([0-9.]+)", summary)
    if runtime_match is None:
        raise ValueError("nvidia-smi did not report a CUDA runtime")
    return {
        "devices": devices,
        "driver_runtime": {
            "driver": devices[0]["driver_version"],
            "runtime": f"CUDA {runtime_match.group(1)}",
        },
    }


def _vulkan_attestation(runner: CommandRunner, timeout: float) -> dict[str, Any]:
    summary = runner(["vulkaninfo", "--summary"], timeout)
    instance_match = re.search(r"Vulkan Instance Version:\s*(\S+)", summary)
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"GPU\d+:", line):
            if current and current.get("deviceType") != "PHYSICAL_DEVICE_TYPE_CPU":
                devices.append(current)
            current = {}
            continue
        if current is None or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in _VULKAN_FIELDS:
            current[key] = value
    if current and current.get("deviceType") != "PHYSICAL_DEVICE_TYPE_CPU":
        devices.append(current)
    if instance_match is None or not devices:
        raise ValueError("vulkaninfo did not report a physical GPU and driver runtime")
    return {
        "devices": devices,
        "driver_runtime": {
            "driver": devices[0].get("driverInfo", devices[0].get("driverVersion", "unknown")),
            "runtime": f"Vulkan {instance_match.group(1)}",
        },
    }


def _run_host_command(host: str | None) -> CommandRunner:
    def run(command: Sequence[str], timeout: float) -> str:
        argv = list(command)
        if host is not None:
            argv = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={max(1, int(timeout))}",
                host,
                *argv,
            ]
        result = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout

    return run


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=min(timeout, 30)) as response:  # noqa: S310 - configured endpoint
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"runtime attestation endpoint returned non-object JSON: {url}")
    return payload


def _props_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/props", "", ""))


def _redacted_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _process_matches(line: str, process_name: str) -> bool:
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError:
        return False
    if tokens and tokens[0].isdigit():
        tokens = tokens[1:]
    return bool(tokens) and Path(tokens[0]).name == process_name


def _selected_serve_flags(arguments: list[str]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item in _SERVE_BOOLEAN_FLAGS:
            selected[item] = True
        elif item in _SERVE_FLAGS_WITH_VALUES:
            if index + 1 >= len(arguments):
                raise ValueError(f"serve flag {item} is missing its value")
            selected[item] = arguments[index + 1]
            index += 1
        index += 1
    return selected


def _redact_process_tokens(tokens: list[str]) -> list[str]:
    redacted = list(tokens)
    index = 0
    while index < len(redacted):
        item = redacted[index]
        if item in _SENSITIVE_SERVE_FLAGS and index + 1 < len(redacted):
            redacted[index + 1] = "[REDACTED]"
            index += 1
        elif any(item.startswith(f"{flag}=") for flag in _SENSITIVE_SERVE_FLAGS):
            redacted[index] = f"{item.split('=', 1)[0]}=[REDACTED]"
        index += 1
    return redacted


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"runtime attestation field {key} is missing")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"runtime attestation field {key} is missing")
    return value


def _hash_json_value(value: Any) -> str:
    if value is None:
        raise ValueError("runtime attestation tokenizer token metadata is missing")
    return sha256_payload(value)
