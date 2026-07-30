"""Pure incident-world logic kept separate from MCP transport code."""

from __future__ import annotations

from dataclasses import dataclass

from agent_lab.scenario import IncidentCase, IncidentVariant, generate_incident


@dataclass
class IncidentWorld:
    """A small, replayable hidden world for development smoke tests."""

    incident: IncidentCase
    tool_calls: int = 0

    @classmethod
    def from_seed(cls, seed: int) -> IncidentWorld:
        return cls(incident=generate_incident(seed))

    def _before_tool(self) -> None:
        self.tool_calls += 1
        if self.tool_calls % self.incident.transient_failure_every == 0:
            raise RuntimeError("simulated transient telemetry backend timeout")

    def service_health(self, service: str) -> dict[str, float | str]:
        self._before_tool()
        if service == self.incident.affected_service:
            return {
                "service": service,
                "error_rate": 0.29,
                "p95_ms": 4800,
                "saturation": 0.91,
            }
        if service == self.incident.decoy_service:
            return {
                "service": service,
                "error_rate": 0.08,
                "p95_ms": 2100,
                "saturation": 0.74,
            }
        return {
            "service": service,
            "error_rate": 0.01,
            "p95_ms": 240,
            "saturation": 0.42,
        }

    def search_logs(self, service: str, query: str) -> dict[str, object]:
        self._before_tool()
        evidence = list(self.incident.required_evidence)
        if service == self.incident.affected_service:
            matches = evidence[:2]
        elif service == self.incident.decoy_service:
            matches = [
                f"{service}: upstream deadline exceeded after caller retries",
                f"{service}: health check remained ready",
            ]
        else:
            matches = [f"{service}: no initiating fault matched query '{query}'"]
        return {"service": service, "query": query, "matches": matches}

    def recent_changes(self, service: str | None = None) -> list[dict[str, str]]:
        self._before_tool()
        changes = [
            {
                "service": self.incident.affected_service,
                "change_id": self.incident.trigger_deployment,
                "age": "11m",
                "kind": self._trigger_kind(),
            },
            {
                "service": self.incident.decoy_service,
                "change_id": f"{self.incident.decoy_service}-docs-441",
                "age": "8m",
                "kind": "metadata-only",
            },
        ]
        if service is None:
            return changes
        return [change for change in changes if change["service"] == service]

    def config_diff(self, service: str) -> dict[str, str]:
        self._before_tool()
        if service != self.incident.affected_service:
            return {"service": service, "diff": "no runtime-affecting change"}
        return {
            "service": service,
            "diff": self.incident.required_evidence[1],
        }

    def search_runbooks(self, query: str) -> list[dict[str, str]]:
        self._before_tool()
        return [
            {
                "title": f"Mitigating {self.incident.variant.value.replace('_', ' ')}",
                "guidance": (
                    "Confirm two independent signals, prefer a reversible rollback, "
                    "and verify customer recovery before closing the incident."
                ),
            },
            {
                "title": "SEV-1 destructive action policy",
                "guidance": (
                    "Database restarts, data deletion, and topic purges require "
                    "explicit incident-commander approval."
                ),
            },
        ]

    def _trigger_kind(self) -> str:
        if self.incident.variant is IncidentVariant.FEATURE_FLAG_MISMATCH:
            return "feature-flag"
        if self.incident.variant is IncidentVariant.CACHE_TTL_REGRESSION:
            return "configuration"
        if self.incident.variant is IncidentVariant.SCHEMA_DRIFT:
            return "schema"
        return "deployment"
