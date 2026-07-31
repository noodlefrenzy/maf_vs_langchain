"""Verify role tool catalogs without starting an MCP transport."""

from __future__ import annotations

import asyncio
import json

from agent_lab.tool_catalog import (
    get_tool_definition,
    tool_definitions_for_role,
)
from incident_lab_mcp.server import ROLE_TO_AGENT_ROLE, Role, build_server

EXPECTED_CATALOGS: dict[Role, set[str]] = {
    role: {tool.name for tool in tool_definitions_for_role(agent_role)}
    for role, agent_role in ROLE_TO_AGENT_ROLE.items()
}


async def get_catalog(role: Role) -> set[str]:
    server = build_server(role, seed=42)
    return {tool.name for tool in await server.list_tools()}


async def check_catalogs() -> dict[str, list[str]]:
    observed: dict[str, list[str]] = {}
    for role, expected in EXPECTED_CATALOGS.items():
        catalog = await get_catalog(role)
        if catalog != expected:
            raise RuntimeError(
                f"catalog mismatch for {role}: expected {sorted(expected)}, "
                f"observed {sorted(catalog)}"
            )
        server = build_server(role, seed=42, fault_profile="none")
        for observed_tool in await server.list_tools():
            definition = get_tool_definition(observed_tool.name)
            if observed_tool.description != definition.description:
                raise RuntimeError(f"description mismatch for {observed_tool.name}")
            expected_input = definition.input_model.model_json_schema()
            observed_input = observed_tool.inputSchema
            if observed_input != expected_input:
                raise RuntimeError(f"input schema mismatch for {observed_tool.name}")
            expected_output = definition.output_model.model_json_schema()
            observed_output = observed_tool.outputSchema
            if observed_output != expected_output:
                raise RuntimeError(f"output schema mismatch for {observed_tool.name}")
        observed[role] = sorted(catalog)
    return observed


def main() -> None:
    observed = asyncio.run(check_catalogs())
    print(json.dumps({"status": "ok", "catalogs": observed}, sort_keys=True))


if __name__ == "__main__":
    main()
