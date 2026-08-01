"""Native LangGraph fan-out investigation and deterministic evidence join."""

from __future__ import annotations

import argparse
import asyncio
import json
import operator
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
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
Commander = Callable[[list["SpecialistFinding"]], Awaitable["CommanderSynthesis"]]


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


class CommanderSynthesis(LaneModel):
    role: Literal[AgentRole.INCIDENT_COMMANDER] = AgentRole.INCIDENT_COMMANDER
    root_cause: str = Field(min_length=1)
    chronology: list[str] = Field(min_length=2, max_length=6)
    evidence_ids: list[str] = Field(min_length=MIN_EVIDENCE_IDS, max_length=12)
    proposed_mitigation: str = Field(min_length=1)
    recovery_checks: list[str] = Field(min_length=1, max_length=6)
    evidence_sufficient: bool
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class EvidencePacket(LaneModel):
    passed: bool
    findings: list[SpecialistFinding]
    synthesis: CommanderSynthesis
    evidence_ids: list[str]
    independent_roles: list[AgentRole]
    gate_reasons: list[str]
    attempt: int = Field(ge=0, le=1)


class RoleRunStats(LaneModel):
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _InvestigationState(TypedDict):
    public_ticket: str
    attempt: int
    feedback: tuple[str, ...]
    findings: Annotated[list[dict[str, Any]], operator.add]
    synthesis: dict[str, Any] | None
    packet: dict[str, Any] | None


class _SpecialistNode:
    def __init__(self, role: AgentRole, investigator: Investigator) -> None:
        self._role = role
        self._investigator = investigator

    async def __call__(self, state: _InvestigationState) -> dict[str, list[dict[str, Any]]]:
        task = InvestigationTask(
            public_ticket=state["public_ticket"],
            attempt=state["attempt"],
            feedback=state["feedback"],
        )
        finding = await self._investigator(task)
        if finding.role is not self._role:
            raise ValueError(
                f"{self._role.value} investigator returned finding for {finding.role.value}"
            )
        return {"findings": [finding.model_dump(mode="json")]}


class _CommanderNode:
    def __init__(self, commander: Commander) -> None:
        self._commander = commander

    async def __call__(self, state: _InvestigationState) -> dict[str, dict[str, Any]]:
        findings = sorted(
            (SpecialistFinding.model_validate(payload) for payload in state["findings"]),
            key=lambda item: item.role.value,
        )
        synthesis = await self._commander(findings)
        return {"synthesis": synthesis.model_dump(mode="json")}


def _evidence_gate(state: _InvestigationState) -> dict[str, dict[str, Any]]:
    findings = sorted(
        (SpecialistFinding.model_validate(payload) for payload in state["findings"]),
        key=lambda item: item.role.value,
    )
    synthesis = CommanderSynthesis.model_validate(state["synthesis"])
    roles = sorted({finding.role for finding in findings}, key=lambda role: role.value)
    available_evidence_ids = {
        evidence_id for finding in findings for evidence_id in finding.evidence_ids
    }
    cited_evidence_ids = set(synthesis.evidence_ids)
    cited_roles = sorted(
        {
            finding.role
            for finding in findings
            if cited_evidence_ids.intersection(finding.evidence_ids)
        },
        key=lambda role: role.value,
    )
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
    if not synthesis.evidence_sufficient:
        reasons.append("incident commander marked the evidence insufficient")
    unknown_evidence_ids = sorted(cited_evidence_ids - available_evidence_ids)
    if unknown_evidence_ids:
        reasons.append(f"commander cited unknown evidence identifiers: {unknown_evidence_ids}")
    if len(cited_roles) < MIN_INDEPENDENT_ROLES:
        reasons.append("commander synthesis must cite at least two specialist roles")
    packet = EvidencePacket(
        passed=not reasons,
        findings=findings,
        synthesis=synthesis,
        evidence_ids=evidence_ids,
        independent_roles=cited_roles,
        gate_reasons=reasons,
        attempt=state["attempt"],
    )
    return {"packet": packet.model_dump(mode="json")}


def build_investigation_graph(
    investigators: dict[AgentRole, Investigator],
    commander: Commander,
) -> CompiledStateGraph:
    """Build the native parallel investigation, commander synthesis, and evidence gate."""

    if set(investigators) != set(SPECIALIST_ROLES):
        missing = sorted(role.value for role in set(SPECIALIST_ROLES) - set(investigators))
        extra = sorted(role.value for role in set(investigators) - set(SPECIALIST_ROLES))
        raise ValueError(f"investigator role mismatch: missing={missing}, extra={extra}")
    builder = StateGraph(_InvestigationState)
    specialist_nodes: list[str] = []
    for role in SPECIALIST_ROLES:
        node_name = role.value
        specialist_nodes.append(node_name)
        builder.add_node(node_name, _SpecialistNode(role, investigators[role]))
        builder.add_edge(START, node_name)
    builder.add_node(AgentRole.INCIDENT_COMMANDER.value, _CommanderNode(commander))
    builder.add_node("evidence_gate", _evidence_gate)
    builder.add_edge(specialist_nodes, AgentRole.INCIDENT_COMMANDER.value)
    builder.add_edge(AgentRole.INCIDENT_COMMANDER.value, "evidence_gate")
    builder.add_edge("evidence_gate", END)
    return builder.compile(name="agent-lab-langgraph-explicit-investigation-v1")


class LangGraphRoleAgent:
    """One LangChain agent bound to exactly one role endpoint and shared model profile."""

    def __init__(self, role: AgentRole, endpoint: RoleEndpoint, profile: ModelProfile) -> None:
        if endpoint.role is not role:
            raise ValueError("role endpoint does not match the specialist agent")
        self.role = role
        self.endpoint = endpoint
        self.profile = profile
        self.stats = RoleRunStats(model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0)
        self._mcp = MultiServerMCPClient(
            {
                "incident": {
                    "transport": "streamable_http",
                    "url": endpoint.url,
                    "headers": {"Authorization": f"Bearer {endpoint.token}"},
                }
            }
        )
        self._tools: dict[str, BaseTool] = {}
        client = ChatOpenAI(
            model=profile.name,
            api_key="agent-lab-local",
            base_url=profile.base_url,
            temperature=profile.temperature,
            top_p=profile.top_p,
            presence_penalty=profile.presence_penalty,
            max_completion_tokens=min(profile.max_output_tokens, 2048),
            seed=profile.seed,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            stream_usage=True,
            default_headers={"X-Agent-Lab-Framework": "langgraph"},
        )
        self._structured_client = client.with_structured_output(
            SpecialistFinding,
            method="json_schema",
            include_raw=True,
            strict=True,
        )

    async def initialize(self) -> LangGraphRoleAgent:
        tools = await self._mcp.get_tools(server_name="incident")
        expected = {tool.name for tool in tool_definitions_for_role(self.role)}
        observed = {tool.name for tool in tools}
        if observed != expected:
            raise RuntimeError(
                f"{self.role.value} MCP catalog mismatch: "
                f"expected={sorted(expected)}, observed={sorted(observed)}"
            )
        self._tools = {tool.name: tool for tool in tools}
        return self

    async def investigate(self, task: InvestigationTask) -> SpecialistFinding:
        observations = []
        observed_evidence_ids: set[str] = set()
        called_tools: list[str] = []
        for tool_name, arguments in _observation_calls(self.role):
            result = await self._tools[tool_name].ainvoke(arguments)
            called_tools.append(tool_name)
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
        response = await self._structured_client.ainvoke(
            [("system", render_role_prompt(self.role)), ("user", prompt)]
        )
        if not isinstance(response, dict):
            raise TypeError(f"{self.role.value} returned an invalid structured response")
        if response.get("parsing_error") is not None:
            raise ValueError(
                f"{self.role.value} structured response failed: {response['parsing_error']}"
            )
        finding = response.get("parsed")
        if not isinstance(finding, SpecialistFinding):
            raise TypeError(f"{self.role.value} did not return a SpecialistFinding")
        if finding.role is not self.role:
            raise ValueError(f"{self.role.value} returned role {finding.role.value}")
        tool_names = set(called_tools)
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
        usage = _usage(response.get("raw"))
        self.stats = RoleRunStats(
            model_calls=1,
            tool_calls=len(called_tools),
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        return finding


class LangGraphCommanderAgent:
    """LangChain incident commander that synthesizes evidence without MCP mutation."""

    def __init__(self, endpoint: RoleEndpoint, profile: ModelProfile) -> None:
        if endpoint.role is not AgentRole.INCIDENT_COMMANDER:
            raise ValueError("commander endpoint does not match the incident commander")
        self.stats = RoleRunStats(model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0)
        client = ChatOpenAI(
            model=profile.name,
            api_key="agent-lab-local",
            base_url=profile.base_url,
            temperature=profile.temperature,
            top_p=profile.top_p,
            presence_penalty=profile.presence_penalty,
            max_completion_tokens=min(profile.max_output_tokens, 2048),
            seed=profile.seed,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            stream_usage=True,
            default_headers={"X-Agent-Lab-Framework": "langgraph"},
        )
        self._structured_client = client.with_structured_output(
            CommanderSynthesis,
            method="json_schema",
            include_raw=True,
            strict=True,
        )

    async def synthesize(self, findings: list[SpecialistFinding]) -> CommanderSynthesis:
        available_ids = sorted(
            {evidence_id for finding in findings for evidence_id in finding.evidence_ids}
        )
        prompt = (
            "Synthesize the specialist findings below into one incident-command decision. "
            "Use only their claims and evidence identifiers. Cite evidence from at least two "
            "specialist roles, describe a chronology with at least two steps, propose the "
            "least-destructive reversible mitigation, and name post-mitigation recovery checks. "
            "Set evidence_sufficient true only when those requirements are met. Do not call tools "
            "or claim that a mitigation has run.\n\n"
            f"Available evidence IDs: {json.dumps(available_ids)}\n"
            f"Specialist findings:\n"
            f"{json.dumps([item.model_dump(mode='json') for item in findings], sort_keys=True)}"
        )
        response = await self._structured_client.ainvoke(
            [("system", render_role_prompt(AgentRole.INCIDENT_COMMANDER)), ("user", prompt)]
        )
        if not isinstance(response, dict):
            raise TypeError("incident commander returned an invalid structured response")
        if response.get("parsing_error") is not None:
            raise ValueError(
                f"incident commander structured response failed: {response['parsing_error']}"
            )
        synthesis = response.get("parsed")
        if not isinstance(synthesis, CommanderSynthesis):
            raise TypeError("incident commander did not return a CommanderSynthesis")
        usage = _usage(response.get("raw"))
        self.stats = RoleRunStats(
            model_calls=1,
            tool_calls=0,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        return synthesis


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, AIMessage):
        return {"input_tokens": 0, "output_tokens": 0}
    usage = raw.usage_metadata or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


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
        text_parts = [
            item["text"]
            for item in result
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if text_parts:
            return "\n".join(text_parts)
        return json.dumps(result, sort_keys=True)
    if isinstance(result, dict):
        return json.dumps(result, sort_keys=True)
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

    required_roles = (*SPECIALIST_ROLES, AgentRole.INCIDENT_COMMANDER)
    missing = [role.value for role in required_roles if role not in request.role_endpoints]
    if missing:
        raise ValueError(f"run request is missing workflow endpoints: {missing}")
    agents = {
        role: await LangGraphRoleAgent(
            role, request.role_endpoints[role], request.manifest.model
        ).initialize()
        for role in SPECIALIST_ROLES
    }
    commander_agent = LangGraphCommanderAgent(
        request.role_endpoints[AgentRole.INCIDENT_COMMANDER], request.manifest.model
    )
    graph = build_investigation_graph(
        {role: agent.investigate for role, agent in agents.items()},
        commander_agent.synthesize,
    )
    task = InvestigationTask(public_ticket=request.public_ticket)
    state = await graph.ainvoke(
        {
            "public_ticket": task.public_ticket,
            "attempt": task.attempt,
            "feedback": task.feedback,
            "findings": [],
            "synthesis": None,
            "packet": None,
        }
    )
    packet = EvidencePacket.model_validate(state["packet"])
    return {
        "framework": "langgraph",
        "model": request.manifest.model.name,
        "packet": packet.model_dump(mode="json"),
        "role_stats": {
            **{role.value: agent.stats.model_dump(mode="json") for role, agent in agents.items()},
            AgentRole.INCIDENT_COMMANDER.value: commander_agent.stats.model_dump(mode="json"),
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

    async def synthesize(findings: list[SpecialistFinding]) -> CommanderSynthesis:
        return CommanderSynthesis(
            root_cause="correlated root cause",
            chronology=["change preceded symptoms", "telemetry confirmed impact"],
            evidence_ids=sorted(
                {evidence_id for finding in findings for evidence_id in finding.evidence_ids}
            ),
            proposed_mitigation="apply the reversible runbook action",
            recovery_checks=["verify affected service health"],
            evidence_sufficient=True,
        )

    graph = build_investigation_graph(investigators, synthesize)
    state = await graph.ainvoke(
        {
            "public_ticket": "deterministic probe",
            "attempt": 0,
            "feedback": (),
            "findings": [],
            "synthesis": None,
            "packet": None,
        }
    )
    return EvidencePacket.model_validate(state["packet"]).model_dump(mode="json")


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
