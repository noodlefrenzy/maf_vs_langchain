"""Run paired native experiments against real isolated MCP world clones."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import socket
import subprocess
from pathlib import Path
from typing import Any, Literal

import uvicorn

from agent_lab.async_cleanup import shutdown_server
from agent_lab.bundle_validation import validate_bundle, validate_pair
from agent_lab.config import load_experiment, load_model_profile
from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import ApprovalDecision, FrameworkLane, RunManifest, TerminalStatus
from agent_lab.control_plane import ControlPlane, resolve_lane_order
from agent_lab.execution_provenance import (
    build_execution_provenance,
    verify_execution_provenance,
)
from agent_lab.matched_contracts import preflight_pair_contract
from agent_lab.runner_host import NativeRunnerDescriptor
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from incident_lab_mcp.http_auth import RunCredentialSigner, RunWorldRegistry
from incident_lab_mcp.http_service import build_http_app, issue_endpoint_credentials
from incident_lab_mcp.roles import ROLE_TO_AGENT_ROLE
from incident_lab_mcp.workflow_finalization import finalize_workflow_bundle

_TRACK_PAIRS = (
    (
        "explicit_workflow",
        FrameworkLane.MAF_WORKFLOW,
        FrameworkLane.LANGGRAPH,
    ),
    (
        "opinionated_harness",
        FrameworkLane.MAF_HARNESS,
        FrameworkLane.DEEP_AGENTS,
    ),
)
_RUNNER_PROJECT = {
    FrameworkLane.MAF_WORKFLOW: ("runners/maf", "maf_runner.protocol_runner"),
    FrameworkLane.MAF_HARNESS: ("runners/maf", "maf_runner.protocol_runner"),
    FrameworkLane.LANGGRAPH: (
        "runners/langgraph",
        "langgraph_runner.protocol_runner",
    ),
    FrameworkLane.DEEP_AGENTS: (
        "runners/langgraph",
        "langgraph_runner.protocol_runner",
    ),
}


async def run_experiment_protocol_probe(
    *,
    repository_root: Path,
    experiment_path: Path,
    runs_dir: Path,
    order_seed: int,
) -> dict[str, Any]:
    """Resolve a profile and probe each matched lane without model or tool calls."""

    return await _run_experiment(
        repository_root=repository_root,
        experiment_path=experiment_path,
        runs_dir=runs_dir,
        order_seed=order_seed,
        execution_mode="protocol_probe",
    )


async def run_native_workflow_experiment(
    *,
    repository_root: Path,
    experiment_path: Path,
    runs_dir: Path,
    order_seed: int,
) -> dict[str, Any]:
    """Run and score one persistent matched native-framework experiment."""

    return await _run_experiment(
        repository_root=repository_root,
        experiment_path=experiment_path,
        runs_dir=runs_dir,
        order_seed=order_seed,
        execution_mode="native_workflow",
    )


async def _run_experiment(
    *,
    repository_root: Path,
    experiment_path: Path,
    runs_dir: Path,
    order_seed: int,
    execution_mode: Literal["protocol_probe", "native_workflow"],
) -> dict[str, Any]:
    """Resolve a profile and run each selected lane through its native process."""

    repository_root = repository_root.resolve()
    experiment_path = _resolve(repository_root, experiment_path)
    runs_dir = _resolve(repository_root, runs_dir)
    experiment = load_experiment(experiment_path)
    model = load_model_profile(_resolve(repository_root, Path(experiment.model_profile)))
    tracks = _selected_tracks(experiment.lanes)
    if execution_mode == "native_workflow" and len(tracks) != 1:
        raise ValueError("native workflow execution requires exactly one matched track")
    descriptors = {
        lane: _describe_runner(repository_root, lane)
        for _track, left, right in tracks
        for lane in (left, right)
    }
    hashes = contract_bundle_hashes()
    source_commit = _source_commit(repository_root)
    source_tree_sha256 = _source_tree_sha256(repository_root)
    execution_provenance_by_lane = {
        lane: build_execution_provenance(
            repository_root=repository_root,
            execution_mode=execution_mode,
            source_commit=source_commit,
            source_tree_sha256=source_tree_sha256,
            model=model,
            runtime_profile=experiment.inference_runtime,
        )
        for _track, left, right in tracks
        for lane in (left, right)
    }
    runs_dir.mkdir(parents=True, exist_ok=True)
    index_prefix = "protocol-probe" if execution_mode == "protocol_probe" else "native-workflow"
    index_path = runs_dir / f"{index_prefix}-{experiment.experiment_id}-order-{order_seed}.json"
    if index_path.exists():
        raise FileExistsError(f"experiment index already exists at {index_path}")
    control_plane = ControlPlane(runs_dir)
    results: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []

    for seed in experiment.seeds:
        for repetition in range(experiment.repetitions):
            block_order_seed = _block_order_seed(order_seed, seed)
            order = resolve_lane_order(
                experiment.lanes,
                policy=experiment.order,
                order_seed=block_order_seed,
                repetition=repetition,
            )
            registry = RunWorldRegistry()
            pair_by_lane = {}
            contract_preflights = {}
            for track, left_lane, right_lane in tracks:
                receipt = registry.create_pair(
                    pair_id=(f"{experiment.experiment_id}:{track}:seed-{seed}:rep-{repetition}"),
                    seed=seed,
                    left_lane=left_lane,
                    right_lane=right_lane,
                    ttl_seconds=max(
                        300,
                        experiment.budget.wall_clock_seconds * len(experiment.lanes) + 60,
                    ),
                    fault_profile=experiment.fault_profile,
                )
                pair_by_lane[left_lane] = (track, receipt.left)
                pair_by_lane[right_lane] = (track, receipt.right)
                contract_preflights[track] = preflight_pair_contract(
                    track=track,
                    left_lane=left_lane,
                    right_lane=right_lane,
                    descriptors=descriptors,
                    expected_contract_hashes=hashes,
                    experiment_id=experiment.experiment_id,
                    scenario=experiment.scenario,
                    scenario_seed=seed,
                    repetition=repetition,
                    model=model,
                    budget=experiment.budget,
                    fault_profile=experiment.fault_profile,
                    source_commit=source_commit,
                    source_tree_sha256=source_tree_sha256,
                    execution_provenance_by_lane=execution_provenance_by_lane,
                    world_manifest_sha256=receipt.left.world_manifest_sha256,
                )

            listener = _listener()
            port = listener.getsockname()[1]
            signer = RunCredentialSigner()
            app = build_http_app(registry, signer, port=port)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                    lifespan="on",
                )
            )
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                await _wait_until_started(server, server_task)
                credential_payload = issue_endpoint_credentials(
                    registry,
                    signer,
                    port=port,
                )
                block_runs = []
                for lane in order.lanes:
                    if lane not in pair_by_lane:
                        continue
                    verify_execution_provenance(
                        execution_provenance_by_lane[lane],
                        repository_root=repository_root,
                        execution_mode=execution_mode,
                        source_commit=source_commit,
                        source_tree_sha256=source_tree_sha256,
                        model=model,
                        runtime_profile=experiment.inference_runtime,
                    )
                    track, receipt = pair_by_lane[lane]
                    run = registry.get(receipt.run_id)
                    descriptor = descriptors[lane]
                    project, module = _RUNNER_PROJECT[lane]
                    preflight = contract_preflights[track]
                    manifest = RunManifest(
                        run_id=run.run_id,
                        experiment_id=f"{experiment.experiment_id}_{execution_mode}",
                        lane=lane,
                        scenario=experiment.scenario,
                        scenario_generator_version=run.world.incident.generator_version,
                        scenario_seed=seed,
                        repetition=repetition,
                        model=model,
                        budget=experiment.budget,
                        source_commit=source_commit,
                        source_tree_sha256=source_tree_sha256,
                        framework_versions=descriptor.framework_versions,
                        dependency_lock_sha256=_file_sha256(repository_root / project / "uv.lock"),
                        prompt_bundle_sha256=hashes["prompt_bundle_sha256"],
                        tool_catalog_sha256=hashes["tool_catalog_sha256"],
                        effective_request_policy_sha256=preflight["effective_request_policy"][
                            "lane_policy_sha256"
                        ][lane.value],
                        execution_provenance_sha256=preflight["execution_provenance_sha256"],
                        world_manifest_sha256=receipt.world_manifest_sha256,
                        host_fingerprint={
                            "hostname": platform.node(),
                            "platform": platform.platform(),
                            "python": platform.python_version(),
                            "execution_mode": execution_mode,
                        },
                    )
                    _bundle, paths = control_plane.allocate_run(manifest)
                    _write_json_exclusive(
                        paths.bundle / "native" / "pair-contract-preflight.json",
                        preflight,
                    )
                    request = RunRequest(
                        manifest=manifest,
                        paths=paths,
                        role_endpoints=_role_endpoints(
                            credential_payload,
                            run_id=str(run.run_id),
                        ),
                        public_ticket=str(run.world.incident.public_view()["brief"]),
                        order_seed=block_order_seed,
                        tracing=experiment.tracing,
                        capture_sensitive=experiment.capture_sensitive,
                        capture_reasoning=experiment.capture_reasoning,
                    )
                    command = [
                        "uv",
                        "run",
                        "--project",
                        str(repository_root / project),
                        "--no-sync",
                        "python",
                        "-m",
                        module,
                        "--protocol-probe" if execution_mode == "protocol_probe" else "--run",
                    ]
                    if execution_mode == "native_workflow":

                        async def approve(event, *, world=run.world):
                            return world.decide_mitigation(
                                str(event.payload["request_id"]),
                                ApprovalDecision.APPROVE,
                                decided_by="scripted-approval-policy-v1",
                            )

                        async def complete(_event, *, world=run.world, request=request):
                            return await finalize_workflow_bundle(request, world)

                        result = await control_plane.execute(
                            command,
                            request,
                            approval_handler=approve,
                            completion_handler=complete,
                        )
                    else:
                        result = await control_plane.execute(command, request)
                    checkpoint = run.world.save_checkpoint(
                        paths.run_root / "world" / "world-checkpoint.json"
                    )
                    run_result = {
                        "run_id": str(run.run_id),
                        "pair_id": run.pair_id,
                        "track": track,
                        "lane": lane.value,
                        "status": result.terminal.status.value,
                        "reason": result.terminal.reason,
                        "bundle": str(paths.bundle),
                        "world_checkpoint": str(checkpoint),
                        "world_manifest_sha256": receipt.world_manifest_sha256,
                        "contract_preflight_sha256": preflight["snapshot_sha256"],
                    }
                    trace_link_path = paths.bundle / "native" / "trace-link.json"
                    if trace_link_path.is_file():
                        trace_link = json.loads(trace_link_path.read_text(encoding="utf-8"))
                        run_result["native_trace"] = {
                            "collector_status": trace_link["collector_status"],
                            "span_count": trace_link["span_count"],
                            "completeness_ratio": trace_link["completeness"]["ratio"],
                            "phoenix_trace_url": trace_link["phoenix_trace_url"],
                        }
                    if execution_mode == "native_workflow":
                        validation = validate_bundle(paths.bundle, mark=True)
                        run_result["bundle_valid"] = validation.valid
                        run_result["inference_status"] = validation.inference_status
                        scores_path = paths.bundle / "scores.json"
                        if scores_path.is_file():
                            score = json.loads(scores_path.read_text(encoding="utf-8"))
                            run_result["task_success"] = score["task_success"]
                            run_result["total_score"] = score["total_score"]
                    results.append(run_result)
                    block_runs.append(run_result)
                block = {
                    "seed": seed,
                    "repetition": repetition,
                    "order_seed": block_order_seed,
                    "lane_order": [lane.value for lane in order.lanes],
                    "contract_preflights": contract_preflights,
                    "runs": block_runs,
                }
                if execution_mode == "native_workflow":
                    _track, left_lane, right_lane = tracks[0]
                    left = next(item for item in block_runs if item["lane"] == left_lane.value)
                    right = next(item for item in block_runs if item["lane"] == right_lane.value)
                    pair_validation = validate_pair(
                        Path(str(left["bundle"])),
                        Path(str(right["bundle"])),
                        mark=True,
                    )
                    block["pair_validation"] = {
                        "valid": pair_validation.valid,
                        "inference_status": pair_validation.inference_status,
                        "track": pair_validation.track,
                    }
                blocks.append(block)
            finally:
                await shutdown_server(server, server_task, listener)

    summary = {
        "schema_version": "0.1.0",
        "execution_mode": execution_mode,
        "experiment_id": experiment.experiment_id,
        "order_seed": order_seed,
        "status": (
            "ok"
            if all(
                item["status"] == TerminalStatus.COMPLETED.value
                and (execution_mode == "protocol_probe" or item.get("bundle_valid") is True)
                for item in results
            )
            and all(
                execution_mode == "protocol_probe"
                or block.get("pair_validation", {}).get("valid") is True
                for block in blocks
            )
            else "failed"
        ),
        "blocks": blocks,
        "runs": results,
    }
    _write_json_exclusive(index_path, summary)
    summary["index"] = str(index_path)
    return summary


def _selected_tracks(
    lanes: list[FrameworkLane],
) -> list[tuple[str, FrameworkLane, FrameworkLane]]:
    selected = []
    lane_set = set(lanes)
    for track in _TRACK_PAIRS:
        name, left, right = track
        present = {left, right} & lane_set
        if present and present != {left, right}:
            raise ValueError(f"{name} experiment requires both {left.value} and {right.value}")
        if present:
            selected.append(track)
    if not selected:
        raise ValueError("experiment has no matched framework lane pair")
    return selected


def _describe_runner(
    repository_root: Path,
    lane: FrameworkLane,
) -> NativeRunnerDescriptor:
    project, module = _RUNNER_PROJECT[lane]
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(repository_root / project),
            "--no-sync",
            "python",
            "-m",
            module,
            "--describe",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    descriptor = NativeRunnerDescriptor.model_validate_json(result.stdout)
    if lane not in descriptor.supported_lanes:
        raise ValueError(f"{descriptor.name} does not advertise lane {lane.value}")
    return descriptor


def _role_endpoints(
    credential_payload: dict[str, Any],
    *,
    run_id: str,
) -> dict:
    endpoints = {}
    for item in credential_payload["credentials"]:
        if item["run_id"] != run_id:
            continue
        agent_role = ROLE_TO_AGENT_ROLE[item["role"]]
        endpoints[agent_role] = RoleEndpoint(
            role=agent_role,
            url=item["url"],
            token=item["token"],
            expires_at=item["expires_at"],
        )
    if set(endpoints) != set(ROLE_TO_AGENT_ROLE.values()):
        raise RuntimeError(f"run {run_id} did not receive all role endpoints")
    return endpoints


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


async def _wait_until_started(
    server: uvicorn.Server,
    server_task: asyncio.Task,
) -> None:
    for _ in range(500):
        if server.started:
            return
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    raise RuntimeError("incident HTTP service did not start")


def _resolve(repository_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repository_root / path).resolve()


def _block_order_seed(order_seed: int, scenario_seed: int) -> int:
    digest = hashlib.sha256(f"{order_seed}:{scenario_seed}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _source_commit(repository_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _source_tree_sha256(repository_root: Path) -> str:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        timeout=10,
    )
    digest = hashlib.sha256()
    for raw_path in sorted(filter(None, result.stdout.split(b"\0"))):
        path = repository_root / raw_path.decode()
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"[MISSING]")
        digest.update(b"\0")
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument(
        "--experiment",
        type=Path,
        default=Path("configs/experiments/smoke.toml"),
    )
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--order-seed", type=int, default=1729)
    parser.add_argument(
        "--mode",
        choices=["protocol_probe", "native_workflow"],
        default="protocol_probe",
    )
    args = parser.parse_args()
    entrypoint = (
        run_experiment_protocol_probe
        if args.mode == "protocol_probe"
        else run_native_workflow_experiment
    )
    summary = asyncio.run(
        entrypoint(
            repository_root=args.repository_root,
            experiment_path=args.experiment,
            runs_dir=args.runs_dir,
            order_seed=args.order_seed,
        )
    )
    print(json.dumps(summary, sort_keys=True))
    if summary["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
