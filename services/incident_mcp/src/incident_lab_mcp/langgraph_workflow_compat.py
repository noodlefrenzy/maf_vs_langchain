"""Run the LangGraph workflow through approved recovery against an isolated world."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import tempfile
from pathlib import Path

import httpx
import uvicorn

from agent_lab.bundle_validation import validate_bundle
from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import (
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


async def run_live_langgraph_workflow(base_url: str, model: str) -> dict[str, object]:
    """Exercise the native LangGraph workflow through approved recovery."""

    advertised_models = await _verify_model(base_url, model)
    registry = RunWorldRegistry()
    receipt = registry.create_pair(
        pair_id="langgraph-live-investigation",
        seed=42,
        left_lane=FrameworkLane.MAF_WORKFLOW,
        right_lane=FrameworkLane.LANGGRAPH,
        ttl_seconds=600,
        fault_profile="none",
    )
    # Fixed non-production key: this hermetic probe needs reproducibility, not secrecy.
    signer = RunCredentialSigner(secret=b"l" * 32)
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
        role_endpoints = {
            ROLE_TO_AGENT_ROLE[item["role"]]: RoleEndpoint(
                role=ROLE_TO_AGENT_ROLE[item["role"]],
                url=item["url"],
                token=item["token"],
                expires_at=item["expires_at"],
            )
            for item in credentials["credentials"]
            if item["run_id"] == str(receipt.right.run_id)
        }
        with tempfile.TemporaryDirectory(prefix="agent-lab-langgraph-live-") as raw_run_root:
            hashes = contract_bundle_hashes()
            profile = ModelProfile(
                name=model,
                base_url=base_url,
                temperature=0.2,
                top_p=0.9,
                max_output_tokens=2048,
                context_window_tokens=65536,
                request_timeout_seconds=120,
                seed=1729,
                thinking=False,
            )
            manifest = RunManifest(
                run_id=receipt.right.run_id,
                experiment_id="langgraph_live_investigation",
                lane=FrameworkLane.LANGGRAPH,
                scenario="incident_response",
                scenario_generator_version="0.1.0",
                scenario_seed=42,
                repetition=0,
                model=profile,
                budget=Budget(
                    wall_clock_seconds=300,
                    model_calls=24,
                    tool_calls=48,
                    output_tokens=24000,
                    approval_requests=1,
                ),
                source_commit="0" * 40,
                source_tree_sha256="0" * 64,
                framework_versions={"langgraph": "live-installed"},
                dependency_lock_sha256="0" * 64,
                prompt_bundle_sha256=hashes["prompt_bundle_sha256"],
                tool_catalog_sha256=hashes["tool_catalog_sha256"],
                effective_request_policy_sha256=effective_request_policy(
                    profile,
                    FrameworkLane.LANGGRAPH,
                )["policy_sha256"],
                execution_provenance_sha256="0" * 64,
                world_manifest_sha256=receipt.right.world_manifest_sha256,
            )
            control_plane = ControlPlane(Path(raw_run_root), graceful_shutdown_seconds=2)
            _bundle, paths = control_plane.allocate_run(manifest)
            request = RunRequest(
                manifest=manifest,
                paths=paths,
                role_endpoints=role_endpoints,
                public_ticket=str(
                    registry.get(receipt.right.run_id).world.incident.public_view()["brief"]
                ),
                order_seed=1729,
            )
            payload = await _run_langgraph_protocol(control_plane, request, registry)
            checkpoint_path = paths.checkpoints / "workflow.json"
            checkpoint_text = checkpoint_path.read_text(encoding="utf-8")
            if "approval_token" in checkpoint_text:
                raise RuntimeError("LangGraph checkpoint persisted an approval token")

        world = registry.get(receipt.right.run_id).world
        operations = [event.operation for event in world.audit_log]
        execution = payload["execution"]
        if world.applied_request_id is None or not world.recovery_verified:
            raise RuntimeError(
                "approved workflow did not apply an effective mitigation and recover: "
                f"action={execution['action_to_execute']!r}, "
                f"effective={execution['mitigation']['effective']!r}, "
                f"recovered={execution['recovery']['recovered']!r}"
            )
        if execution["status"] != "completed" or not execution["recovery"]["recovered"]:
            raise RuntimeError("LangGraph workflow returned without verified recovery")
        if payload["model"] != model:
            raise RuntimeError("LangGraph live workflow reported an unexpected model")
        return {
            **payload,
            "status": "ok",
            "advertised_models": advertised_models,
            "world_operations": operations,
            "world_state_changed": True,
            "checkpoint_contains_approval_token": False,
            "compatibility_approval_policy": COMPATIBILITY_APPROVAL_POLICY,
        }
    finally:
        server.should_exit = True
        await server_task
        listener.close()


async def _run_langgraph_protocol(
    control_plane: ControlPlane,
    request: RunRequest,
    registry: RunWorldRegistry,
) -> dict[str, object]:
    world = registry.get(request.manifest.run_id).world

    async def approve(event):
        return world.decide_mitigation(
            str(event.payload["request_id"]),
            ApprovalDecision.EDIT,
            decided_by="langgraph-live-compat-reviewer",
            edited_action_id=world.incident.correct_mitigation_id,
            edited_action=SEED_42_RECOVERY_ACTION,
        )

    async def complete(_event):
        return await finalize_workflow_bundle(request, world)

    command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / "runners/langgraph"),
        "--no-sync",
        "python",
        "-m",
        "langgraph_runner.protocol_runner",
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
            f"LangGraph protocol workflow failed with {result.terminal.status.value}: "
            f"{result.terminal.reason}; stderr={result.stderr_path.read_text()!r}"
        )
    payload = json.loads((request.paths.state / "workflow-execution.json").read_text())
    events = [
        json.loads(line)
        for line in (request.paths.bundle / "events.jsonl").read_text().splitlines()
    ]
    validation = validate_bundle(request.paths.bundle)
    if not validation.valid:
        raise RuntimeError(
            "LangGraph finalized bundle failed validation: "
            f"{[finding.code for finding in validation.findings]}"
        )
    scores = json.loads((request.paths.bundle / "scores.json").read_text())
    return {
        **payload,
        "protocol_terminal_status": result.terminal.status.value,
        "protocol_usage": {
            "model_calls": result.terminal.used_model_calls,
            "tool_calls": result.terminal.used_tool_calls,
            "output_tokens": result.terminal.used_output_tokens,
        },
        "normalized_event_kinds": [event["kind"] for event in events],
        "bundle_valid": True,
        "task_success": scores["task_success"],
        "total_score": scores["total_score"],
    }


async def _verify_model(base_url: str, model: str) -> list[str]:
    url = httpx.URL(base_url)
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("LangGraph live compatibility model endpoint must be loopback")
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        response = await client.get(f"{str(url).rstrip('/')}/models")
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("model endpoint returned an invalid OpenAI model inventory")
    model_ids = sorted(
        {
            item["id"]
            for item in payload["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )
    if model not in model_ids:
        raise RuntimeError(f"requested model {model!r} is not advertised by endpoint: {model_ids}")
    return model_ids


async def _wait_until_started(server: uvicorn.Server, server_task: asyncio.Task) -> None:
    for _ in range(500):
        if server.started:
            return
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    raise RuntimeError("LangGraph live investigation HTTP service did not start")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    result = asyncio.run(run_live_langgraph_workflow(args.base_url, args.model))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
