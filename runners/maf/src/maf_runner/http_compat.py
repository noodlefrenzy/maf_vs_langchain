"""Verify the MAF native MCP client against one authenticated HTTP catalog."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework.exceptions import ToolExecutionException

from agent_lab.contracts import AgentRole
from agent_lab.tool_catalog import tool_definitions_for_role


async def check(url: str, token: str) -> dict[str, object]:
    expected = {tool.name for tool in tool_definitions_for_role(AgentRole.TELEMETRY_INVESTIGATOR)}
    # MAF's header_provider is evaluated for tool calls, after MCP initialization.
    # Initialization itself therefore needs an authenticated HTTP client.
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ) as http_client:
        async with MCPStreamableHTTPTool(
            name="incident-telemetry",
            url=url,
            http_client=http_client,
            load_prompts=False,
        ) as mcp_tool:
            observed = {function.name for function in mcp_tool.functions}
            if observed != expected:
                raise RuntimeError(
                    f"MAF catalog mismatch: expected {sorted(expected)}, "
                    f"observed {sorted(observed)}"
                )
            status = await mcp_tool.call_tool("get_incident_status")
            try:
                await mcp_tool.call_tool("lookup_trace", trace_id="missing-trace")
            except ToolExecutionException as exc:
                if "not_found" not in str(exc):
                    raise RuntimeError(f"MAF lost the structured tool error: {exc}") from exc
            else:
                raise RuntimeError("MAF did not surface the missing-trace tool error")

            cancelled_call = asyncio.create_task(mcp_tool.call_tool("get_incident_status"))
            await asyncio.sleep(0)
            cancelled_call.cancel()
            try:
                await cancelled_call
            except asyncio.CancelledError:
                pass
            else:
                raise RuntimeError("MAF tool call completed before cancellation")
            status_after_cancel = await mcp_tool.call_tool("get_incident_status")

        async with MCPStreamableHTTPTool(
            name="incident-telemetry-reconnect",
            url=url,
            http_client=http_client,
            load_prompts=False,
        ) as reconnected_tool:
            reconnect_status = await reconnected_tool.call_tool("get_incident_status")

    return {
        "status": "ok",
        "client": "maf",
        "tools": sorted(observed),
        "status_result_present": bool(status and status_after_cancel),
        "structured_error": "not_found",
        "cancellation": True,
        "reconnect": bool(reconnect_status),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("INCIDENT_LAB_MCP_URL"))
    args = parser.parse_args()
    token = os.getenv("INCIDENT_LAB_MCP_TOKEN")
    if not args.url or not token:
        parser.error("URL is required via argument/environment and token via environment")
    print(json.dumps(asyncio.run(check(args.url, token)), sort_keys=True))


if __name__ == "__main__":
    main()
