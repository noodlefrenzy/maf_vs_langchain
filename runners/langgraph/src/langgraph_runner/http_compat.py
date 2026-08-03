"""Verify the LangChain MCP adapter against one authenticated HTTP catalog."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_lab.contracts import AgentRole
from agent_lab.tool_catalog import tool_definitions_for_role


async def check(url: str, token: str) -> dict[str, object]:
    expected = {tool.name for tool in tool_definitions_for_role(AgentRole.TELEMETRY_INVESTIGATOR)}
    client = MultiServerMCPClient(
        {
            "incident": {
                "transport": "streamable_http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    )
    tools = await client.get_tools(server_name="incident")
    observed = {tool.name for tool in tools}
    if observed != expected:
        raise RuntimeError(
            f"LangChain catalog mismatch: expected {sorted(expected)}, observed {sorted(observed)}"
        )
    status_tool = next(tool for tool in tools if tool.name == "get_incident_status")
    status = await status_tool.ainvoke({})
    trace_tool = next(tool for tool in tools if tool.name == "lookup_trace")
    missing_trace = await trace_tool.ainvoke({"trace_id": "missing-trace"})
    if "not_found" not in str(missing_trace):
        raise RuntimeError(f"LangGraph lost the structured tool error: {missing_trace}")

    cancelled_call = asyncio.create_task(status_tool.ainvoke({}))
    await asyncio.sleep(0)
    cancelled_call.cancel()
    try:
        await cancelled_call
    except asyncio.CancelledError:
        pass
    else:
        raise RuntimeError("LangGraph tool call completed before cancellation")
    status_after_cancel = await status_tool.ainvoke({})

    reconnected_tools = await client.get_tools(server_name="incident")
    reconnect_status_tool = next(
        tool for tool in reconnected_tools if tool.name == "get_incident_status"
    )
    reconnect_status = await reconnect_status_tool.ainvoke({})
    return {
        "status": "ok",
        "client": "langgraph",
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
