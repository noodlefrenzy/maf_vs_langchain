"""Native MAF fan-out investigation and deterministic evidence join."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from agent_framework import (
    Agent,
    Executor,
    MCPStreamableHTTPTool,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
)
from agent_framework.openai import OpenAIChatCompletionClient
from httpx import AsyncClient, Timeout
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from agent_lab.contracts import AgentRole, ModelProfile
from agent_lab.prompt_bundle import render_role_prompt
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.tool_catalog import tool_definitions_for_role

SPECIALIST_ROLES = (
    AgentRole.TELEMETRY_INVESTIGATOR,
    AgentRole.CHANGE_INVESTIGATOR,
    AgentRole.RELIABILITY_REVIEWER,
)
MAX_CLAIMS_PER_ROLE = 6
MAX_EVIDENCE_IDS_PER_ROLE = 8
MIN_INDEPENDENT_ROLES = 2
MIN_EVIDENCE_IDS = 2

Investigator = Callable[["InvestigationTask"], Awaitable["SpecialistFinding"]]


class LaneModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvestigationTask(LaneModel):
    public_ticket: str
    attempt: int = Field(default=0, ge=0, le=1)
    feedback: tuple[str, ...] = ()


class SpecialistFinding(LaneModel):
    role: AgentRole
    summary: str = Field(min_length=1)
    claims: list[str] = Field(min_length=1, max_length=MAX_CLAIMS_PER_ROLE)
    evidence_ids: list[str] = Field(min_length=1, max_length=MAX_EVIDENCE_IDS_PER_ROLE)
    tools_used: list[str] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class EvidencePacket(LaneModel):
    passed: bool
    findings: list[SpecialistFinding]
    evidence_ids: list[str]
    independent_roles: list[AgentRole]
    gate_reasons: list[str]
    attempt: int = Field(ge=0, le=1)


class RoleRunStats(LaneModel):
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _TrackingMCPStreamableHTTPTool(MCPStreamableHTTPTool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.called_tools: list[str] = []

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        self.called_tools.append(tool_name)
        return await super().call_tool(tool_name, **kwargs)


class _Dispatcher(Executor):
    @handler
    async def dispatch(self, payload: dict[str, Any], ctx: WorkflowContext[dict[str, Any]]) -> None:
        task = InvestigationTask.model_validate(payload)
        await ctx.send_message(task.model_dump(mode="json"))


class _Specialist(Executor):
    def __init__(self, role: AgentRole, investigator: Investigator) -> None:
        super().__init__(role.value)
        self._role = role
        self._investigator = investigator

    @handler
    async def investigate(
        self,
        payload: dict[str, Any],
        ctx: WorkflowContext[dict[str, Any]],
    ) -> None:
        task = InvestigationTask.model_validate(payload)
        finding = await self._investigator(task)
        if finding.role is not self._role:
            raise ValueError(
                f"{self._role.value} investigator returned finding for {finding.role.value}"
            )
        await ctx.send_message(finding.model_dump(mode="json"))


class _EvidenceGate(Executor):
    @handler
    async def evaluate(
        self,
        payloads: list[dict[str, Any]],
        ctx: WorkflowContext[Any, dict[str, Any]],
    ) -> None:
        findings = sorted(
            (SpecialistFinding.model_validate(payload) for payload in payloads),
            key=lambda item: item.role.value,
        )
        roles = sorted({finding.role for finding in findings}, key=lambda role: role.value)
        evidence_ids = sorted(
            {evidence_id for finding in findings for evidence_id in finding.evidence_ids}
        )
        reasons: list[str] = []
        if set(roles) != set(SPECIALIST_ROLES):
            reasons.append("all three specialist roles must report")
        if len(roles) < MIN_INDEPENDENT_ROLES:
            reasons.append("at least two independent specialist roles are required")
        if len(evidence_ids) < MIN_EVIDENCE_IDS:
            reasons.append("at least two distinct evidence identifiers are required")
        if any(not finding.claims for finding in findings):
            reasons.append("every specialist finding must contain a claim")
        packet = EvidencePacket(
            passed=not reasons,
            findings=findings,
            evidence_ids=evidence_ids,
            independent_roles=roles,
            gate_reasons=reasons,
            attempt=max((int(payload.get("attempt", 0)) for payload in payloads), default=0),
        )
        await ctx.yield_output(packet.model_dump(mode="json"))


def build_investigation_workflow(investigators: dict[AgentRole, Investigator]) -> Workflow:
    """Build the native parallel specialist graph with a synchronized evidence join."""

    if set(investigators) != set(SPECIALIST_ROLES):
        missing = sorted(role.value for role in set(SPECIALIST_ROLES) - set(investigators))
        extra = sorted(role.value for role in set(investigators) - set(SPECIALIST_ROLES))
        raise ValueError(f"investigator role mismatch: missing={missing}, extra={extra}")
    dispatcher = _Dispatcher(id="investigation_dispatch")
    specialists = [_Specialist(role, investigators[role]) for role in SPECIALIST_ROLES]
    gate = _EvidenceGate(id="evidence_gate")
    return (
        WorkflowBuilder(
            start_executor=dispatcher,
            name="agent-lab-maf-explicit-investigation-v1",
            max_iterations=20,
            output_from=[gate],
        )
        .add_fan_out_edges(dispatcher, specialists)
        .add_fan_in_edges(specialists, gate)
        .build()
    )


class MafRoleAgent:
    """One MAF agent bound to exactly one role endpoint and shared model profile."""

    def __init__(self, role: AgentRole, endpoint: RoleEndpoint, profile: ModelProfile) -> None:
        if endpoint.role is not role:
            raise ValueError("role endpoint does not match the specialist agent")
        self.role = role
        self.endpoint = endpoint
        self.profile = profile
        self.stats = RoleRunStats(model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0)
        self._stack = AsyncExitStack()
        sdk = AsyncOpenAI(
            api_key="agent-lab-local",
            base_url=profile.base_url,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            default_headers={"X-Agent-Lab-Framework": "maf"},
        )
        client = OpenAIChatCompletionClient(
            model=profile.name,
            async_client=sdk,
            function_invocation_configuration={
                "enabled": True,
                "max_iterations": 5,
                "max_function_calls": 3,
            },
        )
        allowed_tools = [tool.name for tool in tool_definitions_for_role(role)]
        self._mcp_http_client = AsyncClient(
            headers={"Authorization": f"Bearer {endpoint.token}"},
            follow_redirects=False,
            timeout=Timeout(profile.request_timeout_seconds),
        )
        self._mcp = _TrackingMCPStreamableHTTPTool(
            name=f"incident-{role.value}",
            url=endpoint.url,
            allowed_tools=allowed_tools,
            load_prompts=False,
            request_timeout=int(profile.request_timeout_seconds),
            approval_mode="never_require",
            http_client=self._mcp_http_client,
        )
        self.agent = Agent(
            client,
            name=role.value,
            instructions=render_role_prompt(role),
            tools=[],
        )

    async def __aenter__(self) -> MafRoleAgent:
        await self._stack.enter_async_context(self._mcp_http_client)
        await self._stack.enter_async_context(self._mcp)
        await self._stack.enter_async_context(self.agent)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.__aexit__(*exc_info)

    async def investigate(self, task: InvestigationTask) -> SpecialistFinding:
        observations = []
        observed_evidence_ids: set[str] = set()
        for tool_name, arguments in _observation_calls(self.role):
            result = await self._mcp.call_tool(tool_name, **arguments)
            rendered = _render_tool_result(result)
            observed_evidence_ids.update(_evidence_ids(rendered))
            observations.append({"tool": tool_name, "result": rendered})
        prompt = (
            f"Public ticket:\n{task.public_ticket}\n\n"
            "The role-scoped MCP observations below were collected exactly once each with no "
            "retry. Use only these observations. Ground every claim in their returned evidence "
            f"IDs and return the requested finding for role {self.role.value}. The tools_used "
            "field must list exactly the observation tool names.\n\n"
            f"Observations:\n{json.dumps(observations, sort_keys=True)}"
        )
        if task.feedback:
            prompt += "\nEvidence-gate feedback:\n- " + "\n- ".join(task.feedback)
        response = await self.agent.run(
            prompt,
            options={
                "temperature": self.profile.temperature,
                "top_p": self.profile.top_p,
                "presence_penalty": self.profile.presence_penalty,
                "max_tokens": min(self.profile.max_output_tokens, 2048),
                "seed": self.profile.seed,
                "response_format": SpecialistFinding,
                "tool_choice": "none",
            },
        )
        if not isinstance(response.value, SpecialistFinding):
            raise TypeError(f"{self.role.value} did not return a SpecialistFinding")
        finding = response.value
        if finding.role is not self.role:
            raise ValueError(f"{self.role.value} returned role {finding.role.value}")
        tool_names = set(self._mcp.called_tools)
        if not tool_names:
            raise RuntimeError(f"{self.role.value} returned without calling a role-scoped MCP tool")
        if not set(finding.tools_used).issubset(tool_names):
            raise RuntimeError(
                f"{self.role.value} claimed tools not present in its native response: "
                f"claimed={finding.tools_used}, observed={sorted(tool_names)}"
            )
        if not observed_evidence_ids:
            raise RuntimeError(f"{self.role.value} MCP observations contained no evidence IDs")
        if not set(finding.evidence_ids).issubset(observed_evidence_ids):
            raise RuntimeError(
                f"{self.role.value} cited evidence not returned by its MCP tools: "
                f"cited={finding.evidence_ids}, observed={sorted(observed_evidence_ids)}"
            )
        usage = response.usage_details or {}
        self.stats = RoleRunStats(
            model_calls=sum(
                1 for message in response.messages if getattr(message, "role", None) == "assistant"
            ),
            tool_calls=len(tool_names),
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
        )
        return finding


def _observation_calls(role: AgentRole) -> tuple[tuple[str, dict[str, Any]], ...]:
    return {
        AgentRole.TELEMETRY_INVESTIGATOR: (
            ("query_service_health", {"service": "checkout-api"}),
            (
                "query_metric",
                {"service": "checkout-api", "metric": "retry_attempts", "start_tick": 0},
            ),
        ),
        AgentRole.CHANGE_INVESTIGATOR: (
            ("list_recent_changes", {"service": None, "since_tick": 0}),
            ("get_config_diff", {"service": "checkout-api", "change_id": None}),
        ),
        AgentRole.RELIABILITY_REVIEWER: (
            ("search_runbooks", {"query": "reversible mitigation", "limit": 5}),
            ("get_runbook", {"runbook_id": "reversible-mitigation"}),
        ),
    }[role]


def _render_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = [str(item.text) for item in result if isinstance(getattr(item, "text", None), str)]
        if parts:
            return "\n".join(parts)
    raise TypeError(f"unsupported MCP result type {type(result).__name__}")


def _evidence_ids(text: str) -> set[str]:
    values: set[str] = set()
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            payload, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        _collect_evidence_ids(payload, values)
    return values


def _collect_evidence_ids(value: Any, destination: set[str]) -> None:
    if isinstance(value, dict):
        evidence_id = value.get("evidence_id")
        if isinstance(evidence_id, str):
            destination.add(evidence_id)
        for child in value.values():
            _collect_evidence_ids(child, destination)
    elif isinstance(value, list):
        for child in value:
            _collect_evidence_ids(child, destination)


async def run_live_investigation(request: RunRequest) -> dict[str, Any]:
    """Run the native investigation graph with the request's model and role endpoints."""

    missing = [role.value for role in SPECIALIST_ROLES if role not in request.role_endpoints]
    if missing:
        raise ValueError(f"run request is missing specialist endpoints: {missing}")
    async with AsyncExitStack() as stack:
        agents: dict[AgentRole, MafRoleAgent] = {}
        for role in SPECIALIST_ROLES:
            agents[role] = await stack.enter_async_context(
                MafRoleAgent(role, request.role_endpoints[role], request.manifest.model)
            )
        workflow = build_investigation_workflow(
            {role: agent.investigate for role, agent in agents.items()}
        )
        result = await workflow.run(
            InvestigationTask(public_ticket=request.public_ticket).model_dump(mode="json")
        )
        outputs = [EvidencePacket.model_validate(output) for output in result.get_outputs()]
        if len(outputs) != 1:
            raise RuntimeError(f"MAF investigation produced {len(outputs)} evidence packets")
        return {
            "framework": "maf_workflow",
            "model": request.manifest.model.name,
            "packet": outputs[0].model_dump(mode="json"),
            "role_stats": {
                role.value: agent.stats.model_dump(mode="json") for role, agent in agents.items()
            },
        }


async def _deterministic_probe() -> dict[str, Any]:
    async def finding(role: AgentRole, evidence_id: str) -> SpecialistFinding:
        return SpecialistFinding(
            role=role,
            summary=f"{role.value} summary",
            claims=[f"claim from {role.value}"],
            evidence_ids=[evidence_id],
            tools_used=[f"{role.value}_tool"],
        )

    investigators = {
        AgentRole.TELEMETRY_INVESTIGATOR: lambda _task: finding(
            AgentRole.TELEMETRY_INVESTIGATOR, "ev-telemetry"
        ),
        AgentRole.CHANGE_INVESTIGATOR: lambda _task: finding(
            AgentRole.CHANGE_INVESTIGATOR, "ev-change"
        ),
        AgentRole.RELIABILITY_REVIEWER: lambda _task: finding(
            AgentRole.RELIABILITY_REVIEWER, "ev-runbook"
        ),
    }
    result = await build_investigation_workflow(investigators).run(
        InvestigationTask(public_ticket="deterministic probe").model_dump(mode="json")
    )
    outputs = [EvidencePacket.model_validate(output) for output in result.get_outputs()]
    return outputs[0].model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deterministic-probe", action="store_true")
    mode.add_argument("--live-investigation", action="store_true")
    args = parser.parse_args()
    if args.deterministic_probe:
        payload = asyncio.run(_deterministic_probe())
    else:
        payload = asyncio.run(
            run_live_investigation(RunRequest.model_validate_json(sys.stdin.read()))
        )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
