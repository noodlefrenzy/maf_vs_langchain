"""Run the paired single-agent adapters as a non-measurement live compatibility probe."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import socket
import tempfile
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from agent_lab.async_cleanup import shutdown_server
from agent_lab.bundle_validation import validate_bundle, validate_pair
from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import (
    AgentRole,
    ApprovalDecision,
    Budget,
    FrameworkLane,
    ModelProfile,
    RunManifest,
    TerminalStatus,
)
from agent_lab.control_plane import ControlPlane
from agent_lab.model_request_policy import effective_request_policy
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from incident_lab_mcp.http_auth import RunCredentialSigner, RunWorldRegistry
from incident_lab_mcp.http_service import build_http_app, issue_endpoint_credentials
from incident_lab_mcp.roles import ROLE_TO_AGENT_ROLE
from incident_lab_mcp.workflow_compat_policy import (
    COMPATIBILITY_APPROVAL_POLICY,
    SEED_42_RECOVERY_ACTION,
)
from incident_lab_mcp.workflow_finalization import finalize_workflow_bundle

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_LANES = (FrameworkLane.MAF_SINGLE_AGENT, FrameworkLane.LANGCHAIN_SINGLE_AGENT)
_RUNNERS = {
    FrameworkLane.MAF_SINGLE_AGENT: ("runners/maf", "maf_runner.protocol_runner"),
    FrameworkLane.LANGCHAIN_SINGLE_AGENT: (
        "runners/langgraph",
        "langgraph_runner.protocol_runner",
    ),
}


async def run_live_single_agent_pair(base_url: str, model: str) -> dict[str, Any]:
    """Exercise both primitive-agent baselines without producing measured evidence."""

    advertised_models = await _verify_model(base_url, model)
    registry = RunWorldRegistry()
    receipt = registry.create_pair(
        pair_id="single-agent-live-compat",
        seed=42,
        left_lane=_LANES[0],
        right_lane=_LANES[1],
        ttl_seconds=1200,
        fault_profile="none",
    )
    receipts = dict(zip(_LANES, (receipt.left, receipt.right), strict=True))
    # Fixed non-production key: this hermetic probe needs reproducibility, not secrecy.
    signer = RunCredentialSigner(secret=b"s" * 32)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            build_http_app(registry, signer, port=port),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _wait_until_started(server, server_task)
        credentials = issue_endpoint_credentials(registry, signer, port=port)
        with tempfile.TemporaryDirectory(prefix="agent-lab-single-agent-live-") as raw_root:
            root = Path(raw_root)
            outputs = []
            bundles = []
            for lane in _LANES:
                lane_receipt = receipts[lane]
                endpoints = _role_endpoints(
                    credentials,
                    run_id=str(lane_receipt.run_id),
                )
                profile = ModelProfile(
                    name=model,
                    base_url=base_url,
                    temperature=0.0,
                    top_p=1.0,
                    max_output_tokens=4096,
                    context_window_tokens=65536,
                    request_timeout_seconds=180,
                    seed=1729,
                    thinking=False,
                )
                hashes = contract_bundle_hashes()
                project, module = _RUNNERS[lane]
                manifest = RunManifest(
                    run_id=lane_receipt.run_id,
                    experiment_id="single_agent_live_compat",
                    lane=lane,
                    scenario="incident_response",
                    scenario_generator_version="0.1.0",
                    scenario_seed=42,
                    repetition=0,
                    model=profile,
                    budget=Budget(
                        wall_clock_seconds=600,
                        model_calls=32,
                        tool_calls=64,
                        output_tokens=32000,
                        approval_requests=1,
                    ),
                    source_commit="0" * 40,
                    source_tree_sha256="0" * 64,
                    framework_versions={lane.value: "live-installed"},
                    dependency_lock_sha256=_file_sha256(REPOSITORY_ROOT / project / "uv.lock"),
                    prompt_bundle_sha256=hashes["prompt_bundle_sha256"],
                    tool_catalog_sha256=hashes["tool_catalog_sha256"],
                    effective_request_policy_sha256=effective_request_policy(profile, lane)[
                        "policy_sha256"
                    ],
                    execution_provenance_sha256="0" * 64,
                    world_manifest_sha256=lane_receipt.world_manifest_sha256,
                    host_fingerprint={"execution_mode": "non_measurement_live_compat"},
                )
                control_plane = ControlPlane(root, graceful_shutdown_seconds=2)
                _bundle, paths = control_plane.allocate_run(manifest)
                request = RunRequest(
                    manifest=manifest,
                    paths=paths,
                    role_endpoints=endpoints,
                    public_ticket=str(
                        registry.get(lane_receipt.run_id).world.incident.public_view()["brief"]
                    ),
                    order_seed=1741,
                )
                output = await _run_lane(
                    control_plane,
                    request,
                    registry,
                    project=project,
                    module=module,
                )
                outputs.append(output)
                bundles.append(paths.bundle)
            pair = validate_pair(bundles[0], bundles[1])
            if not pair.valid or pair.track != "single_agent_baseline":
                raise RuntimeError(
                    "single-agent compatibility pair failed validation: "
                    f"{[finding.code for finding in pair.findings]}"
                )
            return {
                "status": "ok",
                "measurement_eligible": False,
                "compatibility_approval_policy": COMPATIBILITY_APPROVAL_POLICY,
                "compatibility_sampling": {
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "seed": 1729,
                },
                "advertised_models": advertised_models,
                "pair_valid": True,
                "track": pair.track,
                "lanes": outputs,
            }
    finally:
        await shutdown_server(server, server_task, listener)


async def _run_lane(
    control_plane: ControlPlane,
    request: RunRequest,
    registry: RunWorldRegistry,
    *,
    project: str,
    module: str,
) -> dict[str, Any]:
    world = registry.get(request.manifest.run_id).world

    async def approve(event):
        return world.decide_mitigation(
            str(event.payload["request_id"]),
            ApprovalDecision.EDIT,
            decided_by="single-agent-live-compat-reviewer",
            edited_action_id=world.incident.correct_mitigation_id,
            edited_action=SEED_42_RECOVERY_ACTION,
        )

    async def complete(_event):
        return await finalize_workflow_bundle(request, world)

    command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / project),
        "--no-sync",
        "python",
        "-m",
        module,
        "--run",
    ]
    result = await control_plane.execute(
        command,
        request,
        approval_handler=approve,
        completion_handler=complete,
    )
    if result.terminal.status is not TerminalStatus.COMPLETED:
        raise RuntimeError(
            f"{request.manifest.lane.value} failed with {result.terminal.status.value}: "
            f"{result.terminal.reason}; stderr={result.stderr_path.read_text()!r}"
        )
    validation = validate_bundle(request.paths.bundle)
    if not validation.valid:
        raise RuntimeError(
            f"{request.manifest.lane.value} bundle failed validation: "
            f"{[finding.code for finding in validation.findings]}"
        )
    payload = json.loads(
        (request.paths.state / "workflow-execution.json").read_text(encoding="utf-8")
    )
    execution = payload["execution"]
    if execution["status"] != "completed" or not execution["recovery"]["recovered"]:
        raise RuntimeError(f"{request.manifest.lane.value} did not recover in live compatibility")
    protocol_usage = {
        "model_calls": result.terminal.used_model_calls,
        "tool_calls": result.terminal.used_tool_calls,
        "output_tokens": result.terminal.used_output_tokens,
    }
    role_usage = payload["role_stats"][AgentRole.INCIDENT_COMMANDER.value]
    for field in ("model_calls", "tool_calls", "output_tokens"):
        if role_usage[field] != protocol_usage[field]:
            raise RuntimeError(
                f"{request.manifest.lane.value} {field} accounting mismatch: "
                f"role={role_usage[field]}, protocol={protocol_usage[field]}"
            )
    return {
        "lane": request.manifest.lane.value,
        "framework": payload["framework"],
        "model": payload["model"],
        "execution": execution,
        "role_stats": payload["role_stats"],
        "source_tool_calls": payload["source_tool_calls"],
        "provenance_correction_attempts": payload["provenance_correction_attempts"],
        "discovery_repair_attempts": payload["discovery_repair_attempts"],
        "protocol_usage": protocol_usage,
        "bundle_valid": True,
    }


def _role_endpoints(credentials: dict[str, Any], *, run_id: str) -> dict:
    return {
        ROLE_TO_AGENT_ROLE[item["role"]]: RoleEndpoint(
            role=ROLE_TO_AGENT_ROLE[item["role"]],
            url=item["url"],
            token=item["token"],
            expires_at=item["expires_at"],
        )
        for item in credentials["credentials"]
        if item["run_id"] == run_id
    }


async def _verify_model(base_url: str, model: str) -> list[str]:
    url = httpx.URL(base_url)
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("single-agent compatibility model endpoint must be loopback")
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        response = await client.get(f"{str(url).rstrip('/')}/models")
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("model endpoint returned an invalid OpenAI model inventory")
    models = sorted(
        {
            item["id"]
            for item in payload["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )
    if model not in models:
        raise RuntimeError(f"requested model {model!r} is not advertised: {models}")
    return models


async def _wait_until_started(server: uvicorn.Server, task: asyncio.Task) -> None:
    for _ in range(500):
        if server.started:
            return
        if task.done():
            await task
        await asyncio.sleep(0.01)
    raise RuntimeError("incident compatibility service did not start")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run_live_single_agent_pair(args.base_url, args.model)),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
