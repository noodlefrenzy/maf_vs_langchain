import json
import stat
from pathlib import Path

from agent_lab.contracts import (
    Budget,
    EventKind,
    FrameworkLane,
    ModelProfile,
    ResourceMetricsProfile,
    RunManifest,
    RunTerminal,
    TerminalStatus,
)
from agent_lab.run_bundle import RunBundle
from agent_lab.run_metrics import (
    CONTROL_METRICS_SCHEMA,
    MODEL_METRICS_SCHEMA,
    RESOURCE_METRICS_SCHEMA,
    ControlPhaseTimeline,
    ResourceMetricsSampler,
    write_model_metrics,
)


def _manifest() -> RunManifest:
    return RunManifest(
        experiment_id="metrics_test",
        lane=FrameworkLane.MAF_WORKFLOW,
        scenario="incident_response",
        scenario_generator_version="0.1.0",
        scenario_seed=42,
        repetition=0,
        model=ModelProfile(
            name="local-test",
            base_url="http://127.0.0.1:8000/v1",
            temperature=0.2,
            top_p=0.9,
            max_output_tokens=100,
            context_window_tokens=8192,
            request_timeout_seconds=30,
        ),
        budget=Budget(
            wall_clock_seconds=60,
            model_calls=4,
            tool_calls=8,
            output_tokens=400,
            approval_requests=1,
        ),
        source_commit="0" * 40,
        source_tree_sha256="1" * 64,
        framework_versions={"test": "1"},
        dependency_lock_sha256="2" * 64,
        prompt_bundle_sha256="3" * 64,
        tool_catalog_sha256="4" * 64,
        effective_request_policy_sha256="5" * 64,
        execution_provenance_sha256="6" * 64,
        world_manifest_sha256="7" * 64,
    )


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_resource_sampler_records_fixed_cadence_identity_and_overhead(tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "host.jsonl"
    sampler = ResourceMetricsSampler(
        path=path,
        run_id=manifest.run_id,
        profile=ResourceMetricsProfile(enabled=True, interval_seconds=0.1),
        gpu_reader=lambda: {
            "status": "collected",
            "reason": None,
            "devices": [
                {
                    "index": 0,
                    "uuid": "GPU-test",
                    "memory_used_mib": 100.0,
                    "memory_total_mib": 1000.0,
                    "utilization_percent": 25.0,
                    "power_watts": 100.0,
                    "temperature_celsius": 45.0,
                }
            ],
        },
    )

    sampler.start()
    sampler.stop()

    records = _records(path)
    samples = [record for record in records if record["record_type"] == "resource_sample"]
    assert records[0]["schema_version"] == RESOURCE_METRICS_SCHEMA
    assert records[0]["interval_seconds"] == 0.1
    assert len(samples) == records[-1]["sample_count"] == 2
    assert all(sample["sampling_overhead_ns"] >= 0 for sample in samples)
    assert samples[0]["host"]["memory_used_mib"] > 0
    assert samples[0]["gpu"]["devices"][0]["power_watts"] == 100.0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_disabled_resource_sampler_records_explicit_absence(tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "host.jsonl"
    sampler = ResourceMetricsSampler(
        path=path,
        run_id=manifest.run_id,
        profile=ResourceMetricsProfile(enabled=False),
    )

    sampler.start()
    sampler.stop()

    records = _records(path)
    assert [record["record_type"] for record in records] == [
        "sampling_configuration",
        "sampling_summary",
    ]
    assert records[-1]["enabled"] is False
    assert records[-1]["sample_count"] == 0


def test_model_metrics_preserve_optional_usage_and_reconcile_terminal(tmp_path: Path) -> None:
    manifest = _manifest()
    bundle = RunBundle.create(tmp_path, manifest)
    bundle.emit(EventKind.RUN_STARTED)
    requested = bundle.emit(
        EventKind.MODEL_REQUESTED,
        actor="commander",
        payload={"model": "local-test"},
    )
    bundle.emit(
        EventKind.MODEL_RESPONDED,
        actor="commander",
        parent_event_id=requested.event_id,
        payload={
            "model": "local-test",
            "input_tokens": 12,
            "output_tokens": 3,
            "cached_input_tokens": 5,
            "reasoning_tokens": 2,
            "provider_request_id": "chatcmpl-test",
            "queue_seconds": 0.001,
            "time_to_first_token_seconds": 0.02,
            "generation_seconds": 0.03,
        },
    )
    bundle.emit(
        EventKind.METRIC_RECORDED,
        actor="commander",
        payload={
            "name": "model_token_usage",
            "input_tokens": 12,
            "output_tokens": 3,
        },
    )
    bundle.emit(EventKind.RUN_FINISHED, payload={"status": "completed"})
    terminal = RunTerminal(
        run_id=manifest.run_id,
        status=TerminalStatus.COMPLETED,
        reason="test_completed",
        used_model_calls=1,
        used_tool_calls=0,
        used_output_tokens=3,
        elapsed_seconds=0.1,
    )

    path = bundle.path / "metrics" / "model.jsonl"
    write_model_metrics(path, bundle.path / "events.jsonl", terminal)

    call, summary = _records(path)
    assert call["schema_version"] == MODEL_METRICS_SCHEMA
    assert call["tokens"] == {
        "input": 12,
        "output": 3,
        "cached_input": 5,
        "reasoning": 2,
        "availability": {
            "input": "reported",
            "output": "reported",
            "cached_input": "reported",
            "reasoning": "reported",
        },
    }
    assert call["total_latency_seconds"] >= 0
    assert call["provider_request_id"] == "chatcmpl-test"
    assert call["queue_seconds"] == 0.001
    assert call["time_to_first_token_seconds"] == 0.02
    assert call["generation_seconds"] == 0.03
    assert set(call["timing_availability"].values()) == {"derived", "provider_reported"}
    assert summary["task_latency_seconds"] >= call["total_latency_seconds"]
    assert summary["reconciliation_status"] == "exact"


def test_control_timeline_emits_every_versioned_phase(tmp_path: Path) -> None:
    manifest = _manifest()
    timeline = ControlPhaseTimeline(run_id=manifest.run_id)
    timeline.points = {
        "control_started": 1,
        "launch_started": 2,
        "launch_finished": 3,
        "task_started": 4,
        "task_finished": 5,
        "finalization_started": 5,
        "finalization_finished": 6,
        "tracing_flush_started": 6,
        "tracing_flush_finished": 7,
        "teardown_started": 7,
        "teardown_finished": 8,
        "control_finished": 9,
    }
    path = tmp_path / "control.jsonl"

    timeline.write(path)

    records = _records(path)
    assert len(records) == 7
    assert all(record["schema_version"] == CONTROL_METRICS_SCHEMA for record in records)
    assert all(record["status"] == "collected" for record in records)
    assert {record["phase"] for record in records} == {
        "subprocess_launch",
        "preparation",
        "task_execution",
        "finalization",
        "tracing_flush",
        "teardown",
        "control_plane_total",
    }
