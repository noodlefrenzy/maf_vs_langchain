import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agent_lab.bundle_validation import validate_bundle
from agent_lab.contract_bundle import contract_bundle_hashes, sha256_payload
from agent_lab.contracts import Budget, FrameworkLane, ModelProfile
from agent_lab.matched_contracts import normalized_topology_for_lane, preflight_pair_contract
from agent_lab.runner_host import NativeRunnerDescriptor
from agent_lab.runner_protocol import CheckpointCapability

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _experiment(path: Path) -> Path:
    model_profile = REPOSITORY_ROOT / "configs/models/qwen35_9b_vllm.toml"
    path.write_text(
        "\n".join(
            [
                'schema_version = "0.1.0"',
                'experiment_id = "control_probe_test"',
                'scenario = "incident_response"',
                'lanes = ["maf_workflow", "langgraph"]',
                "seeds = [42]",
                "repetitions = 1",
                'order = "randomized"',
                f"model_profile = {json.dumps(str(model_profile))}",
                'fault_profile = "none"',
                "capture_sensitive = false",
                "capture_reasoning = false",
                "",
                "[budget]",
                "wall_clock_seconds = 10",
                "model_calls = 2",
                "tool_calls = 4",
                "output_tokens = 100",
                "approval_requests = 1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _run_probe(
    experiment: Path,
    runs_dir: Path,
    *,
    order_seed: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.experiment_control",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment",
            str(experiment),
            "--runs-dir",
            str(runs_dir),
            "--order-seed",
            str(order_seed),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_paired_native_probe_uses_equal_isolated_worlds(tmp_path: Path) -> None:
    experiment = _experiment(tmp_path / "experiment.toml")
    runs_dir = tmp_path / "runs"

    result = _run_probe(experiment, runs_dir, order_seed=731)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["status"] == "ok"
    assert summary["execution_mode"] == "protocol_probe"
    assert len(summary["runs"]) == 2
    assert {run["lane"] for run in summary["runs"]} == {
        "maf_workflow",
        "langgraph",
    }
    assert len({run["world_manifest_sha256"] for run in summary["runs"]}) == 1
    assert len({run["run_id"] for run in summary["runs"]}) == 2
    assert len({run["bundle"] for run in summary["runs"]}) == 2
    preflight = summary["blocks"][0]["contract_preflights"]["explicit_workflow"]
    snapshot = {key: value for key, value in preflight.items() if key != "snapshot_sha256"}
    assert preflight["snapshot_sha256"] == sha256_payload(snapshot)
    assert preflight["normalized_topology"] == normalized_topology_for_lane(
        FrameworkLane.MAF_WORKFLOW
    )
    assert preflight["held_constants"]["budget"] == {
        "wall_clock_seconds": 10,
        "model_calls": 2,
        "tool_calls": 4,
        "output_tokens": 100,
        "approval_requests": 1,
    }
    assert preflight["role_prompts_sha256"] == contract_bundle_hashes()["prompt_bundle_sha256"]
    assert preflight["tool_schemas_sha256"] == contract_bundle_hashes()["tool_catalog_sha256"]
    assert (
        preflight["completion_schema_sha256"] == contract_bundle_hashes()["artifact_schemas_sha256"]
    )
    for run in summary["runs"]:
        bundle = Path(run["bundle"])
        checkpoint = Path(run["world_checkpoint"])
        assert checkpoint.is_file()
        assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
        assert json.loads((bundle / "terminal.json").read_text())["status"] == "completed"
        assert run["native_trace"]["span_count"] >= 1
        assert run["native_trace"]["completeness_ratio"] == 1.0
        assert run["native_trace"]["phoenix_trace_url"].startswith(
            "http://127.0.0.1:6006/redirects/traces/"
        )
        assert run["contract_preflight_sha256"] == preflight["snapshot_sha256"]
        assert (
            json.loads((bundle / "native" / "pair-contract-preflight.json").read_text())
            == preflight
        )
        request = (bundle / "native" / "run-request.redacted.json").read_text()
        assert '"token": "[REDACTED]"' in request
        assert "v1." not in request
        validation = validate_bundle(bundle)
        assert validation.valid is False
        assert validation.inference_status == "invalid_provenance"
        assert {finding.code for finding in validation.findings} >= {
            "scores_missing",
            "completed_artifacts_missing",
        }
    assert Path(summary["index"]).is_file()


@pytest.mark.parametrize("drift", ["contract_hashes", "normalized_topology"])
def test_pair_contract_preflight_fails_closed_on_runner_drift(drift: str) -> None:
    hashes = contract_bundle_hashes()
    lanes = (FrameworkLane.MAF_WORKFLOW, FrameworkLane.LANGGRAPH)
    descriptors = {
        lane: NativeRunnerDescriptor(
            name=f"{lane.value}-test-runner",
            supported_lanes=(lane,),
            framework_versions={lane.value: "test"},
            contract_hashes=hashes,
            topology={lane: {"kind": "native-test"}},
            normalized_topology={lane: normalized_topology_for_lane(lane)},
            checkpoint={
                lane: CheckpointCapability(
                    resumable=True,
                    restart_safe=True,
                    format="test",
                )
            },
        )
        for lane in lanes
    }
    right = descriptors[FrameworkLane.LANGGRAPH]
    if drift == "contract_hashes":
        changed_hashes = {**hashes, "tool_catalog_sha256": "f" * 64}
        descriptors[FrameworkLane.LANGGRAPH] = right.model_copy(
            update={"contract_hashes": changed_hashes}
        )
    else:
        descriptors[FrameworkLane.LANGGRAPH] = right.model_copy(
            update={"normalized_topology": {FrameworkLane.LANGGRAPH: {"kind": "drifted-topology"}}}
        )

    with pytest.raises(ValueError, match="runner (contract hashes|normalized topology) differ"):
        preflight_pair_contract(
            track="explicit_workflow",
            left_lane=FrameworkLane.MAF_WORKFLOW,
            right_lane=FrameworkLane.LANGGRAPH,
            descriptors=descriptors,
            expected_contract_hashes=hashes,
            experiment_id="preflight_test",
            scenario="incident_response",
            scenario_seed=42,
            repetition=0,
            model=ModelProfile(
                name="local-test",
                base_url="http://127.0.0.1:8000/v1",
                temperature=0.2,
                top_p=0.9,
                max_output_tokens=1000,
                context_window_tokens=8192,
                request_timeout_seconds=60,
            ),
            budget=Budget(
                wall_clock_seconds=10,
                model_calls=2,
                tool_calls=4,
                output_tokens=100,
                approval_requests=1,
            ),
            fault_profile="none",
            source_commit="0" * 40,
            source_tree_sha256="1" * 64,
            world_manifest_sha256="2" * 64,
        )


def test_protocol_probe_refuses_to_overwrite_index(tmp_path: Path) -> None:
    experiment = _experiment(tmp_path / "experiment.toml")
    runs_dir = tmp_path / "runs"
    first = _run_probe(experiment, runs_dir, order_seed=991)
    assert first.returncode == 0, first.stderr

    second = _run_probe(experiment, runs_dir, order_seed=991)
    assert second.returncode != 0
    assert "already exists" in second.stderr


def test_native_workflow_mode_requires_one_matched_track(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.experiment_control",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment",
            str(REPOSITORY_ROOT / "configs/experiments/smoke.toml"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--mode",
            "native_workflow",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "requires exactly one matched track" in result.stderr


@pytest.mark.skipif(
    os.getenv("AGENT_LAB_RUN_LIVE_MODEL_TESTS") != "1",
    reason="requires an explicit tunnel to the aligned lonestar model",
)
def test_live_native_workflow_experiment_writes_valid_scored_pair(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.experiment_control",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment",
            str(REPOSITORY_ROOT / "configs/experiments/explicit-smoke.toml"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--order-seed",
            "1729",
            "--mode",
            "native_workflow",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "ok"
    assert summary["execution_mode"] == "native_workflow"
    assert summary["blocks"][0]["pair_validation"] == {
        "inference_status": "eligible",
        "track": "explicit_workflow",
        "valid": True,
    }
    assert {item["lane"] for item in summary["runs"]} == {
        "maf_workflow",
        "langgraph",
    }
    assert all(item["bundle_valid"] for item in summary["runs"])
    assert all(Path(item["bundle"]).joinpath("scores.json").is_file() for item in summary["runs"])


@pytest.mark.skipif(
    os.getenv("AGENT_LAB_RUN_LIVE_MODEL_TESTS") != "1",
    reason="requires an explicit tunnel to the aligned lonestar model",
)
def test_live_native_harness_experiment_writes_valid_scored_pair(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.experiment_control",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment",
            str(REPOSITORY_ROOT / "configs/experiments/harness-smoke.toml"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--order-seed",
            "1737",
            "--mode",
            "native_workflow",
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "ok"
    assert summary["execution_mode"] == "native_workflow"
    assert summary["blocks"][0]["pair_validation"] == {
        "inference_status": "eligible",
        "track": "opinionated_harness",
        "valid": True,
    }
    assert {item["lane"] for item in summary["runs"]} == {
        "maf_harness",
        "deep_agents",
    }
    assert all(item["bundle_valid"] for item in summary["runs"])
    assert all(Path(item["bundle"]).joinpath("scores.json").is_file() for item in summary["runs"])
