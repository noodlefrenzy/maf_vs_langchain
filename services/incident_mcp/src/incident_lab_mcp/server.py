"""Role-scoped FastMCP facade for the seeded incident world."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, cast

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from agent_lab.scenario import MitigationActionId
from agent_lab.tool_catalog import (
    ApprovalRequest,
    ChangeDetailResult,
    DiffResult,
    HypothesisResult,
    IncidentBriefResult,
    IncidentStatusResult,
    LogSearchResult,
    MetricQueryResult,
    MitigationResult,
    RecentChangesResult,
    RecoveryCheckResult,
    RunbookResult,
    RunbookSearchResult,
    ServiceHealthResult,
    TraceLookupResult,
    get_tool_definition,
)
from incident_lab_mcp.roles import ROLES, Role
from incident_lab_mcp.world import FaultProfile, IncidentWorld


class ContractFastMCP(FastMCP):
    """FastMCP variant whose advertised/validated inputs use the frozen schemas."""

    def add_tool(
        self,
        fn: Any,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().add_tool(fn, name=name, **kwargs)
        tool_name = name or fn.__name__
        definition = get_tool_definition(tool_name)
        registered = self._tool_manager._tools[tool_name]
        registered.fn_metadata.arg_model.model_config["extra"] = "forbid"
        registered.fn_metadata.arg_model.model_rebuild(force=True)
        registered.parameters = definition.input_model.model_json_schema()


def _description(tool_name: str) -> str:
    return get_tool_definition(tool_name).description


def build_server(
    role: Role,
    seed: int,
    *,
    fault_profile: FaultProfile = "standard",
    audit_path: Path | None = None,
    world: IncidentWorld | None = None,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    """Build one role-specific tool catalog over an isolated or supplied world."""

    world = world or IncidentWorld.from_seed(
        seed,
        fault_profile=fault_profile,
        audit_path=audit_path,
    )
    server = ContractFastMCP(
        name=f"incident-lab-{role}",
        instructions=(
            "This server exposes a role-scoped view of a simulated incident. "
            "Tool failures may be transient and are never silently retried. "
            "Do not infer that the most visibly degraded service initiated the incident."
        ),
        json_response=True,
        token_verifier=token_verifier,
        auth=auth,
        transport_security=transport_security,
    )

    @server.tool(description=_description("get_incident_brief"))
    def get_incident_brief() -> IncidentBriefResult:
        return IncidentBriefResult.model_validate(world.get_incident_brief())

    @server.tool(description=_description("get_incident_status"))
    def get_incident_status() -> IncidentStatusResult:
        return IncidentStatusResult.model_validate(world.get_incident_status())

    if role == "telemetry":

        @server.tool(description=_description("query_service_health"))
        def query_service_health(service: str) -> ServiceHealthResult:
            return ServiceHealthResult.model_validate(world.query_service_health(service))

        @server.tool(description=_description("search_logs"))
        def search_logs(
            service: str,
            query: str,
            limit: Annotated[int, Field(ge=1, le=100)] = 20,
        ) -> LogSearchResult:
            return LogSearchResult.model_validate(world.search_logs(service, query, limit))

        @server.tool(description=_description("query_metric"))
        def query_metric(
            service: str,
            metric: str,
            start_tick: Annotated[int, Field(ge=0)] = 0,
            end_tick: Annotated[int | None, Field(ge=0)] = None,
        ) -> MetricQueryResult:
            return MetricQueryResult.model_validate(
                world.query_metric(service, metric, start_tick, end_tick)
            )

        @server.tool(description=_description("lookup_trace"))
        def lookup_trace(trace_id: str) -> TraceLookupResult:
            return TraceLookupResult.model_validate(world.lookup_trace(trace_id))

    if role == "change":

        @server.tool(description=_description("list_recent_changes"))
        def list_recent_changes(
            service: str | None = None,
            since_tick: Annotated[int, Field(ge=0)] = 0,
        ) -> RecentChangesResult:
            return RecentChangesResult.model_validate(
                world.list_recent_changes(service, since_tick)
            )

        @server.tool(description=_description("get_change_detail"))
        def get_change_detail(change_id: str) -> ChangeDetailResult:
            return ChangeDetailResult.model_validate(world.get_change_detail(change_id))

        @server.tool(description=_description("get_config_diff"))
        def get_config_diff(
            service: str,
            change_id: str | None = None,
        ) -> DiffResult:
            return DiffResult.model_validate(world.get_config_diff(service, change_id))

        @server.tool(description=_description("get_feature_flag_diff"))
        def get_feature_flag_diff(
            flag: str,
            regions: Annotated[list[str], Field(min_length=2, max_length=2)],
        ) -> DiffResult:
            return DiffResult.model_validate(world.get_feature_flag_diff(flag, regions))

        @server.tool(description=_description("get_schema_diff"))
        def get_schema_diff(subject: str) -> DiffResult:
            return DiffResult.model_validate(world.get_schema_diff(subject))

    if role == "runbook":

        @server.tool(description=_description("search_runbooks"))
        def search_runbooks(
            query: str,
            limit: Annotated[int, Field(ge=1, le=25)] = 10,
        ) -> RunbookSearchResult:
            return RunbookSearchResult.model_validate(world.search_runbooks(query, limit))

        @server.tool(description=_description("get_runbook"))
        def get_runbook(runbook_id: str) -> RunbookResult:
            return RunbookResult.model_validate(world.get_runbook(runbook_id))

    if role == "commander":

        @server.tool(description=_description("submit_hypothesis"))
        def submit_hypothesis(
            root_cause: str,
            evidence_ids: Annotated[list[str], Field(min_length=2)],
            mitigation: str,
        ) -> HypothesisResult:
            return HypothesisResult.model_validate(
                world.submit_hypothesis(root_cause, evidence_ids, mitigation)
            )

        @server.tool(description=_description("request_mitigation"))
        def request_mitigation(
            proposed_action_id: MitigationActionId,
            proposed_action: str,
            rationale: str,
            evidence_ids: Annotated[list[str], Field(min_length=2)],
        ) -> ApprovalRequest:
            return ApprovalRequest.model_validate(
                world.request_mitigation(
                    proposed_action_id,
                    proposed_action,
                    rationale,
                    evidence_ids,
                )
            )

        @server.tool(description=_description("apply_mitigation"))
        def apply_mitigation(
            request_id: str,
            approval_token: str,
        ) -> MitigationResult:
            return MitigationResult.model_validate(
                world.apply_mitigation(request_id, approval_token)
            )

        @server.tool(description=_description("verify_recovery"))
        def verify_recovery(
            services: Annotated[list[str], Field(min_length=1, max_length=10)],
        ) -> RecoveryCheckResult:
            return RecoveryCheckResult.model_validate(world.verify_recovery(services))

    return server


def main() -> None:
    role_value = os.getenv("INCIDENT_LAB_ROLE", "telemetry")
    if role_value not in ROLES:
        raise SystemExit(f"INCIDENT_LAB_ROLE must be one of {sorted(ROLES)}, got {role_value!r}")
    seed = int(os.getenv("INCIDENT_LAB_SEED", "42"))
    transport = os.getenv("INCIDENT_LAB_TRANSPORT", "stdio")
    fault_profile = cast(FaultProfile, os.getenv("INCIDENT_LAB_FAULT_PROFILE", "standard"))
    if fault_profile not in {"none", "standard", "adversarial"}:
        raise SystemExit("INCIDENT_LAB_FAULT_PROFILE must be one of none, standard, adversarial")
    audit_value = os.getenv("INCIDENT_LAB_AUDIT_PATH")
    server = build_server(
        cast(Role, role_value),
        seed,
        fault_profile=fault_profile,
        audit_path=Path(audit_value) if audit_value else None,
    )
    server.run(transport=transport)


if __name__ == "__main__":
    main()
