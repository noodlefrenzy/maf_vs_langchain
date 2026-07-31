"""End-to-end compatibility check for authenticated Streamable HTTP MCP."""

from __future__ import annotations

import asyncio
import json
import os
import socket

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_lab.contracts import AgentRole, FrameworkLane, ToolErrorCode
from agent_lab.tool_catalog import tool_definitions_for_role
from incident_lab_mcp.http_auth import RunCredentialSigner, RunWorldRegistry
from incident_lab_mcp.http_service import (
    ExpectedRunTokenVerifier,
    build_http_app,
    issue_endpoint_credentials,
)


async def check_http_transport() -> dict[str, object]:
    registry = RunWorldRegistry()
    receipt = registry.create_pair(
        pair_id="http-compat",
        seed=42,
        left_lane=FrameworkLane.MAF_WORKFLOW,
        right_lane=FrameworkLane.LANGGRAPH,
        ttl_seconds=300,
        fault_profile="none",
    )
    signer = RunCredentialSigner(secret=b"h" * 32)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
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
        for _ in range(500):
            if server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("HTTP compatibility server did not start")

        credential_payload = issue_endpoint_credentials(
            registry,
            signer,
            port=port,
        )
        credentials = credential_payload["credentials"]
        telemetry = next(
            item
            for item in credentials
            if item["run_id"] == str(receipt.left.run_id) and item["role"] == "telemetry"
        )
        commander = next(
            item
            for item in credentials
            if item["run_id"] == str(receipt.left.run_id) and item["role"] == "commander"
        )

        async with httpx.AsyncClient(timeout=5) as client:
            health = await client.get(f"http://127.0.0.1:{port}/healthz")
            readiness = await client.get(f"http://127.0.0.1:{port}/readyz")
            assert health.json() == {"status": "ok"}
            assert readiness.json()["active_runs"] == 2
            unauthorized = await client.post(
                telemetry["url"],
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert unauthorized.status_code == 401
            wrong_role = await client.post(
                telemetry["url"],
                headers={"Authorization": f"Bearer {commander['token']}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert wrong_role.status_code == 401

        observed_tools = await _list_tools(
            telemetry["url"],
            telemetry["token"],
        )
        expected_telemetry = {
            tool.name for tool in tool_definitions_for_role(AgentRole.TELEMETRY_INVESTIGATOR)
        }
        assert observed_tools == expected_telemetry

        # A fresh session with the same token proves reconnect works.
        assert await _list_tools(telemetry["url"], telemetry["token"]) == observed_tools
        error_code = await _call_missing_trace(
            telemetry["url"],
            telemetry["token"],
        )
        assert error_code == ToolErrorCode.NOT_FOUND.value
        framework_results = await asyncio.gather(
            _framework_client_check(
                "runners/maf",
                "maf_runner.http_compat",
                telemetry["url"],
                telemetry["token"],
            ),
            _framework_client_check(
                "runners/langgraph",
                "langgraph_runner.http_compat",
                telemetry["url"],
                telemetry["token"],
            ),
        )
        assert {result["client"] for result in framework_results} == {
            "maf",
            "langgraph",
        }
        assert all(result["structured_error"] == "not_found" for result in framework_results)
        assert all(result["cancellation"] is True for result in framework_results)
        assert all(result["reconnect"] is True for result in framework_results)

        left_run = registry.get(receipt.left.run_id)
        verifier = ExpectedRunTokenVerifier(
            registry=registry,
            signer=signer,
            run=left_run,
            role="telemetry",
            resource_url=telemetry["url"].removesuffix("/mcp"),
        )
        registry.deactivate(receipt.left.run_id)
        assert await verifier.verify_token(telemetry["token"]) is None
        async with httpx.AsyncClient(timeout=5) as client:
            revoked = await client.post(
                telemetry["url"],
                headers={"Authorization": f"Bearer {telemetry['token']}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert revoked.status_code == 401

        return {
            "status": "ok",
            "active_runs_before_revoke": 2,
            "telemetry_tools": sorted(observed_tools),
            "structured_error": error_code,
            "framework_clients": sorted(result["client"] for result in framework_results),
            "framework_cancellation": True,
            "framework_structured_errors": True,
            "reconnect": True,
            "revocation": True,
        }
    finally:
        server.should_exit = True
        await server_task
        listener.close()


async def _list_tools(url: str, token: str) -> set[str]:
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        ) as client,
        streamable_http_client(url, http_client=client) as (
            read_stream,
            write_stream,
            _session_id,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        return {tool.name for tool in result.tools}


async def _call_missing_trace(url: str, token: str) -> str:
    async with (
        httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        ) as client,
        streamable_http_client(url, http_client=client) as (
            read_stream,
            write_stream,
            _session_id,
        ),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "lookup_trace",
            {"trace_id": "missing-trace"},
        )
        assert result.isError is True
        text = result.content[0].text  # type: ignore[union-attr]
        payload = json.loads(text[text.index("{") :])
        return payload["code"]


async def _framework_client_check(
    project: str,
    module: str,
    url: str,
    token: str,
) -> dict[str, object]:
    environment = {
        **os.environ,
        "INCIDENT_LAB_MCP_URL": url,
        "INCIDENT_LAB_MCP_TOKEN": token,
    }
    environment.pop("VIRTUAL_ENV", None)
    process = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "--project",
        project,
        "python",
        "-m",
        module,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    if process.returncode != 0:
        raise RuntimeError(
            f"{module} failed with {process.returncode}: {stderr.decode(errors='replace')}"
        )
    return json.loads(stdout)


def main() -> None:
    result = asyncio.run(check_http_transport())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
