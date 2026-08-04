"""Fail-closed measured-study preregistration and pilot sizing."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from enum import StrEnum
from pathlib import Path
from statistics import NormalDist
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_lab.config import load_toml
from agent_lab.report_builder import PairedPoint

STUDY_PROTOCOL_SCHEMA_VERSION = "agent-lab-study-protocol/v1"


class StudyStatus(StrEnum):
    BLOCKED = "blocked"
    APPROVED = "approved"


class StudyProtocol(BaseModel):
    """Versioned decisions that must be complete before measured execution."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-lab-study-protocol/v1"] = STUDY_PROTOCOL_SCHEMA_VERSION
    study_id: str = Field(min_length=1)
    status: StudyStatus
    blocked_reasons: list[str] = Field(default_factory=list)
    report_schema_version: Literal["paired-report/v3"]
    primary_outcome: Literal["task_success"]
    primary_outcome_definition: str = Field(min_length=1)
    smallest_practical_risk_difference: float = Field(gt=0, lt=1)
    familywise_alpha: float = Field(gt=0, lt=1)
    target_power: float = Field(gt=0, lt=1)
    confirmatory_tracks: tuple[
        Literal["explicit_workflow", "opinionated_harness"],
        Literal["explicit_workflow", "opinionated_harness"],
    ]
    multiplicity: Literal["holm_two_sided"]
    analysis_unit: Literal["scenario_seed"]
    repetitions_per_seed: int = Field(ge=2)
    minimum_measured_seeds: int = Field(ge=2)
    pilot_minimum_seeds: int = Field(ge=2)
    pilot_minimum_repetitions: int = Field(ge=2)
    pilot_sd_inflation: float = Field(ge=1)
    operational_failure_policy: Literal["count_as_task_failure"]
    invalid_pair_policy: Literal["exclude_whole_pair_and_replace_before_unblinding"]
    outlier_policy: Literal["no_exclusion"]
    stopping_policy: Literal["no_optional_stopping"]
    prompt_sensitivity_policy: Literal["matched_exploratory"]
    ablation_policy: Literal["matched_exploratory"]
    developer_effort_policy: Literal["descriptive_only"]
    evidence_retention: Literal["portable_redacted_plus_owner_only_raw"]
    frozen_experiment_profiles: list[str] = Field(min_length=1)
    model_profile: str | None = Field(default=None, min_length=1)
    model_gate_passed: bool = False
    source_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    pilot_report_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    measured_seeds: int | None = Field(default=None, ge=2)
    estimated_gpu_hours: float | None = Field(default=None, gt=0)
    component_discrimination_passed: bool = False
    reviewer: str | None = Field(default=None, min_length=1)
    approved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_gate(self) -> StudyProtocol:
        if set(self.confirmatory_tracks) != {"explicit_workflow", "opinionated_harness"}:
            raise ValueError(
                "confirmatory tracks must contain explicit_workflow and opinionated_harness"
            )
        if self.status is StudyStatus.BLOCKED:
            if not self.blocked_reasons:
                raise ValueError("blocked study protocol requires at least one blocking reason")
            return self
        if self.blocked_reasons:
            raise ValueError("approved study protocol cannot retain blocking reasons")
        required = {
            "model_profile": self.model_profile,
            "source_commit": self.source_commit,
            "pilot_report_sha256": self.pilot_report_sha256,
            "measured_seeds": self.measured_seeds,
            "estimated_gpu_hours": self.estimated_gpu_hours,
            "reviewer": self.reviewer,
            "approved_at": self.approved_at,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(f"approved study protocol is incomplete: {', '.join(missing)}")
        if self.measured_seeds is not None and self.measured_seeds < self.minimum_measured_seeds:
            raise ValueError("approved measured seed count is below the preregistered floor")
        if not self.model_gate_passed:
            raise ValueError("approved study protocol requires a passed model gate")
        if not self.component_discrimination_passed:
            raise ValueError("approved study protocol requires component discrimination")
        return self


class TrackSampleSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    pilot_seeds: int
    pilot_repetitions_minimum: int
    paired_seed_difference_sd: float
    inflated_planning_sd: float
    estimated_seeds: int
    planned_pairs: int
    median_pair_seconds: float
    estimated_gpu_hours: float


class PilotSizingRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_success: bool
    elapsed_seconds: float = Field(ge=0)


class PilotSizingPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_seed: int
    repetition: int = Field(ge=0)
    left: PilotSizingRun
    right: PilotSizingRun


class PilotSizingTrack(BaseModel):
    model_config = ConfigDict(extra="ignore")

    paired_points: list[PilotSizingPoint]


class PilotSizingReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["paired-report/v3"]
    tracks: dict[str, PilotSizingTrack]


class StudySampleSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent-lab-study-size/v1"] = "agent-lab-study-size/v1"
    study_id: str
    pilot_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome: Literal["task_success"] = "task_success"
    analysis_unit: Literal["scenario_seed"] = "scenario_seed"
    familywise_alpha: float
    conservative_per_track_alpha: float
    target_power: float
    smallest_practical_risk_difference: float
    repetitions_per_seed: int
    tracks: dict[str, TrackSampleSize]
    measured_seeds: int
    total_pairs: int
    estimated_gpu_hours: float


def load_study_protocol(path: Path) -> StudyProtocol:
    """Load a study protocol TOML document."""

    return StudyProtocol.model_validate(load_toml(path))


def validate_study_references(protocol: StudyProtocol, repository_root: Path) -> None:
    """Require every frozen profile and any selected model profile to exist."""

    paths = list(protocol.frozen_experiment_profiles)
    if protocol.model_profile is not None:
        paths.append(protocol.model_profile)
    missing = [value for value in paths if not (repository_root / value).is_file()]
    if missing:
        raise ValueError(f"study protocol references missing files: {', '.join(missing)}")


def estimate_study_sample_size(
    protocol: StudyProtocol,
    report: PilotSizingReport,
    report_path: Path,
) -> StudySampleSize:
    """Size seed clusters from an accepted-model paired pilot report."""

    if protocol.status is StudyStatus.APPROVED:
        raise ValueError("sample size must be frozen before protocol approval")
    if not protocol.model_gate_passed or protocol.model_profile is None:
        raise ValueError("sample sizing requires a selected model that passed the paired gate")
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    per_track_alpha = protocol.familywise_alpha / len(protocol.confirmatory_tracks)
    tracks: dict[str, TrackSampleSize] = {}
    for track in protocol.confirmatory_tracks:
        track_report = report.tracks.get(track)
        if track_report is None:
            raise ValueError(f"pilot report is missing confirmatory track {track}")
        tracks[track] = estimate_track_sample_size(
            track,
            track_report.paired_points,
            alpha=per_track_alpha,
            target_power=protocol.target_power,
            minimum_effect=protocol.smallest_practical_risk_difference,
            repetitions_per_seed=protocol.repetitions_per_seed,
            minimum_pilot_seeds=protocol.pilot_minimum_seeds,
            minimum_pilot_repetitions=protocol.pilot_minimum_repetitions,
            minimum_measured_seeds=protocol.minimum_measured_seeds,
            sd_inflation=protocol.pilot_sd_inflation,
        )
    measured_seeds = max(item.estimated_seeds for item in tracks.values())
    total_pairs = measured_seeds * protocol.repetitions_per_seed * len(tracks)
    estimated_gpu_hours = sum(
        item.median_pair_seconds * measured_seeds * protocol.repetitions_per_seed / 3600
        for item in tracks.values()
    )
    return StudySampleSize(
        study_id=protocol.study_id,
        pilot_report_sha256=report_sha256,
        familywise_alpha=protocol.familywise_alpha,
        conservative_per_track_alpha=per_track_alpha,
        target_power=protocol.target_power,
        smallest_practical_risk_difference=protocol.smallest_practical_risk_difference,
        repetitions_per_seed=protocol.repetitions_per_seed,
        tracks=tracks,
        measured_seeds=measured_seeds,
        total_pairs=total_pairs,
        estimated_gpu_hours=estimated_gpu_hours,
    )


def estimate_track_sample_size(
    track: str,
    points: list[PilotSizingPoint] | list[PairedPoint],
    *,
    alpha: float,
    target_power: float,
    minimum_effect: float,
    repetitions_per_seed: int,
    minimum_pilot_seeds: int,
    minimum_pilot_repetitions: int,
    minimum_measured_seeds: int,
    sd_inflation: float,
) -> TrackSampleSize:
    """Estimate seed clusters using paired seed-level task-success differences."""

    grouped: dict[int, list[PilotSizingPoint | PairedPoint]] = defaultdict(list)
    for point in points:
        grouped[point.scenario_seed].append(point)
    if len(grouped) < minimum_pilot_seeds:
        raise ValueError(
            f"{track} pilot has {len(grouped)} seeds; at least {minimum_pilot_seeds} required"
        )
    differences: list[float] = []
    pair_seconds: list[float] = []
    minimum_repetitions_seen = min(len(seed_points) for seed_points in grouped.values())
    if minimum_repetitions_seen < minimum_pilot_repetitions:
        raise ValueError(
            f"{track} pilot has only {minimum_repetitions_seen} repetitions for a seed; "
            f"at least {minimum_pilot_repetitions} required"
        )
    for seed_points in grouped.values():
        repetitions = [point.repetition for point in seed_points]
        if len(set(repetitions)) != len(repetitions):
            raise ValueError(f"{track} pilot repeats a repetition within one scenario seed")
        left = statistics.fmean(float(point.left.task_success) for point in seed_points)
        right = statistics.fmean(float(point.right.task_success) for point in seed_points)
        differences.append(right - left)
        pair_seconds.extend(
            point.left.elapsed_seconds + point.right.elapsed_seconds for point in seed_points
        )
    paired_sd = statistics.stdev(differences)
    if paired_sd == 0:
        raise ValueError(
            f"{track} pilot has zero paired seed variance; sample size is unidentified"
        )
    inflated_sd = paired_sd * sd_inflation
    normal = NormalDist()
    critical = normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(target_power)
    estimated = math.ceil((critical * inflated_sd / minimum_effect) ** 2)
    estimated = max(minimum_measured_seeds, estimated)
    median_pair_seconds = statistics.median(pair_seconds)
    return TrackSampleSize(
        track=track,
        pilot_seeds=len(grouped),
        pilot_repetitions_minimum=minimum_repetitions_seen,
        paired_seed_difference_sd=paired_sd,
        inflated_planning_sd=inflated_sd,
        estimated_seeds=estimated,
        planned_pairs=estimated * repetitions_per_seed,
        median_pair_seconds=median_pair_seconds,
        estimated_gpu_hours=median_pair_seconds * estimated * repetitions_per_seed / 3600,
    )


def load_paired_report(path: Path) -> PilotSizingReport:
    """Load only public fields from the report archive used for sizing."""

    return PilotSizingReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
