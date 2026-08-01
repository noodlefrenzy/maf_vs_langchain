"""Run the MAF workflow through approved recovery against an isolated world."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import tempfile
from pathlib import Path

import httpx
import uvicorn

from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import ApprovalDecision, Budget, FrameworkLane, ModelProfile, RunManifest
from agent_lab.runner_protocol import RoleEndpoint, RunPaths, RunRequest
from incident_lab_mcp.http_auth import RunCredentialSigner, RunWorldRegistry
from incident_lab_mcp.http_service import build_http_app, issue_endpoint_credentials
from incident_lab_mcp.roles import ROLE_TO_AGENT_ROLE
from incident_lab_mcp.workflow_compat_policy import (
    COMPATIBILITY_APPROVAL_POLICY,
    SEED_42_RECOVERY_ACTION,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


async def run_live_maf_workflow(base_url: str, model: str) -> dict[str, object]:
    """Exercise the native MAF workflow through approved recovery."""

    advertised_models = await _verify_model(base_url, model)
    registry = RunWorldRegistry()
    receipt = registry.create_pair(
        pair_id="maf-live-investigation",
        seed=42,
        left_lane=FrameworkLane.MAF_WORKFLOW,
        right_lane=FrameworkLane.LANGGRAPH,
        ttl_seconds=600,
        fault_profile="none",
    )
    signer = RunCredentialSigner(secret=b"m" * 32)
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
            if item["run_id"] == str(receipt.left.run_id)
        }
        with tempfile.TemporaryDirectory(prefix="agent-lab-maf-live-") as raw_run_root:
            run_root = Path(raw_run_root).resolve() / str(receipt.left.run_id)
            run_root.mkdir(mode=0o700)
            for child in ("workspace", "checkpoints", "state", "traces"):
                (run_root / child).mkdir(mode=0o700)
            hashes = contract_bundle_hashes()
            manifest = RunManifest(
                run_id=receipt.left.run_id,
                experiment_id="maf_live_investigation",
                lane=FrameworkLane.MAF_WORKFLOW,
                scenario="incident_response",
                scenario_generator_version="0.1.0",
                scenario_seed=42,
                repetition=0,
                model=ModelProfile(
                    name=model,
                    base_url=base_url,
                    temperature=0.2,
                    top_p=0.9,
                    max_output_tokens=2048,
                    context_window_tokens=65536,
                    request_timeout_seconds=120,
                    seed=1729,
                    thinking=False,
                ),
                budget=Budget(
                    wall_clock_seconds=300,
                    model_calls=24,
                    tool_calls=48,
                    output_tokens=24000,
                    approval_requests=1,
                ),
                source_commit="0" * 40,
                source_tree_sha256="0" * 64,
                framework_versions={"agent-framework-core": "live-installed"},
                dependency_lock_sha256="0" * 64,
                prompt_bundle_sha256=hashes["prompt_bundle_sha256"],
                tool_catalog_sha256=hashes["tool_catalog_sha256"],
                world_manifest_sha256=receipt.left.world_manifest_sha256,
            )
            request = RunRequest(
                manifest=manifest,
                paths=RunPaths(
                    run_root=run_root,
                    bundle=run_root,
                    workspace=run_root / "workspace",
                    checkpoints=run_root / "checkpoints",
                    state=run_root / "state",
                    traces=run_root / "traces",
                ),
                role_endpoints=role_endpoints,
                public_ticket=str(
                    registry.get(receipt.left.run_id).world.incident.public_view()["brief"]
                ),
                order_seed=1729,
            )
            payload = await _run_maf_approved_subprocess(request, registry)
            checkpoint_text = "".join(
                path.read_text(encoding="utf-8")
                for path in (run_root / "checkpoints").glob("*.json")
            )
            if "approval_token" in checkpoint_text:
                raise RuntimeError("MAF checkpoint persisted an approval token")

        world = registry.get(receipt.left.run_id).world
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
            raise RuntimeError("MAF workflow returned without verified recovery")
        if payload["model"] != model:
            raise RuntimeError("MAF live workflow reported an unexpected model")
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


async def _run_maf_approved_subprocess(
    request: RunRequest,
    registry: RunWorldRegistry,
) -> dict[str, object]:
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / "runners/maf"),
        "--no-sync",
        "python",
        "-m",
        "maf_runner.workflow_lane",
        "--live-approved-run",
        cwd=REPOSITORY_ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("MAF approved subprocess pipes were not created")
    process.stdin.write(request.model_dump_json().encode() + b"\n")
    await process.stdin.drain()
    approval_line = await asyncio.wait_for(
        process.stdout.readline(),
        timeout=request.manifest.budget.wall_clock_seconds,
    )
    if not approval_line:
        _stdout, stderr = await process.communicate()
        raise RuntimeError(
            f"MAF workflow exited before requesting approval with {process.returncode}: "
            f"{stderr.decode(errors='replace')}"
        )
    approval_event = json.loads(approval_line)
    if approval_event.get("kind") != "approval_requested":
        raise RuntimeError(f"MAF workflow returned an unexpected approval event: {approval_event}")
    request_id = str(approval_event["request"]["request_id"])
    world = registry.get(request.manifest.run_id).world
    approval = world.decide_mitigation(
        request_id,
        ApprovalDecision.EDIT,
        decided_by="maf-live-compat-reviewer",
        edited_action=SEED_42_RECOVERY_ACTION,
    )
    process.stdin.write(
        (
            json.dumps(
                {
                    "request_id": approval.request_id,
                    "decision": approval.decision.value,
                    "action_to_execute": approval.action_to_execute,
                    "approval_token": approval.approval_token,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
    )
    await process.stdin.drain()
    stdout, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=request.manifest.budget.wall_clock_seconds,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"MAF live approved workflow failed with {process.returncode}: "
            f"{stderr.decode(errors='replace')}"
        )
    lines = stdout.splitlines()
    if len(lines) != 1:
        raise RuntimeError(f"MAF workflow emitted {len(lines)} terminal payloads")
    return json.loads(lines[0])


async def _verify_model(base_url: str, model: str) -> list[str]:
    url = httpx.URL(base_url)
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("MAF live compatibility model endpoint must be loopback")
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
    raise RuntimeError("MAF live investigation HTTP service did not start")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    result = asyncio.run(run_live_maf_workflow(args.base_url, args.model))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
