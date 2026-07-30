"""Verify role tool catalogs without starting an MCP transport."""

from __future__ import annotations

import asyncio
import json
from typing import cast

from incident_lab_mcp.server import Role, build_server

EXPECTED_CATALOGS = {
    "telemetry": {
        "get_incident_brief",
        "query_service_health",
        "search_logs",
    },
    "change": {
        "get_incident_brief",
        "list_recent_changes",
        "get_config_diff",
    },
    "runbook": {
        "get_incident_brief",
        "search_runbooks",
    },
    "commander": {
        "get_incident_brief",
        "submit_hypothesis",
        "request_mitigation",
    },
}


async def get_catalog(role: Role) -> set[str]:
    server = build_server(role, seed=42)
    return {tool.name for tool in await server.list_tools()}


async def check_catalogs() -> dict[str, list[str]]:
    observed: dict[str, list[str]] = {}
    for raw_role, expected in EXPECTED_CATALOGS.items():
        role = cast(Role, raw_role)
        catalog = await get_catalog(role)
        if catalog != expected:
            raise RuntimeError(
                f"catalog mismatch for {role}: expected {sorted(expected)}, "
                f"observed {sorted(catalog)}"
            )
        observed[role] = sorted(catalog)
    return observed


def main() -> None:
    observed = asyncio.run(check_catalogs())
    print(json.dumps({"status": "ok", "catalogs": observed}, sort_keys=True))


if __name__ == "__main__":
    main()
