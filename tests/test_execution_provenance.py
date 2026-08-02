import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_lab.contracts import InferenceRuntimeProfile, ModelProfile
from agent_lab.execution_provenance import (
    attest_model_runtime,
    attest_source_worktree,
    build_execution_provenance,
)


def _profile() -> ModelProfile:
    return ModelProfile(
        name="org/model:Q4",
        base_url="http://127.0.0.1:8000/v1",
        temperature=0.2,
        top_p=0.9,
        max_output_tokens=1024,
        context_window_tokens=4096,
        request_timeout_seconds=30,
        thinking=False,
    )


def _endpoint_payloads() -> tuple[dict[str, Any], dict[str, Any]]:
    revision = "a" * 40
    props = {
        "model_path": f"/models/snapshots/{revision}/model-q4.gguf",
        "model_alias": "org/model:Q4",
        "model_ftype": "Q4_K - Medium",
        "chat_template": "{{ messages }}",
        "chat_template_caps": {"supports_tools": True},
        "build_info": "b100-test",
        "bos_token": "<s>",
        "eos_token": "</s>",
        "total_slots": 4,
        "endpoint_metrics": False,
        "endpoint_props": False,
        "endpoint_slots": True,
        "cors_proxy_enabled": False,
        "ui": False,
        "modalities": {"vision": False},
        "default_generation_settings": {
            "params": {
                "chat_format": "Content-only",
                "reasoning_format": "none",
                "reasoning_in_content": False,
            }
        },
    }
    models = {
        "data": [
            {
                "id": "org/model:Q4",
                "created": 1,
                "aliases": ["org/model:Q4"],
                "meta": {
                    "vocab_type": 2,
                    "n_vocab": 151936,
                    "n_ctx": 8192,
                    "n_ctx_train": 32768,
                    "n_params": 9_000_000_000,
                    "size": 5_000_000_000,
                },
            }
        ]
    }
    return props, models


def _command_runner(command: list[str] | tuple[str, ...], _timeout: float) -> str:
    if command[:2] == ["uname", "-a"]:
        return "Linux inference-host 7.0 x86_64\n"
    if command[:3] == ["ps", "-eo", "pid=,args="]:
        return (
            "123 /opt/llama-server -hf org/model:Q4 --ctx-size 8192 --jinja "
            "-ngl 99 --host 127.0.0.1\n"
        )
    if command[:2] == ["vulkaninfo", "--summary"]:
        return """Vulkan Instance Version: 1.4.341
GPU0:
    apiVersion = 1.4.335
    driverVersion = 26.0.3
    vendorID = 0x1002
    deviceID = 0x1586
    deviceType = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
    deviceName = Radeon 8060S Graphics
    driverID = DRIVER_ID_MESA_RADV
    driverName = radv
    driverInfo = Mesa 26.0.3
    deviceUUID = abc
    driverUUID = def
GPU1:
    deviceType = PHYSICAL_DEVICE_TYPE_CPU
    deviceName = llvmpipe
"""
    raise AssertionError(f"unexpected command: {command}")


def _vllm_command_runner(command: list[str] | tuple[str, ...], _timeout: float) -> str:
    revision = "b" * 40
    if command[:2] == ["uname", "-a"]:
        return "Linux tower 7.0 x86_64\n"
    if command[:3] == ["ps", "-eo", "pid=,args="]:
        return (
            "456 /opt/vllm/bin/vllm serve Qwen/Qwen3.5-9B "
            "--served-model-name Qwen/Qwen3.5-9B "
            f"--revision {revision} --tokenizer-revision {revision} "
            "--tokenizer Qwen/Qwen3.5-9B --max-model-len 32768 "
            "--max-num-batched-tokens 32768 --max-num-seqs 2 "
            "--host 127.0.0.1 --port 8001 --dtype bfloat16 "
            "--load-format safetensors --tensor-parallel-size 1 "
            "--gpu-memory-utilization 0.9 --seed 1729 "
            "--generation-config vllm --reasoning-parser qwen3 "
            "--tool-call-parser qwen3_coder --enable-auto-tool-choice "
            "--enable-chunked-prefill --enable-prefix-caching "
            "--enable-tokenizer-info-endpoint "
            "--enable-force-include-usage --enable-per-request-metrics "
            "--enable-prompt-tokens-details "
            "--language-model-only\n"
        )
    if command[:2] == ["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total"]:
        return "NVIDIA GeForce RTX 4090, GPU-test, 610.74, 24564\n"
    if command == ["nvidia-smi"]:
        return "NVIDIA-SMI 610.53 Driver Version: 610.74 CUDA Version: 13.3\n"
    raise AssertionError(f"unexpected command: {command}")


def _vllm_container_command_runner(command: list[str] | tuple[str, ...], _timeout: float) -> str:
    revision = "b" * 40
    image_digest = "sha256:" + "c" * 64
    if command[:2] == ["uname", "-a"]:
        return "Linux tower 7.0 x86_64\n"
    if command[:2] == ["docker", "inspect"]:
        return json.dumps(
            [
                {
                    "State": {"Running": True, "Pid": 456},
                    "Config": {"Image": f"vllm/vllm-openai@{image_digest}"},
                    "Image": "sha256:" + "d" * 64,
                    "Path": "/usr/local/bin/vllm",
                    "Args": [
                        "serve",
                        "Qwen/Qwen3.5-9B",
                        "--served-model-name",
                        "Qwen/Qwen3.5-9B",
                        "--revision",
                        revision,
                        "--tokenizer-revision",
                        revision,
                        "--tokenizer",
                        "Qwen/Qwen3.5-9B",
                        "--max-model-len",
                        "32768",
                        "--max-num-batched-tokens",
                        "32768",
                        "--max-num-seqs",
                        "2",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "8001",
                        "--dtype",
                        "bfloat16",
                        "--load-format",
                        "safetensors",
                        "--tensor-parallel-size",
                        "1",
                        "--gpu-memory-utilization",
                        "0.9",
                        "--seed",
                        "1729",
                        "--generation-config",
                        "vllm",
                        "--reasoning-parser",
                        "qwen3",
                        "--tool-call-parser",
                        "qwen3_coder",
                        "--enable-auto-tool-choice",
                        "--enable-chunked-prefill",
                        "--enable-prefix-caching",
                        "--enable-tokenizer-info-endpoint",
                        "--enable-force-include-usage",
                        "--enable-per-request-metrics",
                        "--enable-prompt-tokens-details",
                        "--language-model-only",
                    ],
                    "NetworkSettings": {
                        "Ports": {"8001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]}
                    },
                }
            ]
        )
    if command[:2] == ["docker", "exec"]:
        return json.dumps(
            {"python": "3.12.12", "pytorch": "2.10.0", "cuda": "13.0", "vllm": "0.26.dev"}
        )
    if command[:2] == ["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total"]:
        return "NVIDIA GeForce RTX 4090, GPU-test, 610.74, 24564\n"
    if command == ["nvidia-smi"]:
        return "NVIDIA-SMI 610.53 Driver Version: 610.74 CUDA Version: 13.3\n"
    raise AssertionError(f"unexpected command: {command}")


def test_model_runtime_attestation_hashes_exact_endpoint_and_compute_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    props, models = _endpoint_payloads()

    def fetch(url: str, _timeout: float) -> dict[str, Any]:
        return props if url.endswith("/props") else models

    monkeypatch.setattr("agent_lab.execution_provenance._fetch_json", fetch)
    result = attest_model_runtime(
        _profile(),
        InferenceRuntimeProfile(host="inference-host", compute_backend="vulkan"),
        command_runner=_command_runner,
    )

    assert result["collection_status"] == "complete"
    assert result["artifact"]["revision"] == "a" * 40
    assert result["tokenizer"]["vocab_size"] == 151936
    assert len(result["chat_template"]["sha256"]) == 64
    assert result["server"]["build"] == "b100-test"
    assert result["server"]["parser"]["reasoning_format"] == "none"
    assert result["server"]["serve_process"]["flags"]["--ctx-size"] == "8192"
    assert result["compute_runtime"]["backend"] == "vulkan"
    assert result["compute_runtime"]["devices"][0]["driverName"] == "radv"
    assert len(result["attestation_sha256"]) == 64


def test_model_runtime_attestation_rejects_context_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    props, models = _endpoint_payloads()
    models["data"][0]["meta"]["n_ctx"] = 2048

    def fetch(url: str, _timeout: float) -> dict[str, Any]:
        return props if url.endswith("/props") else models

    monkeypatch.setattr("agent_lab.execution_provenance._fetch_json", fetch)
    with pytest.raises(ValueError, match="context window"):
        attest_model_runtime(
            _profile(),
            InferenceRuntimeProfile(compute_backend="vulkan"),
            command_runner=_command_runner,
        )


def test_model_runtime_attestation_excludes_volatile_endpoint_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    props, models = _endpoint_payloads()
    request_count = 0

    def fetch(url: str, _timeout: float) -> dict[str, Any]:
        nonlocal request_count
        if url.endswith("/props"):
            return {**props, "is_sleeping": bool(request_count % 2)}
        request_count += 1
        models["data"][0]["created"] = request_count
        return models

    monkeypatch.setattr("agent_lab.execution_provenance._fetch_json", fetch)
    runtime = InferenceRuntimeProfile(compute_backend="vulkan")

    first = attest_model_runtime(_profile(), runtime, command_runner=_command_runner)
    second = attest_model_runtime(_profile(), runtime, command_runner=_command_runner)

    assert first["attestation_sha256"] == second["attestation_sha256"]
    assert first["endpoint_identity_sha256"] == second["endpoint_identity_sha256"]


def test_vllm_runtime_attestation_hashes_pinned_model_tokenizer_and_serve_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = {"data": [{"id": "Qwen/Qwen3.5-9B", "created": 1}]}
    tokenizer_info = {
        "tokenizer_class": "Qwen2TokenizerFast",
        "chat_template": "{% for message in messages %}{{ message.content }}{% endfor %}",
        "model_max_length": 262144,
    }

    def fetch(url: str, _timeout: float) -> dict[str, Any]:
        if url.endswith("/models"):
            return models
        if url.endswith("/version"):
            return {"version": "0.24.0+test"}
        if url.endswith("/tokenizer_info"):
            return tokenizer_info
        raise AssertionError(f"unexpected URL: {url}")

    def post(url: str, payload: dict[str, Any], _timeout: float) -> dict[str, Any]:
        assert url == "http://127.0.0.1:8001/tokenize"
        assert payload["model"] == "Qwen/Qwen3.5-9B"
        return {"count": 4, "max_model_len": 32768, "tokens": [1, 2, 3, 4]}

    monkeypatch.setattr("agent_lab.execution_provenance._fetch_json", fetch)
    monkeypatch.setattr("agent_lab.execution_provenance._post_json", post)
    profile = _profile().model_copy(
        update={
            "name": "Qwen/Qwen3.5-9B",
            "base_url": "http://127.0.0.1:8001/v1",
            "context_window_tokens": 32768,
        }
    )

    result = attest_model_runtime(
        profile,
        InferenceRuntimeProfile(
            server_kind="vllm",
            compute_backend="nvidia",
            process_name="vllm",
        ),
        command_runner=_vllm_command_runner,
    )

    assert result["artifact"]["revision"] == "b" * 40
    assert result["tokenizer"]["revision"] == "b" * 40
    assert result["server"]["kind"] == "vllm"
    assert result["server"]["build"] == "0.24.0+test"
    assert result["server"]["parser"] == {
        "reasoning": "qwen3",
        "tool_call": "qwen3_coder",
        "auto_tool_choice": True,
    }
    assert result["server"]["serve_process"]["flags"]["--port"] == "8001"
    assert result["compute_runtime"]["backend"] == "nvidia"
    assert len(result["chat_template"]["sha256"]) == 64
    assert len(result["attestation_sha256"]) == 64


def test_vllm_runtime_attestation_rejects_missing_required_serve_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_lab.execution_provenance._fetch_json",
        lambda url, _timeout: (
            {"data": [{"id": "Qwen/Qwen3.5-9B"}]}
            if url.endswith("/models")
            else {"version": "0.24.0"}
            if url.endswith("/version")
            else {"tokenizer_class": "Qwen2TokenizerFast", "chat_template": "template"}
        ),
    )
    monkeypatch.setattr(
        "agent_lab.execution_provenance._post_json",
        lambda _url, _payload, _timeout: {"count": 4, "max_model_len": 32768},
    )

    def missing_prefix_cache(command: list[str] | tuple[str, ...], timeout: float) -> str:
        return _vllm_command_runner(command, timeout).replace("--enable-prefix-caching ", "")

    profile = _profile().model_copy(
        update={
            "name": "Qwen/Qwen3.5-9B",
            "base_url": "http://127.0.0.1:8001/v1",
            "context_window_tokens": 32768,
        }
    )
    with pytest.raises(ValueError, match="enable-prefix-caching"):
        attest_model_runtime(
            profile,
            InferenceRuntimeProfile(
                server_kind="vllm",
                compute_backend="nvidia",
                process_name="vllm",
            ),
            command_runner=missing_prefix_cache,
        )


def test_vllm_container_attestation_binds_manifest_stack_and_loopback_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent_lab.execution_provenance._fetch_json",
        lambda url, _timeout: (
            {"data": [{"id": "Qwen/Qwen3.5-9B"}]}
            if url.endswith("/models")
            else {"version": "0.26.0.dev"}
            if url.endswith("/version")
            else {"tokenizer_class": "Qwen2TokenizerFast", "chat_template": "template"}
        ),
    )
    monkeypatch.setattr(
        "agent_lab.execution_provenance._post_json",
        lambda _url, _payload, _timeout: {"count": 4, "max_model_len": 32768},
    )
    profile = _profile().model_copy(
        update={
            "name": "Qwen/Qwen3.5-9B",
            "base_url": "http://127.0.0.1:8001/v1",
            "context_window_tokens": 32768,
        }
    )

    result = attest_model_runtime(
        profile,
        InferenceRuntimeProfile(
            server_kind="vllm",
            compute_backend="nvidia",
            process_name="vllm",
            container_name="agent-lab-qwen35-9b-vllm",
            container_image_digest="sha256:" + "c" * 64,
        ),
        command_runner=_vllm_container_command_runner,
    )

    container = result["server"]["serve_process"]["container"]
    assert container["image_manifest_digest"] == "sha256:" + "c" * 64
    assert container["image_content_digest"] == "sha256:" + "d" * 64
    assert container["published_ports"] == {
        "8001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]
    }
    assert result["compute_runtime"]["server_runtime"] == {
        "python": "3.12.12",
        "pytorch": "2.10.0",
        "cuda": "13.0",
        "vllm": "0.26.dev",
    }


def test_dirty_worktree_is_labeled_for_protocol_probe_and_rejected_for_measurement(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    probe = attest_source_worktree(
        tmp_path,
        execution_mode="protocol_probe",
        source_commit="0" * 40,
        source_tree_sha256="1" * 64,
    )

    assert probe["clean"] is False
    assert probe["dirty_protocol_probe"] is True
    assert probe["measurement_eligible"] is False
    assert any("dirty.txt" in entry for entry in probe["status_entries"])
    with pytest.raises(ValueError, match="clean Git worktree"):
        attest_source_worktree(
            tmp_path,
            execution_mode="native_workflow",
            source_commit="0" * 40,
            source_tree_sha256="1" * 64,
        )


def test_protocol_provenance_does_not_contact_inference_runtime(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = build_execution_provenance(
        repository_root=tmp_path,
        execution_mode="protocol_probe",
        source_commit="0" * 40,
        source_tree_sha256="1" * 64,
        model=_profile(),
        runtime_profile=InferenceRuntimeProfile(),
        command_runner=lambda _command, _timeout: pytest.fail("runtime command was called"),
    )

    assert result["model_runtime"]["collection_status"] == "not_collected_protocol_probe"
    assert len(result["provenance_sha256"]) == 64
    json.dumps(result)
