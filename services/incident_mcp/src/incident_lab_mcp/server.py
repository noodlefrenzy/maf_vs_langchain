"""Role-scoped FastMCP facade for the seeded incident world."""

from __future__ import annotations

import os
from typing import Literal, cast

from mcp.server.fastmcp import FastMCP

from incident_lab_mcp.world import IncidentWorld

Role = Literal["telemetry", "change", "runbook", "commander"]
_ROLES = {"telemetry", "change", "runbook", "commander"}


def build_server(role: Role, seed: int) -> FastMCP:
    """Build one role-specific tool catalog."""

    world = IncidentWorld.from_seed(seed)
    server = FastMCP(
        name=f"incident-lab-{role}",
        instructions=(
            "This server exposes a role-scoped view of a simulated incident. "
            "Tool failures may be transient. Do not infer that the most visibly "
            "degraded service initiated the incident."
        ),
        json_response=True,
    )

    @server.tool()
    def get_incident_brief() -> dict[str, str | int]:
        """Return the public incident ticket and case identifier."""

        return world.incident.public_view()

    if role == "telemetry":

        @server.tool()
        def query_service_health(service: str) -> dict[str, float | str]:
            """Query current error rate, latency, and saturation for one service."""

            return world.service_health(service)

        @server.tool()
        def search_logs(service: str, query: str) -> dict[str, object]:
            """Search bounded logs for one service using a focused query."""

            return world.search_logs(service, query)

    if role == "change":

        @server.tool()
        def list_recent_changes(service: str | None = None) -> list[dict[str, str]]:
            """List recent deployments, schemas, flags, and configuration changes."""

            return world.recent_changes(service)

        @server.tool()
        def get_config_diff(service: str) -> dict[str, str]:
            """Return the runtime-relevant configuration diff for one service."""

            return world.config_diff(service)

    if role == "runbook":

        @server.tool()
        def search_runbooks(query: str) -> list[dict[str, str]]:
            """Find incident policies and reversible mitigation guidance."""

            return world.search_runbooks(query)

    if role == "commander":

        @server.tool()
        def submit_hypothesis(
            root_cause: str,
            evidence: list[str],
            mitigation: str,
        ) -> dict[str, str | int]:
            """Submit the final diagnosis; scoring is deliberately not returned."""

            return {
                "case_id": world.incident.case_id,
                "status": "recorded_for_evaluation",
                "evidence_items": len(evidence),
                "root_cause": root_cause,
                "mitigation": mitigation,
            }

        @server.tool()
        def request_mitigation(mitigation: str) -> dict[str, str]:
            """Create an approval-gated mitigation request; this does not execute it."""

            return {
                "case_id": world.incident.case_id,
                "status": "approval_required",
                "requested_mitigation": mitigation,
            }

    return server


def main() -> None:
    role_value = os.getenv("INCIDENT_LAB_ROLE", "telemetry")
    if role_value not in _ROLES:
        raise SystemExit(f"INCIDENT_LAB_ROLE must be one of {sorted(_ROLES)}, got {role_value!r}")
    seed = int(os.getenv("INCIDENT_LAB_SEED", "42"))
    transport = os.getenv("INCIDENT_LAB_TRANSPORT", "stdio")
    server = build_server(cast(Role, role_value), seed)
    server.run(transport=transport)


if __name__ == "__main__":
    main()
