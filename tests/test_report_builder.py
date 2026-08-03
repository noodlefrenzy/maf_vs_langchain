from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import Budget, FrameworkLane, ModelProfile, RunManifest, RunTerminal
from agent_lab.evaluator import ComponentScore, DeterministicScore
from agent_lab.report_builder import build_paired_report, write_paired_report


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
        return SimpleNamespace(valid=True, track="explicit_workflow", findings=[])

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
    assert track.task_success.pairs_total == 2
    assert track.task_success.left_successes == 1
    assert track.task_success.right_successes == 1
    assert track.continuous_effects["total_score"].pairs_total == 2
    assert track.continuous_effects["total_score"].pairs_analyzed == 1
    assert track.continuous_effects["elapsed_seconds"].pairs_analyzed == 2
    assert (
        track.component_discrimination["maf_workflow"][
            "correct_and_effective_containment"
        ].discriminates_observed_outcomes
        is False
    )

    json_path, html_path = write_paired_report(report, tmp_path / "report")
    assert json.loads(json_path.read_text())["schema_version"] == "paired-report/v1"
    assert "operational failures remain in denominators" in html_path.read_text()
    with pytest.raises(FileExistsError):
        write_paired_report(report, tmp_path / "report")


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
