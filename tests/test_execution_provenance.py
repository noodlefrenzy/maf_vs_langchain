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
