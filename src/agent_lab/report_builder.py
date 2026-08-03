"""Offline paired report generation from immutable, provenance-valid bundles."""

from __future__ import annotations

import html
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_lab.bundle_validation import validate_pair
from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import RunManifest, RunTerminal, TerminalStatus
from agent_lab.evaluator import DeterministicScore

REPORT_SCHEMA_VERSION = "paired-report/v1"


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunPoint(ReportModel):
    run_id: str
    bundle: str
    lane: str
    terminal_status: TerminalStatus
    operational_failure: bool
    task_success: bool
    total_score: float | None = Field(default=None, ge=0, le=1)
    elapsed_seconds: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
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
    schema_version: Literal["paired-report/v1"] = REPORT_SCHEMA_VERSION
    source_indices: list[str]
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
        sources.append(str(index_path))
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
        bootstrap_seed=bootstrap_seed,
        bootstrap_resamples=bootstrap_resamples,
        tracks=tracks,
    )


def write_paired_report(report: PairedReport, output_dir: Path) -> tuple[Path, Path]:
    """Write machine-readable and self-contained HTML reports without overwriting."""

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paired-report.json"
    html_path = output_dir / "paired-report.html"
    if json_path.exists() or html_path.exists():
        raise FileExistsError(f"report destination already contains output: {output_dir}")
    body = report.model_dump(mode="json")
    json_path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(_render_html(body), encoding="utf-8")
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
    return RunPoint(
        run_id=str(manifest.run_id),
        bundle=str(bundle),
        lane=manifest.lane.value,
        terminal_status=terminal.status,
        operational_failure=not completed,
        task_success=bool(score.task_success) if completed and score is not None else False,
        total_score=score.total_score if score is not None else None,
        elapsed_seconds=terminal.elapsed_seconds,
        model_calls=terminal.used_model_calls,
        tool_calls=terminal.used_tool_calls,
        output_tokens=terminal.used_output_tokens,
        components=components,
    )


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
        "total_score": ("total_score", False),
        "elapsed_seconds": ("elapsed_seconds", True),
        "model_calls": ("model_calls", True),
        "tool_calls": ("tool_calls", True),
        "output_tokens": ("output_tokens", True),
    }
    continuous = {}
    for offset, (name, (attribute, include_failures)) in enumerate(metrics.items()):
        values = []
        for point in points:
            left = getattr(point.left, attribute)
            right = getattr(point.right, attribute)
            if include_failures or (left is not None and right is not None):
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


def _render_html(body: dict[str, Any]) -> str:
    sections = []
    for track, report in body["tracks"].items():
        lanes = report["lanes"]
        rows = []
        for point in report["paired_points"]:
            left = point["left"]
            right = point["right"]
            left_uri = html.escape(Path(left["bundle"]).as_uri(), quote=True)
            right_uri = html.escape(Path(right["bundle"]).as_uri(), quote=True)
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
        "#ccc;padding:.4rem;text-align:left}code{white-space:pre-wrap}</style></head><body>"
        "<h1>Agent Lab paired report</h1>"
        "<p>Generated offline from revalidated immutable bundles. Right-minus-left effects "
        "preserve the frozen preflight lane order; operational failures remain in denominators."
        f"</p>{''.join(sections)}<details><summary>Machine-readable report</summary>"
        f"<code>{embedded}</code></details></body></html>\n"
    )


def _display(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return html.escape(str(value))
