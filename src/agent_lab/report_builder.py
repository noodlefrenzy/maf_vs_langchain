"""Offline paired report generation from immutable, provenance-valid bundles."""

from __future__ import annotations

import hashlib
import html
import json
import math
import random
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_lab.bundle_validation import validate_pair
from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import RunManifest, RunTerminal, TerminalStatus
from agent_lab.evaluator import DeterministicScore

REPORT_SCHEMA_VERSION = "paired-report/v2"
EVIDENCE_INDEX_SCHEMA_VERSION = "agent-lab.report-evidence/1.0.0"
REDACTED_INDEX_SCHEMA_VERSION = "agent-lab.redacted-experiment-index/1.0.0"
PLOT_METRICS = (
    ("task_latency_seconds", "Task latency (seconds)"),
    ("provider_queue_seconds", "Provider queue time (seconds)"),
    ("model_calls", "Model calls"),
    ("tool_calls", "Tool calls"),
    ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"),
    ("components.correct_and_effective_containment", "Containment score"),
    ("components.recovery_verification", "Recovery score"),
    ("actors_observed", "Actors observed"),
    ("max_in_flight_model_calls", "Max client in-flight model calls"),
    ("gpu_peak_utilization_percent", "Peak GPU utilization (%)"),
    ("gpu_peak_memory_mib", "Peak GPU memory (MiB)"),
    ("host_peak_memory_mib", "Peak host memory (MiB)"),
    ("trace_completeness", "Native trace completeness"),
)


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunPoint(ReportModel):
    run_id: str
    bundle: str
    source_bundle: str = Field(exclude=True, repr=False)
    source_bundle_inventory_sha256: str = Field(exclude=True, repr=False)
    lane: str
    terminal_status: TerminalStatus
    operational_failure: bool
    task_success: bool
    total_score: float | None = Field(default=None, ge=0, le=1)
    elapsed_seconds: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    task_latency_seconds: float | None = Field(default=None, ge=0)
    control_overhead_seconds: float | None = Field(default=None, ge=0)
    provider_queue_seconds: float | None = Field(default=None, ge=0)
    provider_ttft_seconds: float | None = Field(default=None, ge=0)
    provider_generation_seconds: float | None = Field(default=None, ge=0)
    model_call_time_seconds: float | None = Field(default=None, ge=0)
    actors_observed: int | None = Field(default=None, ge=0)
    max_in_flight_model_calls: int | None = Field(default=None, ge=0)
    resource_samples: int | None = Field(default=None, ge=0)
    mean_sampling_overhead_ms: float | None = Field(default=None, ge=0)
    max_cadence_error_ms: float | None = Field(default=None, ge=0)
    gpu_peak_utilization_percent: float | None = Field(default=None, ge=0, le=100)
    gpu_peak_memory_mib: float | None = Field(default=None, ge=0)
    gpu_peak_power_watts: float | None = Field(default=None, ge=0)
    gpu_peak_temperature_celsius: float | None = None
    host_peak_cpu_percent: float | None = Field(default=None, ge=0, le=100)
    host_peak_memory_mib: float | None = Field(default=None, ge=0)
    trace_completeness: float | None = Field(default=None, ge=0, le=1)
    native_span_count: int | None = Field(default=None, ge=0)
    components: dict[str, float] = Field(default_factory=dict)


class PairedPoint(ReportModel):
    experiment_id: str
    track: Literal["explicit_workflow", "opinionated_harness", "single_agent_baseline"]
    scenario_seed: int
    repetition: int = Field(ge=0)
    left: RunPoint
    right: RunPoint


class ContinuousEffect(ReportModel):
    pairs_total: int = Field(ge=0)
    pairs_analyzed: int = Field(ge=0)
    left_median: float | None = None
    right_median: float | None = None
    median_difference_right_minus_left: float | None = None
    mean_difference_right_minus_left: float | None = None
    mean_difference_ci95: tuple[float, float] | None = None
    median_difference_ci95: tuple[float, float] | None = None


class BinaryEffect(ReportModel):
    pairs_total: int = Field(ge=0)
    left_successes: int = Field(ge=0)
    right_successes: int = Field(ge=0)
    left_only_successes: int = Field(ge=0)
    right_only_successes: int = Field(ge=0)
    paired_risk_difference_right_minus_left: float | None = None
    exact_mcnemar_p_two_sided: float | None = Field(default=None, ge=0, le=1)


class ComponentDiscrimination(ReportModel):
    observations: int = Field(ge=0)
    distinct_raw_scores: list[float]
    minimum: float | None = None
    maximum: float | None = None
    discriminates_observed_outcomes: bool


class TrackReport(ReportModel):
    lanes: tuple[str, str]
    paired_points: list[PairedPoint]
    operational_failure_rate: dict[str, float]
    task_success: BinaryEffect
    continuous_effects: dict[str, ContinuousEffect]
    component_discrimination: dict[str, dict[str, ComponentDiscrimination]]


class PairedReport(ReportModel):
    schema_version: Literal["paired-report/v2"] = REPORT_SCHEMA_VERSION
    source_indices: list[str]
    source_index_paths: list[str] = Field(exclude=True, repr=False)
    bootstrap_seed: int
    bootstrap_resamples: int = Field(gt=0)
    tracks: dict[str, TrackReport]


def build_paired_report(
    index_paths: list[Path],
    *,
    bootstrap_seed: int = 1729,
    bootstrap_resamples: int = 10_000,
) -> PairedReport:
    """Validate source pairs, retain failures, and compute deterministic summaries."""

    if not index_paths:
        raise ValueError("at least one native experiment index is required")
    if bootstrap_resamples <= 0:
        raise ValueError("bootstrap_resamples must be positive")
    points_by_track: dict[str, list[PairedPoint]] = defaultdict(list)
    sources: list[str] = []
    source_paths: list[str] = []
    seen_pairs: set[tuple[str, int, int, str]] = set()
    seen_runs: set[str] = set()
    for raw_path in index_paths:
        index_path = raw_path.resolve()
        payload = _load_object(index_path, "experiment index")
        if payload.get("execution_mode") != "native_workflow":
            raise ValueError(f"report input is not a native workflow index: {index_path}")
        experiment_id = _required_string(payload, "experiment_id", "experiment index")
        blocks = payload.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError(f"experiment index blocks must be a list: {index_path}")
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError(f"experiment block must be an object: {index_path}")
            seed = _required_int(block, "seed", "experiment block")
            repetition = _required_int(block, "repetition", "experiment block")
            runs = block.get("runs")
            if not isinstance(runs, list):
                raise ValueError(f"experiment block runs must be a list: {index_path}")
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for run in runs:
                if not isinstance(run, dict):
                    raise ValueError(f"experiment run must be an object: {index_path}")
                grouped[_required_string(run, "track", "experiment run")].append(run)
            for track, pair_runs in grouped.items():
                if len(pair_runs) != 2:
                    raise ValueError(
                        f"{track} block seed={seed} repetition={repetition} "
                        "must contain exactly two runs"
                    )
                pair_key = (experiment_id, seed, repetition, track)
                if pair_key in seen_pairs:
                    raise ValueError(f"duplicate paired block in report inputs: {pair_key}")
                seen_pairs.add(pair_key)
                point = _load_pair(
                    index_path,
                    experiment_id=experiment_id,
                    track=track,
                    seed=seed,
                    repetition=repetition,
                    pair_runs=pair_runs,
                )
                run_ids = {point.left.run_id, point.right.run_id}
                reused = run_ids & seen_runs
                if reused:
                    raise ValueError(
                        f"run bundles were reused across report pairs: {sorted(reused)}"
                    )
                seen_runs.update(run_ids)
                points_by_track[track].append(point)
        index_digest = _sha256_file(index_path)
        sources.append(f"evidence/indices/{index_digest}.json")
        source_paths.append(str(index_path))
    tracks = {
        track: _summarize_track(
            sorted(
                points,
                key=lambda point: (
                    point.experiment_id,
                    point.scenario_seed,
                    point.repetition,
                ),
            ),
            bootstrap_seed=bootstrap_seed,
            bootstrap_resamples=bootstrap_resamples,
        )
        for track, points in sorted(points_by_track.items())
    }
    if not tracks:
        raise ValueError("report inputs contained no paired runs")
    return PairedReport(
        source_indices=sources,
        source_index_paths=source_paths,
        bootstrap_seed=bootstrap_seed,
        bootstrap_resamples=bootstrap_resamples,
        tracks=tracks,
    )


def write_paired_report(report: PairedReport, output_dir: Path) -> tuple[Path, Path]:
    """Write a portable report and redacted evidence snapshot without overwriting."""

    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"report destination already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paired-report.json"
    html_path = output_dir / "paired-report.html"
    body = report.model_dump(mode="json")
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.tmp-",
        dir=output_dir.parent,
    ) as temporary:
        staging = Path(temporary)
        _write_private_text(
            staging / "paired-report.json",
            json.dumps(body, indent=2, sort_keys=True) + "\n",
        )
        _write_private_text(staging / "paired-report.html", _render_html(body))
        _write_evidence_snapshot(report, staging)
        staging.rename(output_dir)
    return json_path, html_path


def _load_pair(
    index_path: Path,
    *,
    experiment_id: str,
    track: str,
    seed: int,
    repetition: int,
    pair_runs: list[dict[str, Any]],
) -> PairedPoint:
    bundles = [_resolve_bundle(index_path, run) for run in pair_runs]
    manifests = [
        RunManifest.model_validate_json((bundle / "manifest.json").read_text(encoding="utf-8"))
        for bundle in bundles
    ]
    left_index = 0
    right_index = 1
    preflight_paths = [bundle / "native" / "pair-contract-preflight.json" for bundle in bundles]
    preflights = [_load_object(path, "pair contract preflight") for path in preflight_paths]
    if preflights[0] != preflights[1]:
        raise ValueError(f"paired bundles contain different contract preflights: {index_path}")
    preflight = preflights[0]
    preflight_path = preflight_paths[0]
    frozen_preflight_hash = preflight.get("snapshot_sha256")
    preflight_body = {key: value for key, value in preflight.items() if key != "snapshot_sha256"}
    if not isinstance(frozen_preflight_hash, str) or frozen_preflight_hash != sha256_payload(
        preflight_body
    ):
        raise ValueError(f"pair preflight snapshot hash is invalid: {preflight_path}")
    for run in pair_runs:
        if run.get("contract_preflight_sha256") != frozen_preflight_hash:
            raise ValueError(f"index preflight hash differs from paired bundles: {index_path}")
    if preflight.get("track") != track:
        raise ValueError(f"pair preflight track differs from experiment index: {index_path}")
    lanes = preflight.get("lanes")
    if (
        not isinstance(lanes, list)
        or len(lanes) != 2
        or any(not isinstance(lane, str) for lane in lanes)
    ):
        raise ValueError(f"pair preflight has invalid lane order: {preflight_path}")
    by_lane = {manifest.lane.value: index for index, manifest in enumerate(manifests)}
    if set(by_lane) != set(lanes):
        raise ValueError(f"pair index lanes differ from the frozen preflight: {index_path}")
    left_index, right_index = by_lane[lanes[0]], by_lane[lanes[1]]
    expected_manifest_experiment = f"{experiment_id}_native_workflow"
    for manifest in manifests:
        if manifest.experiment_id != expected_manifest_experiment:
            raise ValueError(f"bundle experiment identity differs from its index: {index_path}")
        if manifest.scenario_seed != seed or manifest.repetition != repetition:
            raise ValueError(f"bundle block identity differs from its index: {index_path}")
    left_bundle, right_bundle = bundles[left_index], bundles[right_index]
    validation = validate_pair(left_bundle, right_bundle, mark=False)
    if not validation.valid:
        codes = [finding.code for finding in validation.findings]
        raise ValueError(f"report pair failed provenance validation: {codes}")
    if validation.track != track:
        raise ValueError(f"index track {track!r} differs from validated track {validation.track!r}")
    left = _load_run_point(left_bundle, pair_runs[left_index])
    right = _load_run_point(right_bundle, pair_runs[right_index])
    if left.lane != lanes[0] or right.lane != lanes[1]:
        raise ValueError("report pair lane order could not be reconstructed")
    return PairedPoint(
        experiment_id=experiment_id,
        track=track,
        scenario_seed=seed,
        repetition=repetition,
        left=left,
        right=right,
    )


def _load_run_point(bundle: Path, index_run: dict[str, Any]) -> RunPoint:
    inventory_before = _bundle_inventory_sha256(bundle)
    manifest = RunManifest.model_validate_json(
        (bundle / "manifest.json").read_text(encoding="utf-8")
    )
    terminal = RunTerminal.model_validate_json(
        (bundle / "terminal.json").read_text(encoding="utf-8")
    )
    indexed_status = _required_string(index_run, "status", "experiment run")
    if indexed_status != terminal.status.value:
        raise ValueError(f"index status differs from bundle terminal: {bundle}")
    indexed_lane = _required_string(index_run, "lane", "experiment run")
    if indexed_lane != manifest.lane.value:
        raise ValueError(f"index lane differs from bundle manifest: {bundle}")
    score_path = bundle / "scores.json"
    score = (
        DeterministicScore.model_validate_json(score_path.read_text(encoding="utf-8"))
        if score_path.is_file()
        else None
    )
    completed = terminal.status is TerminalStatus.COMPLETED
    if completed and score is None:
        raise ValueError(f"completed report run has no deterministic score: {bundle}")
    components = (
        {component.name: component.raw_score for component in score.components}
        if score is not None
        else {}
    )
    measurements = _load_run_measurements(bundle, terminal)
    inventory_after = _bundle_inventory_sha256(bundle)
    if inventory_before != inventory_after:
        raise ValueError(f"run bundle changed while report point was loaded: {bundle}")
    run_id = str(manifest.run_id)
    return RunPoint(
        run_id=run_id,
        bundle=f"evidence/runs/{run_id}",
        source_bundle=str(bundle),
        source_bundle_inventory_sha256=inventory_after,
        lane=manifest.lane.value,
        terminal_status=terminal.status,
        operational_failure=not completed,
        task_success=bool(score.task_success) if completed and score is not None else False,
        total_score=score.total_score if score is not None else None,
        elapsed_seconds=terminal.elapsed_seconds,
        model_calls=terminal.used_model_calls,
        tool_calls=terminal.used_tool_calls,
        output_tokens=terminal.used_output_tokens,
        **measurements,
        components=components,
    )


def _load_run_measurements(bundle: Path, terminal: RunTerminal) -> dict[str, Any]:
    model_records = _load_jsonl(bundle / "metrics" / "model.jsonl")
    model_calls = [record for record in model_records if record.get("record_type") == "model_call"]
    model_summary = next(
        (
            record
            for record in reversed(model_records)
            if record.get("record_type") == "model_metrics_summary"
        ),
        {},
    )
    input_tokens = _complete_nested_sum(model_calls, ("tokens", "input"))
    cached_tokens = _complete_nested_sum(model_calls, ("tokens", "cached_input"))
    reasoning_tokens = _complete_nested_sum(model_calls, ("tokens", "reasoning"))
    queue_seconds = _complete_sum(model_calls, "queue_seconds")
    ttft_seconds = _complete_sum(model_calls, "time_to_first_token_seconds")
    generation_seconds = _complete_sum(model_calls, "generation_seconds")
    model_call_time = _complete_sum(model_calls, "total_latency_seconds")
    task_latency = _optional_number(model_summary.get("task_latency_seconds"))

    control_records = _load_jsonl(bundle / "metrics" / "control.jsonl")
    control_total = _phase_duration(control_records, "control_plane_total")
    control_overhead = (
        max(0.0, control_total - task_latency)
        if control_total is not None and task_latency is not None
        else None
    )

    host_records = _load_jsonl(bundle / "metrics" / "host.jsonl")
    resource_samples = [
        record for record in host_records if record.get("record_type") == "resource_sample"
    ]
    resource_summary = next(
        (
            record
            for record in reversed(host_records)
            if record.get("record_type") == "sampling_summary"
        ),
        {},
    )
    gpu_devices = [
        device
        for sample in resource_samples
        for device in (
            sample.get("gpu", {}).get("devices", []) if isinstance(sample.get("gpu"), dict) else []
        )
        if isinstance(device, dict)
    ]
    host_samples = [
        sample["host"] for sample in resource_samples if isinstance(sample.get("host"), dict)
    ]

    trace_link = _load_optional_object(bundle / "native" / "trace-link.json")
    completeness = trace_link.get("completeness")
    trace_ratio = (
        _optional_number(completeness.get("ratio")) if isinstance(completeness, dict) else None
    )
    span_count = trace_link.get("span_count")

    actors = {
        actor for record in model_calls if isinstance((actor := record.get("actor")), str) and actor
    }
    return {
        "input_tokens": _optional_int(input_tokens),
        "cached_input_tokens": _optional_int(cached_tokens),
        "reasoning_tokens": _optional_int(reasoning_tokens),
        "task_latency_seconds": task_latency,
        "control_overhead_seconds": control_overhead,
        "provider_queue_seconds": queue_seconds,
        "provider_ttft_seconds": ttft_seconds,
        "provider_generation_seconds": generation_seconds,
        "model_call_time_seconds": model_call_time,
        "actors_observed": len(actors) if model_calls else None,
        "max_in_flight_model_calls": _max_concurrent_calls(model_calls),
        "resource_samples": (
            resource_summary.get("sample_count")
            if isinstance(resource_summary.get("sample_count"), int)
            else len(resource_samples)
            if resource_samples
            else None
        ),
        "mean_sampling_overhead_ms": _optional_number(
            resource_summary.get("mean_sampling_overhead_ms")
        ),
        "max_cadence_error_ms": _optional_number(resource_summary.get("max_cadence_error_ms")),
        "gpu_peak_utilization_percent": _maximum(gpu_devices, "utilization_percent"),
        "gpu_peak_memory_mib": _maximum(gpu_devices, "memory_used_mib"),
        "gpu_peak_power_watts": _maximum(gpu_devices, "power_watts"),
        "gpu_peak_temperature_celsius": _maximum(gpu_devices, "temperature_celsius"),
        "host_peak_cpu_percent": _maximum(host_samples, "cpu_utilization_percent"),
        "host_peak_memory_mib": _maximum(host_samples, "memory_used_mib"),
        "trace_completeness": trace_ratio,
        "native_span_count": span_count if isinstance(span_count, int) else None,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("record is not an object")
            records.append(record)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"could not read metrics JSONL at {path}: {exc}") from exc
    return records


def _load_optional_object(path: Path) -> dict[str, Any]:
    return _load_object(path, path.name) if path.is_file() else {}


def _optional_number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _complete_sum(records: list[dict[str, Any]], key: str) -> float | None:
    if not records:
        return None
    values = [_optional_number(record.get(key)) for record in records]
    return (
        sum(value for value in values if value is not None)
        if all(value is not None for value in values)
        else None
    )


def _complete_nested_sum(records: list[dict[str, Any]], path: tuple[str, str]) -> int | None:
    if not records:
        return None
    values = []
    for record in records:
        parent = record.get(path[0])
        value = parent.get(path[1]) if isinstance(parent, dict) else None
        values.append(_optional_int(value))
    return (
        sum(value for value in values if value is not None)
        if all(value is not None for value in values)
        else None
    )


def _phase_duration(records: list[dict[str, Any]], phase: str) -> float | None:
    record = next(
        (
            item
            for item in records
            if item.get("record_type") == "control_phase" and item.get("phase") == phase
        ),
        None,
    )
    return _optional_number(record.get("duration_seconds")) if record is not None else None


def _maximum(records: list[dict[str, Any]], key: str) -> float | None:
    values = [_optional_number(record.get(key)) for record in records]
    collected = [value for value in values if value is not None]
    return max(collected) if collected else None


def _max_concurrent_calls(records: list[dict[str, Any]]) -> int | None:
    boundaries = []
    for record in records:
        started = record.get("started_monotonic_ns")
        finished = record.get("finished_monotonic_ns")
        if not isinstance(started, int) or not isinstance(finished, int) or finished < started:
            return None
        boundaries.extend(((started, 1), (finished, -1)))
    if not boundaries:
        return None
    active = 0
    maximum = 0
    for _timestamp, delta in sorted(boundaries, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def _summarize_track(
    points: list[PairedPoint],
    *,
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> TrackReport:
    lanes = (points[0].left.lane, points[0].right.lane)
    if any((point.left.lane, point.right.lane) != lanes for point in points):
        raise ValueError("one report track contains inconsistent lane identities")
    total = len(points)
    failure_rate = {
        lanes[0]: sum(point.left.operational_failure for point in points) / total,
        lanes[1]: sum(point.right.operational_failure for point in points) / total,
    }
    task_success = _binary_effect(
        [point.left.task_success for point in points],
        [point.right.task_success for point in points],
    )
    metrics = {
        "total_score": "total_score",
        "elapsed_seconds": "elapsed_seconds",
        "task_latency_seconds": "task_latency_seconds",
        "control_overhead_seconds": "control_overhead_seconds",
        "model_calls": "model_calls",
        "tool_calls": "tool_calls",
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "reasoning_tokens": "reasoning_tokens",
        "provider_queue_seconds": "provider_queue_seconds",
        "provider_ttft_seconds": "provider_ttft_seconds",
        "provider_generation_seconds": "provider_generation_seconds",
        "model_call_time_seconds": "model_call_time_seconds",
        "actors_observed": "actors_observed",
        "max_in_flight_model_calls": "max_in_flight_model_calls",
        "resource_samples": "resource_samples",
        "mean_sampling_overhead_ms": "mean_sampling_overhead_ms",
        "max_cadence_error_ms": "max_cadence_error_ms",
        "gpu_peak_utilization_percent": "gpu_peak_utilization_percent",
        "gpu_peak_memory_mib": "gpu_peak_memory_mib",
        "gpu_peak_power_watts": "gpu_peak_power_watts",
        "gpu_peak_temperature_celsius": "gpu_peak_temperature_celsius",
        "host_peak_cpu_percent": "host_peak_cpu_percent",
        "host_peak_memory_mib": "host_peak_memory_mib",
        "trace_completeness": "trace_completeness",
        "native_span_count": "native_span_count",
    }
    continuous = {}
    for offset, (name, attribute) in enumerate(metrics.items()):
        values = []
        for point in points:
            left = getattr(point.left, attribute)
            right = getattr(point.right, attribute)
            if left is not None and right is not None:
                values.append((float(left), float(right)))
        continuous[name] = _continuous_effect(
            values,
            pairs_total=total,
            seed=bootstrap_seed + offset,
            resamples=bootstrap_resamples,
        )
    component_values: dict[str, dict[str, list[float]]] = {
        lanes[0]: defaultdict(list),
        lanes[1]: defaultdict(list),
    }
    for point in points:
        for name, value in point.left.components.items():
            component_values[lanes[0]][name].append(value)
        for name, value in point.right.components.items():
            component_values[lanes[1]][name].append(value)
    component_discrimination = {
        lane: {
            name: _component_discrimination(values) for name, values in sorted(by_component.items())
        }
        for lane, by_component in component_values.items()
    }
    return TrackReport(
        lanes=lanes,
        paired_points=points,
        operational_failure_rate=failure_rate,
        task_success=task_success,
        continuous_effects=continuous,
        component_discrimination=component_discrimination,
    )


def _continuous_effect(
    values: list[tuple[float, float]],
    *,
    pairs_total: int,
    seed: int,
    resamples: int,
) -> ContinuousEffect:
    if not values:
        return ContinuousEffect(pairs_total=pairs_total, pairs_analyzed=0)
    left = [pair[0] for pair in values]
    right = [pair[1] for pair in values]
    differences = [right_value - left_value for left_value, right_value in values]
    mean_ci, median_ci = _bootstrap_intervals(differences, seed=seed, resamples=resamples)
    return ContinuousEffect(
        pairs_total=pairs_total,
        pairs_analyzed=len(values),
        left_median=statistics.median(left),
        right_median=statistics.median(right),
        median_difference_right_minus_left=statistics.median(differences),
        mean_difference_right_minus_left=statistics.fmean(differences),
        mean_difference_ci95=mean_ci,
        median_difference_ci95=median_ci,
    )


def _bootstrap_intervals(
    differences: list[float],
    *,
    seed: int,
    resamples: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    randomizer = random.Random(seed)
    means = []
    medians = []
    for _ in range(resamples):
        sample = randomizer.choices(differences, k=len(differences))
        means.append(statistics.fmean(sample))
        medians.append(statistics.median(sample))
    means.sort()
    medians.sort()
    return (_percentile(means, 0.025), _percentile(means, 0.975)), (
        _percentile(medians, 0.025),
        _percentile(medians, 0.975),
    )


def _percentile(values: list[float], probability: float) -> float:
    position = probability * (len(values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def _binary_effect(left: list[bool], right: list[bool]) -> BinaryEffect:
    left_only = sum(
        left_value and not right_value for left_value, right_value in zip(left, right, strict=True)
    )
    right_only = sum(
        right_value and not left_value for left_value, right_value in zip(left, right, strict=True)
    )
    discordant = left_only + right_only
    p_value = 1.0
    if discordant:
        tail = sum(
            math.comb(discordant, count) for count in range(min(left_only, right_only) + 1)
        ) / (2**discordant)
        p_value = min(1.0, 2 * tail)
    return BinaryEffect(
        pairs_total=len(left),
        left_successes=sum(left),
        right_successes=sum(right),
        left_only_successes=left_only,
        right_only_successes=right_only,
        paired_risk_difference_right_minus_left=(sum(right) - sum(left)) / len(left),
        exact_mcnemar_p_two_sided=p_value,
    )


def _component_discrimination(values: list[float]) -> ComponentDiscrimination:
    distinct = sorted(set(values))
    return ComponentDiscrimination(
        observations=len(values),
        distinct_raw_scores=distinct,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        discriminates_observed_outcomes=len(distinct) > 1,
    )


def _resolve_bundle(index_path: Path, run: dict[str, Any]) -> Path:
    value = Path(_required_string(run, "bundle", "experiment run"))
    return value.resolve() if value.is_absolute() else (index_path.parent / value).resolve()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def _required_int(mapping: dict[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label}.{key} must be an integer")
    return value


def _write_evidence_snapshot(report: PairedReport, staging: Path) -> None:
    run_points: dict[str, RunPoint] = {}
    pair_entries = []
    for track_report in report.tracks.values():
        for point in track_report.paired_points:
            run_points[point.left.run_id] = point.left
            run_points[point.right.run_id] = point.right
            validation = validate_pair(
                Path(point.left.source_bundle),
                Path(point.right.source_bundle),
                mark=False,
            )
            if not validation.valid:
                codes = [finding.code for finding in validation.findings]
                raise ValueError(f"report evidence changed during export: {codes}")
            pair_key = (
                f"{point.experiment_id}:{point.track}:{point.scenario_seed}:{point.repetition}"
            )
            pair_name = hashlib.sha256(pair_key.encode()).hexdigest()
            pair_path = f"evidence/pairs/{pair_name}.json"
            pair_payload = validation.model_dump(mode="json")
            pair_payload["left_bundle"] = point.left.bundle
            pair_payload["right_bundle"] = point.right.bundle
            pair_payload["left"]["bundle_path"] = point.left.bundle
            pair_payload["right"]["bundle_path"] = point.right.bundle
            _write_private_json(staging / pair_path, pair_payload)
            pair_entries.append(
                {
                    "pair_key": pair_key,
                    "path": pair_path,
                    "sha256": _sha256_file(staging / pair_path),
                }
            )
            for run_point, result in (
                (point.left, validation.left),
                (point.right, validation.right),
            ):
                validation_payload = result.model_dump(mode="json")
                validation_payload["bundle_path"] = run_point.bundle
                _write_private_json(
                    staging / run_point.bundle / "validation.json",
                    validation_payload,
                    allow_existing=True,
                )

    source_entries = []
    for source_path_value, retained_path in zip(
        report.source_index_paths,
        report.source_indices,
        strict=True,
    ):
        source_path = Path(source_path_value)
        source_digest = _sha256_file(source_path)
        if Path(retained_path).stem != source_digest:
            raise ValueError(f"experiment index changed during report export: {source_path}")
        redacted = _redact_experiment_index(source_path, run_points)
        if _sha256_file(source_path) != source_digest:
            raise ValueError(f"experiment index changed during report export: {source_path}")
        _write_private_json(staging / retained_path, redacted)
        source_entries.append(
            {
                "path": retained_path,
                "source_sha256": _sha256_file(source_path),
                "retained_sha256": _sha256_file(staging / retained_path),
            }
        )

    run_entries = []
    retained_paths = (
        "manifest.json",
        "manifest.sha256",
        "terminal.json",
        "scores.json",
        "metrics/host.jsonl",
        "metrics/model.jsonl",
        "metrics/control.jsonl",
        "native/pair-contract-preflight.json",
        "native/trace-link.json",
        "native/tracing-profile.json",
        "native/tracing-profile.sha256",
    )
    for run_id, point in sorted(run_points.items()):
        source_bundle = Path(point.source_bundle)
        retained = []
        for relative in retained_paths:
            source = source_bundle / relative
            if not source.is_file():
                continue
            destination = staging / point.bundle / relative
            _copy_private_file(source, destination)
            retained.append(
                {
                    "path": f"{point.bundle}/{relative}",
                    "sha256": _sha256_file(destination),
                    "size_bytes": destination.stat().st_size,
                }
            )
        raw_inventory = []
        for source in sorted(path for path in source_bundle.rglob("*") if path.is_file()):
            if source.is_symlink():
                raise ValueError(f"run bundle contains a symlink: {source}")
            relative = source.relative_to(source_bundle).as_posix()
            private_path = f"private-runs/{run_id}/{relative}"
            private_destination = staging / private_path
            _copy_private_file(source, private_destination)
            source_digest = _sha256_file(source)
            retained_digest = _sha256_file(private_destination)
            if source_digest != retained_digest:
                raise ValueError(f"run bundle file changed while copied: {source}")
            raw_inventory.append(
                {
                    "path": relative,
                    "retained_path": private_path,
                    "sha256": retained_digest,
                    "size_bytes": source.stat().st_size,
                    "retention": "private_snapshot",
                }
            )
        if _bundle_inventory_sha256(source_bundle) != point.source_bundle_inventory_sha256:
            raise ValueError(f"run bundle changed during report export: {source_bundle}")
        validation_path = staging / point.bundle / "validation.json"
        retained.append(
            {
                "path": f"{point.bundle}/validation.json",
                "sha256": _sha256_file(validation_path),
                "size_bytes": validation_path.stat().st_size,
            }
        )
        run_entries.append(
            {
                "run_id": run_id,
                "lane": point.lane,
                "path": point.bundle,
                "private_bundle_path": f"private-runs/{run_id}",
                "retained_files": sorted(retained, key=lambda item: item["path"]),
                "source_file_inventory": raw_inventory,
            }
        )

    archived_files = []
    evidence_root = staging / "evidence"
    for path in sorted(item for item in evidence_root.rglob("*") if item.is_file()):
        relative = path.relative_to(staging).as_posix()
        archived_files.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    evidence_index = {
        "schema_version": EVIDENCE_INDEX_SCHEMA_VERSION,
        "report_schema_version": report.schema_version,
        "retention_policy": {
            "portable_redacted_snapshot": "retained",
            "raw_events_artifacts_checkpoints_spans": "private_snapshot",
            "publication_rule": (
                "Publish paired-report files and evidence-index metadata only after review; "
                "never publish private-runs and review evidence/runs for host identifiers."
            ),
            "audit_rule": (
                "Recompute SHA-256 over a private-runs file and compare it with the "
                "source_file_inventory entry."
            ),
        },
        "report_files": [
            {
                "path": name,
                "sha256": _sha256_file(staging / name),
                "size_bytes": (staging / name).stat().st_size,
            }
            for name in ("paired-report.json", "paired-report.html")
        ],
        "source_indices": source_entries,
        "pairs": sorted(pair_entries, key=lambda item: item["pair_key"]),
        "runs": run_entries,
        "archived_files": archived_files,
    }
    evidence_index_path = staging / "evidence-index.json"
    _write_private_json(evidence_index_path, evidence_index)
    digest = _sha256_file(evidence_index_path)
    _write_private_text(staging / "evidence-index.sha256", f"{digest}  evidence-index.json\n")


def _redact_experiment_index(
    source_path: Path,
    run_points: dict[str, RunPoint],
) -> dict[str, Any]:
    source = _load_object(source_path, "experiment index")
    redacted = {
        "schema_version": REDACTED_INDEX_SCHEMA_VERSION,
        "source_schema_version": source.get("schema_version"),
        "source_sha256": _sha256_file(source_path),
    }
    for key in ("execution_mode", "experiment_id", "order_seed", "status", "case_selection"):
        if key in source:
            redacted[key] = source[key]
    redacted_blocks = []
    blocks = source.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError(f"experiment index blocks must be a list: {source_path}")
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError(f"experiment block must be an object: {source_path}")
        retained_block = {
            key: value
            for key, value in block.items()
            if key
            in {
                "case",
                "contract_preflights",
                "lane_order",
                "order_seed",
                "pair_validation",
                "repetition",
                "seed",
            }
        }
        retained_runs = []
        for run in block.get("runs", []):
            if not isinstance(run, dict):
                raise ValueError(f"experiment run must be an object: {source_path}")
            run_id = run.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                resolved = _resolve_bundle(source_path, run)
                run_id = next(
                    (
                        candidate.run_id
                        for candidate in run_points.values()
                        if Path(candidate.source_bundle) == resolved
                    ),
                    None,
                )
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("experiment run has no resolvable run identity")
            point = run_points.get(run_id)
            if point is None:
                raise ValueError(f"source index contains an unreported run: {run_id}")
            retained_run = {
                key: value
                for key, value in run.items()
                if key
                in {
                    "bundle_valid",
                    "contract_preflight_sha256",
                    "inference_status",
                    "lane",
                    "native_trace",
                    "pair_id",
                    "reason",
                    "run_id",
                    "status",
                    "task_success",
                    "total_score",
                    "track",
                    "world_manifest_sha256",
                }
            }
            retained_run["bundle"] = point.bundle
            retained_runs.append(retained_run)
        retained_block["runs"] = retained_runs
        redacted_blocks.append(retained_block)
    redacted["blocks"] = redacted_blocks
    return redacted


def _write_private_json(
    path: Path,
    payload: dict[str, Any],
    *,
    allow_existing: bool = False,
) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if allow_existing and path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise ValueError(f"conflicting retained evidence payload: {path}")
        return
    _write_private_text(path, rendered)


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(0o600)


def _copy_private_file(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"retained evidence file is a symlink: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        while chunk := source_handle.read(1024 * 1024):
            destination_handle.write(chunk)
    destination.chmod(0o600)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_inventory_sha256(bundle: Path) -> str:
    inventory = []
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise ValueError(f"run bundle contains a symlink: {path}")
        inventory.append(
            {
                "path": path.relative_to(bundle).as_posix(),
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _render_html(body: dict[str, Any]) -> str:
    sections = []
    for track, report in body["tracks"].items():
        lanes = report["lanes"]
        rows = []
        for point in report["paired_points"]:
            left = point["left"]
            right = point["right"]
            left_uri = html.escape(left["bundle"], quote=True)
            right_uri = html.escape(right["bundle"], quote=True)
            rows.append(
                "<tr>"
                f"<td>{point['scenario_seed']}</td><td>{point['repetition']}</td>"
                f'<td><a href="{left_uri}">{html.escape(left["run_id"])}</a></td>'
                f"<td>{html.escape(left['terminal_status'])}</td>"
                f"<td>{'' if left['total_score'] is None else left['total_score']}</td>"
                f'<td><a href="{right_uri}">{html.escape(right["run_id"])}</a></td>'
                f"<td>{html.escape(right['terminal_status'])}</td>"
                f"<td>{'' if right['total_score'] is None else right['total_score']}</td>"
                "</tr>"
            )
        effect_rows = []
        for metric, effect in report["continuous_effects"].items():
            effect_rows.append(
                "<tr>"
                f"<td>{html.escape(metric)}</td><td>{effect['pairs_analyzed']}/"
                f"{effect['pairs_total']}</td>"
                f"<td>{_display(effect['left_median'])}</td>"
                f"<td>{_display(effect['right_median'])}</td>"
                f"<td>{_display(effect['mean_difference_right_minus_left'])}</td>"
                f"<td>{_display(effect['mean_difference_ci95'])}</td>"
                "</tr>"
            )
        component_rows = []
        for lane, components in report["component_discrimination"].items():
            for component, result in components.items():
                component_rows.append(
                    "<tr>"
                    f"<td>{html.escape(lane)}</td><td>{html.escape(component)}</td>"
                    f"<td>{result['observations']}</td>"
                    f"<td>{html.escape(str(result['distinct_raw_scores']))}</td>"
                    f"<td>{result['discriminates_observed_outcomes']}</td>"
                    "</tr>"
                )
        binary = report["task_success"]
        failures = report["operational_failure_rate"]
        plots = _render_paired_plots(report)
        sections.append(
            f"<section><h2>{html.escape(track)}</h2>"
            f"<p>{html.escape(lanes[0])} vs {html.escape(lanes[1])}; "
            f"{len(report['paired_points'])} paired blocks.</p>"
            f"<p>Operational failure rates: {html.escape(lanes[0])} "
            f"{failures[lanes[0]]:.3f}; {html.escape(lanes[1])} "
            f"{failures[lanes[1]]:.3f}. Task successes: {binary['left_successes']} vs "
            f"{binary['right_successes']}; discordant left-only/right-only: "
            f"{binary['left_only_successes']}/{binary['right_only_successes']}; exact "
            f"McNemar p={_display(binary['exact_mcnemar_p_two_sided'])}.</p>"
            "<h3>Paired effects (right minus left)</h3><table><thead><tr><th>Metric</th>"
            "<th>Analyzed/total</th><th>Left median</th><th>Right median</th>"
            "<th>Mean difference</th><th>95% bootstrap CI</th></tr></thead>"
            f"<tbody>{''.join(effect_rows)}</tbody></table>"
            "<h3>Paired raw-point plots</h3>"
            f"{plots}"
            "<h3>Component discrimination</h3><table><thead><tr><th>Lane</th>"
            "<th>Component</th><th>N</th><th>Distinct raw scores</th>"
            "<th>Discriminates</th></tr></thead>"
            f"<tbody>{''.join(component_rows)}</tbody></table>"
            "<h3>Raw paired points</h3><table><thead><tr><th>Seed</th><th>Rep</th>"
            f"<th>{html.escape(lanes[0])} bundle</th><th>status</th><th>score</th>"
            f"<th>{html.escape(lanes[1])} bundle</th><th>status</th><th>score</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>"
        )
    embedded = html.escape(json.dumps(body, sort_keys=True))
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Agent Lab paired '
        "report</title><style>body{font-family:system-ui;max-width:1100px;margin:2rem auto;"
        "padding:0 1rem}table{border-collapse:collapse;width:100%}th,td{border:1px solid "
        "#ccc;padding:.4rem;text-align:left}code{white-space:pre-wrap}.paired-plot{border:1px "
        "solid #ddd;margin:.5rem 0;max-width:100%}.plot-left{fill:#2563eb}.plot-right{fill:"
        "#16a34a}.plot-pair{stroke:#64748b;stroke-width:1}</style></head><body>"
        "<h1>Agent Lab paired report</h1>"
        "<p>Generated offline from revalidated immutable bundles. Right-minus-left effects "
        "preserve the frozen preflight lane order; operational failures remain in denominators."
        f"</p>{''.join(sections)}<details><summary>Machine-readable report</summary>"
        f"<code>{embedded}</code></details></body></html>\n"
    )


def _render_paired_plots(report: dict[str, Any]) -> str:
    rendered = [
        '<p><span style="color:#2563eb">&#9679; left</span> &nbsp; '
        '<span style="color:#16a34a">&#9679; right</span></p>'
    ]
    for metric, label in PLOT_METRICS:
        values = []
        for point in report["paired_points"]:
            left = _plot_value(point["left"], metric)
            right = _plot_value(point["right"], metric)
            if left is not None and right is not None:
                values.append((point["scenario_seed"], point["repetition"], left, right))
        if not values:
            continue
        extent = [value for _seed, _repetition, left, right in values for value in (left, right)]
        minimum = min(extent)
        maximum = max(extent)

        def x_position(
            value: float,
            lower: float = minimum,
            upper: float = maximum,
        ) -> float:
            if lower == upper:
                return 390.0
            return 180 + (value - lower) / (upper - lower) * 420

        height = 42 + 26 * len(values)
        marks = []
        for index, (seed, repetition, left, right) in enumerate(values):
            y = 34 + index * 26
            left_x = x_position(left)
            right_x = x_position(right)
            marks.append(
                f'<text x="5" y="{y + 4}" font-size="11">seed {seed} / rep {repetition}</text>'
                f'<line class="plot-pair" x1="{left_x:.2f}" y1="{y}" '
                f'x2="{right_x:.2f}" y2="{y}"/>'
                f'<circle class="plot-left" cx="{left_x:.2f}" cy="{y}" r="5">'
                f"<title>left: {left:.6g}</title></circle>"
                f'<circle class="plot-right" cx="{right_x:.2f}" cy="{y}" r="5">'
                f"<title>right: {right:.6g}</title></circle>"
            )
        rendered.append(
            f'<h4>{html.escape(label)}</h4><svg class="paired-plot" role="img" '
            f'aria-label="{html.escape(label, quote=True)} paired plot" '
            f'viewBox="0 0 640 {height}"><text x="180" y="14" font-size="10">'
            f'{minimum:.6g}</text><text x="600" y="14" text-anchor="end" '
            f'font-size="10">{maximum:.6g}</text>{"".join(marks)}</svg>'
        )
    return "".join(rendered)


def _plot_value(run: dict[str, Any], path: str) -> float | None:
    value: Any = run
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _optional_number(value)


def _display(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return html.escape(str(value))
