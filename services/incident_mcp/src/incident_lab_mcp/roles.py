"""Role identifiers shared by MCP transports without importing FastMCP."""

from __future__ import annotations

from typing import Literal

from agent_lab.contracts import AgentRole

Role = Literal["telemetry", "change", "runbook", "commander"]
ROLES: tuple[Role, ...] = ("telemetry", "change", "runbook", "commander")
ROLE_TO_AGENT_ROLE: dict[Role, AgentRole] = {
    "telemetry": AgentRole.TELEMETRY_INVESTIGATOR,
    "change": AgentRole.CHANGE_INVESTIGATOR,
    "runbook": AgentRole.RELIABILITY_REVIEWER,
    "commander": AgentRole.INCIDENT_COMMANDER,
}
