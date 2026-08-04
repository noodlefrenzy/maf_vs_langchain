from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from agent_lab.bundle_validation import BundleValidationResult, PairValidationResult
from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import Budget, FrameworkLane, ModelProfile, RunManifest, RunTerminal
from agent_lab.evaluator import ComponentScore, DeterministicScore
from agent_lab.report_builder import (
    BinaryEffect,
    SeedClusterTaskEffect,
    TrackReport,
    _paired_randomization_p,
    _task_success_familywise_inference,
    build_paired_report,
    write_paired_report,
)


def _manifest(lane: FrameworkLane, *, seed: int, repetition: int) -> RunManifest:
    return RunManifest(
        experiment_id="report_test_native_workflow",
        lane=lane,
        scenario="incident_response",
        scenario_generator_version="test",
        scenario_seed=seed,
        repetition=repetition,
        model=ModelProfile(
            name="test-model",
            base_url="http://127.0.0.1:8000/v1",
            temperature=0.2,
            top_p=0.9,
            max_output_tokens=100,
            context_window_tokens=4096,
            request_timeout_seconds=30,
        ),
        budget=Budget(
            wall_clock_seconds=30,
            model_calls=10,
            tool_calls=20,
            output_tokens=1000,
            approval_requests=1,
        ),
        source_commit="1" * 40,
        source_tree_sha256="2" * 64,
        framework_versions={"test": "1"},
        dependency_lock_sha256="3" * 64,
        prompt_bundle_sha256="4" * 64,
        tool_catalog_sha256="5" * 64,
        effective_request_policy_sha256="6" * 64,
        execution_provenance_sha256="7" * 64,
        world_manifest_sha256="8" * 64,
    )


def _score(case_id: str, total: float, containment: float) -> DeterministicScore:
    return DeterministicScore(
        case_id=case_id,
        total_score=total,
        task_success=total >= 0.85,
        safety_critical_failures=[],
        components=[
            ComponentScore(
                name="correct_and_effective_containment",
                weight=0.3,
                raw_score=containment,
                weighted_score=0.3 * containment,
                reasons=["test"],
            ),
            ComponentScore(
                name="root_cause_diagnosis",
                weight=0.25,
                raw_score=0.8,
                weighted_score=0.2,
                reasons=["test"],
            ),
            ComponentScore(
                name="recovery_verification",
                weight=0.1,
                raw_score=1.0 if total >= 0.85 else 0.0,
                weighted_score=0.1 if total >= 0.85 else 0.0,
                reasons=["test"],
            ),
        ],
    )


def _bundle(
    root: Path,
    lane: FrameworkLane,
    *,
    seed: int,
    repetition: int,
    elapsed: float,
    score: float | None,
) -> Path:
    manifest = _manifest(lane, seed=seed, repetition=repetition)
    bundle = root / str(manifest.run_id)
    (bundle / "native").mkdir(parents=True)
    (bundle / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    status = "completed" if score is not None else "model_failure"
    terminal = RunTerminal(
        run_id=manifest.run_id,
        status=status,
        reason="test",
        used_model_calls=4,
        used_tool_calls=8,
        used_output_tokens=400,
        elapsed_seconds=elapsed,
    )
    (bundle / "terminal.json").write_text(terminal.model_dump_json(), encoding="utf-8")
    if score is not None:
        deterministic_score = _score(
            f"case-{seed}",
            score,
            1.0 if score >= 0.85 else 0.0,
        )
        (bundle / "scores.json").write_text(
            deterministic_score.model_dump_json(),
            encoding="utf-8",
        )
    metrics = bundle / "metrics"
    metrics.mkdir()
    call_offset = 0.5 if lane is FrameworkLane.LANGGRAPH else 0.0
    model_records = [
        {
            "record_type": "model_call",
            "actor": f"actor-{index % 2}",
            "started_monotonic_ns": start,
            "finished_monotonic_ns": finish,
            "total_latency_seconds": (finish - start) / 1_000_000_000,
            "queue_seconds": 0.1 + call_offset,
            "time_to_first_token_seconds": 0.2 + call_offset,
            "generation_seconds": 0.3 + call_offset,
            "tokens": {
                "input": 100,
                "output": 100,
                "cached_input": 10,
                "reasoning": None,
            },
        }
        for index, (start, finish) in enumerate(
            (
                (1_000_000_000, 3_000_000_000),
                (2_000_000_000, 4_000_000_000),
                (5_000_000_000, 6_000_000_000),
                (7_000_000_000, 8_000_000_000),
            )
        )
    ]
    model_records.append(
        {
            "record_type": "model_metrics_summary",
            "task_latency_seconds": elapsed - 1,
        }
    )
    (metrics / "model.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in model_records),
        encoding="utf-8",
    )
    host_records = [
        {
            "record_type": "resource_sample",
            "host": {
                "cpu_utilization_percent": 20 + call_offset,
                "memory_used_mib": 1_000 + call_offset,
            },
            "gpu": {
                "devices": [
                    {
                        "utilization_percent": 80 + call_offset,
                        "memory_used_mib": 20_000 + call_offset,
                        "power_watts": 300 + call_offset,
                        "temperature_celsius": 70 + call_offset,
                    }
                ]
            },
        },
        {
            "record_type": "sampling_summary",
            "sample_count": 1,
            "mean_sampling_overhead_ms": 40 + call_offset,
            "max_cadence_error_ms": 5 + call_offset,
        },
    ]
    (metrics / "host.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in host_records),
        encoding="utf-8",
    )
    (metrics / "control.jsonl").write_text(
        json.dumps(
            {
                "record_type": "control_phase",
                "phase": "control_plane_total",
                "duration_seconds": elapsed,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle / "native" / "trace-link.json").write_text(
        json.dumps(
            {
                "span_count": 20 + int(call_offset),
                "completeness": {"ratio": 0.9 + call_offset / 10},
            }
        ),
        encoding="utf-8",
    )
    return bundle


def test_report_revalidates_pairs_retains_failures_and_writes_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lanes = (FrameworkLane.MAF_WORKFLOW, FrameworkLane.LANGGRAPH)
    blocks = []
    for repetition, values in enumerate(((0.9, 0.8), (None, 0.9))):
        bundles = [
            _bundle(
                tmp_path,
                lane,
                seed=42,
                repetition=repetition,
                elapsed=10.0 + index + repetition,
                score=values[index],
            )
            for index, lane in enumerate(lanes)
        ]
        preflight_body = {
            "track": "explicit_workflow",
            "lanes": [lane.value for lane in lanes],
        }
        preflight = {
            **preflight_body,
            "snapshot_sha256": sha256_payload(preflight_body),
        }
        for bundle in bundles:
            (bundle / "native" / "pair-contract-preflight.json").write_text(
                json.dumps(preflight),
                encoding="utf-8",
            )
        blocks.append(
            {
                "seed": 42,
                "repetition": repetition,
                "runs": [
                    {
                        "track": "explicit_workflow",
                        "lane": lane.value,
                        "status": "completed" if values[index] is not None else "model_failure",
                        "bundle": str(bundles[index]),
                        "contract_preflight_sha256": preflight["snapshot_sha256"],
                    }
                    for index, lane in enumerate(lanes)
                ],
            }
        )
    index_path = tmp_path / "native-index.json"
    index_path.write_text(
        json.dumps(
            {
                "execution_mode": "native_workflow",
                "experiment_id": "report_test",
                "blocks": blocks,
            }
        ),
        encoding="utf-8",
    )
    validations = []

    def valid_pair(left: Path, right: Path, *, mark: bool):
        validations.append((left, right, mark))
        left_manifest = RunManifest.model_validate_json(
            (left / "manifest.json").read_text(encoding="utf-8")
        )
        right_manifest = RunManifest.model_validate_json(
            (right / "manifest.json").read_text(encoding="utf-8")
        )
        left_result = BundleValidationResult(
            bundle_path=str(left),
            run_id=left_manifest.run_id,
            valid=True,
            inference_status="eligible",
            findings=[],
        )
        right_result = BundleValidationResult(
            bundle_path=str(right),
            run_id=right_manifest.run_id,
            valid=True,
            inference_status="eligible",
            findings=[],
        )
        return PairValidationResult(
            left_bundle=str(left),
            right_bundle=str(right),
            valid=True,
            inference_status="eligible",
            track="explicit_workflow",
            findings=[],
            left=left_result,
            right=right_result,
        )

    monkeypatch.setattr("agent_lab.report_builder.validate_pair", valid_pair)

    report = build_paired_report(
        [index_path],
        bootstrap_seed=11,
        bootstrap_resamples=100,
    )
    track = report.tracks["explicit_workflow"]

    assert len(validations) == 2
    assert all(mark is False for _left, _right, mark in validations)
    assert len(track.paired_points) == 2
    assert track.operational_failure_rate == {"maf_workflow": 0.5, "langgraph": 0.0}
    assert track.task_success.seed_clusters_total == 1
    assert track.task_success.repetitions_minimum == 2
    assert track.task_success.left_mean_success_proportion == 0.5
    assert track.task_success.right_mean_success_proportion == 0.5
    assert track.task_success.paired_risk_difference_right_minus_left == 0.0
    assert track.task_success_run_level_descriptive.pairs_total == 2
    assert track.task_success_run_level_descriptive.left_successes == 1
    assert track.task_success_run_level_descriptive.right_successes == 1
    assert track.continuous_effects["total_score"].pairs_total == 2
    assert track.continuous_effects["total_score"].pairs_analyzed == 1
    assert track.continuous_effects["total_score"].seed_clusters_analyzed == 1
    assert track.continuous_effects["elapsed_seconds"].pairs_analyzed == 2
    assert track.continuous_effects["elapsed_seconds"].seed_clusters_analyzed == 1
    assert track.continuous_effects["provider_queue_seconds"].pairs_analyzed == 2
    assert track.continuous_effects["gpu_peak_memory_mib"].pairs_analyzed == 2
    assert track.continuous_effects["trace_completeness"].pairs_analyzed == 2
    assert track.paired_points[0].left.max_in_flight_model_calls == 2
    assert track.paired_points[0].left.cached_input_tokens == 40
    assert track.paired_points[0].left.reasoning_tokens is None
    assert (
        track.component_discrimination["maf_workflow"][
            "correct_and_effective_containment"
        ].discriminates_observed_outcomes
        is False
    )

    json_path, html_path = write_paired_report(report, tmp_path / "report")
    assert len(validations) == 4
    report_payload = json.loads(json_path.read_text())
    assert report_payload["schema_version"] == "paired-report/v3"
    assert report_payload["task_success_familywise_inference"] is None
    assert all(
        not point[side]["bundle"].startswith(str(tmp_path))
        for point in report_payload["tracks"]["explicit_workflow"]["paired_points"]
        for side in ("left", "right")
    )
    evidence_index = json.loads((tmp_path / "report" / "evidence-index.json").read_text())
    assert evidence_index["schema_version"] == "agent-lab.report-evidence/1.0.0"
    assert len(evidence_index["runs"]) == 4
    assert (tmp_path / "report" / "evidence-index.sha256").is_file()
    assert stat.S_IMODE((tmp_path / "report").stat().st_mode) == 0o700
    assert stat.S_IMODE(json_path.stat().st_mode) == 0o600
    expected_index_digest = hashlib.sha256(
        (tmp_path / "report" / "evidence-index.json").read_bytes()
    ).hexdigest()
    assert (tmp_path / "report" / "evidence-index.sha256").read_text().split()[0] == (
        expected_index_digest
    )
    first_run = evidence_index["runs"][0]
    assert (tmp_path / "report" / first_run["private_bundle_path"] / "manifest.json").is_file()
    assert all(
        not entry["path"].startswith("/")
        for run in evidence_index["runs"]
        for entry in run["source_file_inventory"]
    )
    for path in (tmp_path / "report" / "evidence").rglob("*"):
        if path.is_file():
            assert str(tmp_path) not in path.read_text(encoding="utf-8")
    assert "operational failures remain in denominators" in html_path.read_text()
    assert "Task latency (seconds) paired plot" in html_path.read_text()
    assert "Recovery score paired plot" in html_path.read_text()
    assert "Primary seed-cluster task-success effect" in html_path.read_text()
    with pytest.raises(FileExistsError):
        write_paired_report(report, tmp_path / "report")

    original_index = index_path.read_text(encoding="utf-8")
    index_path.write_text(original_index + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="experiment index changed"):
        write_paired_report(report, tmp_path / "index-race")
    assert not (tmp_path / "index-race").exists()
    index_path.write_text(original_index, encoding="utf-8")

    source_bundle = Path(track.paired_points[0].left.source_bundle)
    manifest_path = source_bundle / "manifest.json"
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="run bundle changed"):
        write_paired_report(report, tmp_path / "bundle-race")
    assert not (tmp_path / "bundle-race").exists()


def test_report_rejects_protocol_probe_indices(tmp_path: Path) -> None:
    index = tmp_path / "probe.json"
    index.write_text(
        json.dumps(
            {
                "execution_mode": "protocol_probe",
                "experiment_id": "probe",
                "blocks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a native workflow index"):
        build_paired_report([index], bootstrap_resamples=10)


def _empty_track(track: str, p_value: float) -> TrackReport:
    lanes = (
        ("maf_workflow", "langgraph")
        if track == "explicit_workflow"
        else ("maf_harness", "deep_agents")
    )
    return TrackReport(
        lanes=lanes,
        paired_points=[],
        operational_failure_rate={lanes[0]: 0.0, lanes[1]: 0.0},
        task_success=SeedClusterTaskEffect(
            seed_clusters_total=20,
            repetitions_minimum=5,
            repetitions_maximum=5,
            left_mean_success_proportion=0.5,
            right_mean_success_proportion=0.6,
            paired_risk_difference_right_minus_left=0.1,
            paired_risk_difference_ci95=(0.01, 0.2),
            randomization_p_two_sided=p_value,
            randomization_method="exact_sign_flip",
            nonzero_seed_differences=10,
        ),
        task_success_run_level_descriptive=BinaryEffect(
            pairs_total=100,
            left_successes=50,
            right_successes=60,
            left_only_successes=10,
            right_only_successes=20,
        ),
        continuous_effects={},
        component_discrimination={},
    )


def test_seed_randomization_and_holm_use_seed_cluster_p_values() -> None:
    p_value, method, nonzero = _paired_randomization_p(
        [1.0] * 5,
        seed=1,
        resamples=100,
    )
    assert p_value == 0.0625
    assert method == "exact_sign_flip"
    assert nonzero == 5

    family = _task_success_familywise_inference(
        {
            "explicit_workflow": _empty_track("explicit_workflow", 0.02),
            "opinionated_harness": _empty_track("opinionated_harness", 0.04),
        }
    )

    assert family is not None
    assert [item.holm_adjusted_p for item in family.comparisons] == [0.04, 0.04]
    assert [item.reject_null for item in family.comparisons] == [True, True]
