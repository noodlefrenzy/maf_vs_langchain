"""Measured-run source, model-server, and compute-runtime attestation."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

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
    "--cpu-offload-gb",
    "--host",
    "--model",
    "--n-gpu-layers",
    "--parallel",
    "--port",
    "--reasoning-format",
    "--reasoning-parser",
    "--revision",
    "--served-model-name",
    "--tokenizer",
    "--tokenizer-revision",
    "--tool-call-parser",
    "--max-model-len",
    "--generation-config",
    "--dtype",
    "--quantization",
    "--tensor-parallel-size",
    "--pipeline-parallel-size",
    "--data-parallel-size",
    "--gpu-memory-utilization",
    "--max-num-seqs",
    "--max-num-batched-tokens",
    "--load-format",
    "--seed",
}
_SERVE_BOOLEAN_FLAGS = {
    "--enable-auto-tool-choice",
    "--enable-chunked-prefill",
    "--enable-force-include-usage",
    "--enable-per-request-metrics",
    "--enable-prefix-caching",
    "--enable-prompt-tokens-details",
    "--enable-tokenizer-info-endpoint",
    "--jinja",
    "--language-model-only",
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
    """Collect fail-closed endpoint and serving-host identity."""

    if runtime_profile.server_kind == "vllm":
        return _attest_vllm_runtime(
            model,
            runtime_profile,
            command_runner=command_runner,
        )
    return _attest_llama_cpp_runtime(
        model,
        runtime_profile,
        command_runner=command_runner,
    )


def _attest_llama_cpp_runtime(
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
        "endpoint_identity_sha256": {
            "props": sha256_payload(_stable_props_identity(props)),
            "models": sha256_payload(_stable_models_identity(models)),
        },
    }
    return {**attestation, "attestation_sha256": sha256_payload(attestation)}


def _attest_vllm_runtime(
    model: ModelProfile,
    runtime_profile: InferenceRuntimeProfile,
    *,
    command_runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Collect fail-closed vLLM endpoint, tokenizer, parser, and compute identity."""

    root_url = _server_root_url(model.base_url)
    models = _fetch_json(f"{model.base_url.rstrip('/')}/models", model.request_timeout_seconds)
    data = models.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("model endpoint must expose exactly one OpenAI model")
    served = data[0]
    served_id = served.get("id")
    if served_id != model.name:
        raise ValueError(f"requested model {model.name!r} differs from served model {served_id!r}")

    version_payload = _fetch_json(f"{root_url}/version", model.request_timeout_seconds)
    version = _required_string(version_payload, "version")
    tokenizer_info = _fetch_json(
        f"{root_url}/tokenizer_info",
        model.request_timeout_seconds,
    )
    chat_template = _required_string(tokenizer_info, "chat_template")
    tokenized = _post_json(
        f"{root_url}/tokenize",
        {
            "model": model.name,
            "messages": [{"role": "user", "content": "attest"}],
            "add_generation_prompt": True,
        },
        model.request_timeout_seconds,
    )
    max_model_len = _required_int(tokenized, "max_model_len")
    if model.context_window_tokens > max_model_len:
        raise ValueError(
            f"requested context window {model.context_window_tokens} exceeds served {max_model_len}"
        )

    runner = command_runner or _run_host_command(runtime_profile.host)
    host_runtime = _attest_compute_runtime(runtime_profile, runner)
    serve_process = host_runtime.pop("serve_process")
    flags = serve_process["flags"]
    revision = _required_revision_flag(flags, "--revision")
    tokenizer_revision = _required_revision_flag(flags, "--tokenizer-revision")
    _require_vllm_serve_contract(
        flags,
        model,
        max_model_len,
        containerized=runtime_profile.container_name is not None,
    )
    if runtime_profile.container_name is not None:
        published_ports = serve_process["container"]["published_ports"]
        if not _container_publishes_endpoint_on_loopback(published_ports, model.base_url):
            raise ValueError("vLLM container must publish the configured endpoint only on loopback")

    tokenizer = {
        "revision": tokenizer_revision,
        "class": _required_string(tokenizer_info, "tokenizer_class"),
        "config_sha256": sha256_payload(tokenizer_info),
    }
    artifact = {
        "model_id": served_id,
        "revision": revision,
        "format": "safetensors",
        "load_format": flags["--load-format"],
        "quantization": flags.get("--quantization"),
    }
    parser = {
        "reasoning": flags["--reasoning-parser"],
        "tool_call": flags["--tool-call-parser"],
        "auto_tool_choice": flags.get("--enable-auto-tool-choice") is True,
    }
    attestation = {
        "collection_status": "complete",
        "measurement_eligible": True,
        "endpoint": _redacted_endpoint(model.base_url),
        "artifact": artifact,
        "tokenizer": {**tokenizer, "tokenizer_sha256": sha256_payload(tokenizer)},
        "chat_template": {"sha256": hashlib.sha256(chat_template.encode()).hexdigest()},
        "server": {
            "kind": "vllm",
            "build": version,
            "parser": parser,
            "endpoint_flags": {"context_window_tokens": max_model_len},
            "serve_process": serve_process,
        },
        "compute_runtime": host_runtime,
        "endpoint_identity_sha256": {
            "models": sha256_payload(_stable_models_identity(models)),
            "version": sha256_payload(version_payload),
            "tokenizer": sha256_payload(tokenizer_info),
            "tokenize_contract": sha256_payload(
                {"max_model_len": max_model_len, "model": model.name}
            ),
        },
    }
    return {**attestation, "attestation_sha256": sha256_payload(attestation)}


def _attest_compute_runtime(
    runtime_profile: InferenceRuntimeProfile,
    runner: CommandRunner,
) -> dict[str, Any]:
    timeout = runtime_profile.command_timeout_seconds
    kernel = runner(["uname", "-a"], timeout).strip()
    if runtime_profile.container_name is not None:
        serve_process, server_runtime = _attest_container_process(runtime_profile, runner, timeout)
    else:
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
            process_tokens.pop(0)
        redacted_process_tokens = _redact_process_tokens(process_tokens)
        serve_process = {
            "executable": process_tokens[0] if process_tokens else runtime_profile.process_name,
            "command_sha256": sha256_payload(redacted_process_tokens),
            "flags": _selected_serve_flags(process_tokens[1:]),
        }
        server_runtime = None

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
                "server_runtime": server_runtime,
                "serve_process": serve_process,
            }
    vulkan = _vulkan_attestation(runner, timeout)
    return {
        "host": runtime_profile.host or "local",
        "kernel": kernel,
        "backend": "vulkan",
        "devices": vulkan["devices"],
        "driver_runtime": vulkan["driver_runtime"],
        "server_runtime": server_runtime,
        "serve_process": serve_process,
    }


def _attest_container_process(
    runtime_profile: InferenceRuntimeProfile,
    runner: CommandRunner,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    container_name = runtime_profile.container_name
    expected_digest = runtime_profile.container_image_digest
    assert container_name is not None and expected_digest is not None
    inspected = json.loads(runner(["docker", "inspect", container_name], timeout))
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise ValueError("docker inspect did not return exactly one container")
    container = inspected[0]
    state = container.get("State")
    config = container.get("Config")
    network = container.get("NetworkSettings")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise ValueError("vLLM container is not running")
    if not isinstance(config, dict) or not isinstance(network, dict):
        raise ValueError("vLLM container inspect metadata is incomplete")
    image_reference = config.get("Image")
    if not isinstance(image_reference, str) or not image_reference.endswith(f"@{expected_digest}"):
        raise ValueError("running vLLM container does not use the pinned image manifest digest")
    image_id = container.get("Image")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None:
        raise ValueError("vLLM container image content digest is missing")
    process_path = container.get("Path")
    arguments = container.get("Args")
    if (
        not isinstance(process_path, str)
        or not isinstance(arguments, list)
        or not all(isinstance(item, str) for item in arguments)
    ):
        raise ValueError("vLLM container process command is incomplete")
    process_tokens = [process_path, *arguments]
    redacted = _redact_process_tokens(process_tokens)
    serve_flags = _selected_serve_flags(arguments)
    uses_bitsandbytes = serve_flags.get("--quantization") == "bitsandbytes"
    ports = network.get("Ports")
    if not isinstance(ports, dict):
        raise ValueError("vLLM container published-port metadata is missing")
    published_ports = {
        key: value
        for key, value in ports.items()
        if isinstance(key, str) and isinstance(value, list)
    }
    bitsandbytes_import = ",bitsandbytes" if uses_bitsandbytes else ""
    bitsandbytes_field = ",'bitsandbytes':bitsandbytes.__version__" if uses_bitsandbytes else ""
    stack_command = [
        "docker",
        "exec",
        container_name,
        "/usr/bin/python3",
        "-c",
        (
            f"import json,platform,torch,vllm{bitsandbytes_import};"
            "print(json.dumps({'python':platform.python_version(),"
            "'pytorch':torch.__version__,'cuda':torch.version.cuda,"
            f"'vllm':vllm.__version__{bitsandbytes_field}"
            "},sort_keys=True))"
        ),
    ]
    stack = json.loads(runner(stack_command, timeout))
    required_stack_keys = ("python", "pytorch", "cuda", "vllm") + (
        ("bitsandbytes",) if uses_bitsandbytes else ()
    )
    if not isinstance(stack, dict) or not all(
        isinstance(stack.get(key), str) and stack[key] for key in required_stack_keys
    ):
        raise ValueError("vLLM container Python/CUDA runtime metadata is incomplete")
    serve_process = {
        "executable": process_path,
        "command_sha256": sha256_payload(redacted),
        "flags": serve_flags,
        "container": {
            "name": container_name,
            "image_reference": image_reference,
            "image_manifest_digest": expected_digest,
            "image_content_digest": image_id,
            "published_ports": published_ports,
        },
    }
    return serve_process, {key: str(value) for key, value in stack.items()}


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
        elif argv and argv[0] == "nvidia-smi" and shutil.which("nvidia-smi") is None:
            wsl_nvidia_smi = Path("/usr/lib/wsl/lib/nvidia-smi")
            if wsl_nvidia_smi.is_file():
                argv[0] = str(wsl_nvidia_smi)
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


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=min(timeout, 30)) as response:  # noqa: S310 - configured endpoint
        body = json.load(response)
    if not isinstance(body, dict):
        raise ValueError(f"runtime attestation endpoint returned non-object JSON: {url}")
    return body


def _props_url(base_url: str) -> str:
    return f"{_server_root_url(base_url)}/props"


def _server_root_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _required_revision_flag(flags: dict[str, Any], key: str) -> str:
    value = flags.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{40}", value) is None:
        raise ValueError(f"vLLM serve flag {key} must pin a 40-character commit revision")
    return value


def _require_vllm_serve_contract(
    flags: dict[str, Any],
    model: ModelProfile,
    max_model_len: int,
    *,
    containerized: bool,
) -> None:
    endpoint_port = urlsplit(model.base_url).port
    if endpoint_port is None:
        raise ValueError("vLLM model endpoint must declare an explicit port")
    required_values = {
        "--dtype": "bfloat16",
        "--served-model-name": model.name,
        "--max-model-len": str(max_model_len),
        "--max-num-seqs": "2",
        "--port": str(endpoint_port),
        "--generation-config": "vllm",
        "--tensor-parallel-size": "1",
        "--tokenizer": model.name,
    }
    if model.seed is not None:
        required_values["--seed"] = str(model.seed)
    if model.name == "Qwen/Qwen3.5-9B":
        required_values.update(
            {
                "--reasoning-parser": "qwen3",
                "--tool-call-parser": "qwen3_coder",
                "--load-format": "bitsandbytes",
                "--quantization": "bitsandbytes",
                "--max-num-batched-tokens": str(min(max_model_len, 8192)),
            }
        )
    elif model.name == "Qwen/Qwen3.5-35B-A3B-GPTQ-Int4":
        required_values.update(
            {
                "--reasoning-parser": "qwen3",
                "--tool-call-parser": "qwen3_coder",
                "--load-format": "safetensors",
                "--quantization": "moe_wna16",
                "--max-num-batched-tokens": str(min(max_model_len, 8192)),
                "--cpu-offload-gb": "8",
            }
        )
    else:
        required_values.update(
            {
                "--load-format": "safetensors",
                "--max-num-batched-tokens": str(max_model_len),
            }
        )
        for key in ("--reasoning-parser", "--tool-call-parser"):
            if not isinstance(flags.get(key), str) or not flags[key]:
                raise ValueError(f"vLLM serve contract requires an explicit {key}")
    for key, expected in required_values.items():
        if flags.get(key) != expected:
            raise ValueError(f"vLLM serve flag {key} must be {expected!r}")
    host = flags.get("--host")
    allowed_hosts = {"0.0.0.0", "::"} if containerized else {"127.0.0.1", "localhost", "::1"}
    if host not in allowed_hosts:
        raise ValueError("vLLM must bind its serving endpoint to loopback")
    try:
        gpu_memory_utilization = float(flags["--gpu-memory-utilization"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("vLLM serve contract requires --gpu-memory-utilization") from exc
    if not 0 < gpu_memory_utilization <= 1:
        raise ValueError("vLLM --gpu-memory-utilization must be in (0, 1]")
    if model.name == "Qwen/Qwen3.5-9B" and gpu_memory_utilization != 0.75:
        raise ValueError("Qwen3.5-9B vLLM --gpu-memory-utilization must be 0.75")
    if model.name == "Qwen/Qwen3.5-35B-A3B-GPTQ-Int4" and gpu_memory_utilization != 0.9:
        raise ValueError("Qwen3.5-35B-A3B vLLM --gpu-memory-utilization must be 0.9")
    for key in (
        "--enable-auto-tool-choice",
        "--enable-chunked-prefill",
        "--enable-force-include-usage",
        "--enable-per-request-metrics",
        "--enable-prefix-caching",
        "--enable-prompt-tokens-details",
        "--enable-tokenizer-info-endpoint",
        "--language-model-only",
    ):
        if flags.get(key) is not True:
            raise ValueError(f"vLLM serve contract requires {key}")


def _container_publishes_endpoint_on_loopback(
    published_ports: dict[str, Any], base_url: str
) -> bool:
    endpoint = urlsplit(base_url)
    port = endpoint.port
    if port is None:
        return False
    bindings = published_ports.get(f"{port}/tcp")
    if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], dict):
        return False
    binding = bindings[0]
    return binding.get("HostIp") in {"127.0.0.1", "::1"} and binding.get("HostPort") == str(port)


def _stable_props_identity(props: dict[str, Any]) -> dict[str, Any]:
    """Exclude live server state that is not an inference input."""

    return {key: value for key, value in props.items() if key != "is_sleeping"}


def _stable_models_identity(models: dict[str, Any]) -> dict[str, Any]:
    """Exclude response-local metadata that is not an inference input."""

    normalized = json.loads(json.dumps(models))
    for item in normalized.get("data", []):
        if isinstance(item, dict):
            item.pop("created", None)
            permissions = item.get("permission", [])
            if isinstance(permissions, list):
                for permission in permissions:
                    if isinstance(permission, dict):
                        permission.pop("id", None)
                        permission.pop("created", None)
    return normalized


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
