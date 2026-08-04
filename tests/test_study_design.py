import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_lab.contracts import TerminalStatus
from agent_lab.report_builder import (
    BinaryEffect,
    PairedPoint,
    PairedReport,
    RunPoint,
    SeedClusterTaskEffect,
    TrackReport,
)
from agent_lab.study_design import (
    StudyProtocol,
    estimate_study_sample_size,
    estimate_track_sample_size,
    load_paired_report,
    load_study_protocol,
    validate_study_references,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPOSITORY_ROOT / "configs/studies/measured-v1.toml"


def _run(lane: str, success: bool, elapsed_seconds: float) -> RunPoint:
    return RunPoint(
        run_id=f"run-{lane}-{success}-{elapsed_seconds}",
        bundle=f"bundles/{lane}",
        source_bundle=f"/private/{lane}",
        source_bundle_inventory_sha256="a" * 64,
        lane=lane,
        terminal_status=TerminalStatus.COMPLETED,
        operational_failure=False,
        task_success=success,
        total_score=0.9 if success else 0.6,
        elapsed_seconds=elapsed_seconds,
        model_calls=4,
        tool_calls=8,
        output_tokens=100,
    )


def _points(track: str) -> list[PairedPoint]:
    success_counts = [(0, 3), (1, 2), (2, 1), (2, 3), (1, 0)]
    lanes = (
        ("maf_workflow", "langgraph")
        if track == "explicit_workflow"
        else ("maf_harness", "deep_agents")
    )
    points: list[PairedPoint] = []
    for seed_offset, (left_count, right_count) in enumerate(success_counts):
        for repetition in range(3):
            points.append(
                PairedPoint(
                    experiment_id=f"{track}-pilot",
                    track=track,
                    scenario_seed=100 + seed_offset,
                    repetition=repetition,
                    left=_run(lanes[0], repetition < left_count, 90 + repetition),
                    right=_run(lanes[1], repetition < right_count, 100 + repetition),
                )
            )
    return points


def _track(track: str) -> TrackReport:
    points = _points(track)
    return TrackReport(
        lanes=(points[0].left.lane, points[0].right.lane),
        paired_points=points,
        operational_failure_rate={points[0].left.lane: 0.0, points[0].right.lane: 0.0},
        task_success=SeedClusterTaskEffect(
            seed_clusters_total=5,
            repetitions_minimum=3,
            repetitions_maximum=3,
            left_mean_success_proportion=0.4,
            right_mean_success_proportion=0.6,
            paired_risk_difference_right_minus_left=0.2,
            paired_risk_difference_ci95=(-0.1, 0.5),
            randomization_p_two_sided=0.5,
            randomization_method="exact_sign_flip",
            nonzero_seed_differences=5,
        ),
        task_success_run_level_descriptive=BinaryEffect(
            pairs_total=len(points),
            left_successes=sum(point.left.task_success for point in points),
            right_successes=sum(point.right.task_success for point in points),
            left_only_successes=sum(
                point.left.task_success and not point.right.task_success for point in points
            ),
            right_only_successes=sum(
                point.right.task_success and not point.left.task_success for point in points
            ),
        ),
        continuous_effects={},
        component_discrimination={},
    )


def test_blocked_study_protocol_is_valid_and_references_exist() -> None:
    protocol = load_study_protocol(PROTOCOL_PATH)

    validate_study_references(protocol, REPOSITORY_ROOT)

    assert protocol.status.value == "blocked"
    assert protocol.primary_outcome == "task_success"
    assert protocol.model_profile == "configs/models/qwen35_27b_llamacpp_q4km.toml"
    assert protocol.model_gate_passed is True
    assert len(protocol.blocked_reasons) == 2


def test_approved_study_protocol_fails_closed_without_gate_evidence() -> None:
    payload = load_study_protocol(PROTOCOL_PATH).model_dump(mode="json")
    payload["status"] = "approved"
    payload["blocked_reasons"] = []

    with pytest.raises(ValidationError, match="approved study protocol is incomplete"):
        StudyProtocol.model_validate(payload)


def test_seed_cluster_sizing_uses_repetitions_as_clusters() -> None:
    estimate = estimate_track_sample_size(
        "explicit_workflow",
        _points("explicit_workflow"),
        alpha=0.025,
        target_power=0.8,
        minimum_effect=0.1,
        repetitions_per_seed=5,
        minimum_pilot_seeds=5,
        minimum_pilot_repetitions=3,
        minimum_measured_seeds=20,
        sd_inflation=1.25,
    )

    assert estimate.pilot_seeds == 5
    assert estimate.pilot_repetitions_minimum == 3
    assert estimate.estimated_seeds >= 20
    assert estimate.planned_pairs == estimate.estimated_seeds * 5
    assert estimate.estimated_gpu_hours > 0


def test_study_sizing_hashes_report_and_uses_larger_track_requirement(
    tmp_path: Path,
) -> None:
    protocol = load_study_protocol(PROTOCOL_PATH)
    report = PairedReport(
        source_indices=["pilot-explicit.json", "pilot-harness.json"],
        source_index_paths=["/private/pilot-explicit.json", "/private/pilot-harness.json"],
        bootstrap_seed=1729,
        bootstrap_resamples=10_000,
        tracks={
            "explicit_workflow": _track("explicit_workflow"),
            "opinionated_harness": _track("opinionated_harness"),
        },
    )
    report_path = tmp_path / "paired-report.json"
    report_path.write_text(report.model_dump_json(), encoding="utf-8")

    public_report = load_paired_report(report_path)
    estimate = estimate_study_sample_size(protocol, public_report, report_path)

    assert estimate.pilot_report_sha256
    assert estimate.measured_seeds == max(
        track.estimated_seeds for track in estimate.tracks.values()
    )
    assert estimate.total_pairs == estimate.measured_seeds * 5 * 2
    assert json.loads(estimate.model_dump_json())["outcome"] == "task_success"


def test_study_sizing_rejects_unaccepted_model(tmp_path: Path) -> None:
    protocol_payload = load_study_protocol(PROTOCOL_PATH).model_dump(mode="json")
    protocol_payload["model_profile"] = None
    protocol_payload["model_gate_passed"] = False
    protocol = StudyProtocol.model_validate(protocol_payload)
    report_path = tmp_path / "paired-report.json"
    report_path.write_text("{}", encoding="utf-8")
    report = PairedReport(
        source_indices=[],
        source_index_paths=[],
        bootstrap_seed=1729,
        bootstrap_resamples=10_000,
        tracks={},
    )

    with pytest.raises(ValueError, match="selected model that passed"):
        estimate_study_sample_size(protocol, report, report_path)


def test_seed_cluster_sizing_rejects_zero_variance() -> None:
    points = _points("explicit_workflow")
    for point in points:
        point.left.task_success = False
        point.right.task_success = False

    with pytest.raises(ValueError, match="zero paired seed variance"):
        estimate_track_sample_size(
            "explicit_workflow",
            points,
            alpha=0.025,
            target_power=0.8,
            minimum_effect=0.1,
            repetitions_per_seed=5,
            minimum_pilot_seeds=5,
            minimum_pilot_repetitions=3,
            minimum_measured_seeds=20,
            sd_inflation=1.25,
        )


def test_seed_cluster_sizing_rejects_duplicate_repetitions() -> None:
    points = _points("explicit_workflow")
    points[1].repetition = points[0].repetition

    with pytest.raises(ValueError, match="repeats a repetition"):
        estimate_track_sample_size(
            "explicit_workflow",
            points,
            alpha=0.025,
            target_power=0.8,
            minimum_effect=0.1,
            repetitions_per_seed=5,
            minimum_pilot_seeds=5,
            minimum_pilot_repetitions=3,
            minimum_measured_seeds=20,
            sd_inflation=1.25,
        )
