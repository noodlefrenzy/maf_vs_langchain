"""Native LangGraph fan-out investigation and deterministic evidence join."""

from __future__ import annotations

import argparse
import asyncio
import json
import operator
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_lab.contracts import AgentRole, ApprovalDecision, ApprovalRequest, ModelProfile
from agent_lab.prompt_bundle import render_role_prompt
from agent_lab.runner_host import ApprovalProvider
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.tool_catalog import (
    MitigationResult,
    RecoveryCheckResult,
    tool_definitions_for_role,
)
from agent_lab.workflow_telemetry import NOOP_WORKFLOW_TELEMETRY, WorkflowTelemetry
from langgraph_runner.checkpoint_compat import _FileBackedSaver

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
MitigationPreparer = Callable[["EvidencePacket"], Awaitable[ApprovalRequest]]
MitigationExecutor = Callable[[str], Awaitable[tuple[MitigationResult, RecoveryCheckResult]]]


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


class ApprovalResponse(LaneModel):
    request_id: str
    decision: ApprovalDecision
    action_to_execute: str | None = None
    approval_token: str | None = Field(default=None, repr=False, exclude=True)


class WorkflowExecutionResult(LaneModel):
    status: Literal["completed", "approval_rejected"]
    evidence: EvidencePacket
    approval_request: ApprovalRequest
    approval_decision: ApprovalDecision
    action_to_execute: str | None = None
    mitigation: MitigationResult | None = None
    recovery: RecoveryCheckResult | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> WorkflowExecutionResult:
        if self.status == "completed":
            if self.approval_decision not in {ApprovalDecision.APPROVE, ApprovalDecision.EDIT}:
                raise ValueError("completed execution requires an approved decision")
            if not self.action_to_execute or self.mitigation is None or self.recovery is None:
                raise ValueError("completed execution requires action, mitigation, and recovery")
        elif (
            self.approval_decision is not ApprovalDecision.REJECT
            or self.action_to_execute is not None
            or self.mitigation is not None
            or self.recovery is not None
        ):
            raise ValueError("rejected execution cannot contain authorized execution state")
        return self


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
    approval_request: dict[str, Any] | None
    approval_decision: str | None
    action_to_execute: str | None
    execution: dict[str, Any] | None


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


class _PrepareMitigationNode:
    def __init__(self, preparer: MitigationPreparer) -> None:
        self._preparer = preparer

    async def __call__(self, state: _InvestigationState) -> dict[str, dict[str, Any]]:
        evidence = EvidencePacket.model_validate(state["packet"])
        request = await self._preparer(evidence)
        return {"approval_request": request.model_dump(mode="json")}


def _approval_node(state: _InvestigationState) -> dict[str, Any]:
    request = ApprovalRequest.model_validate(state["approval_request"])
    raw_response = interrupt(
        {
            "kind": "approval_requested",
            "request": request.model_dump(mode="json"),
        }
    )
    response = ApprovalResponse.model_validate(raw_response)
    if response.request_id != request.request_id:
        raise ValueError("approval response request ID differs from the pending request")
    evidence = EvidencePacket.model_validate(state["packet"])
    if response.decision is ApprovalDecision.REJECT:
        execution = WorkflowExecutionResult(
            status="approval_rejected",
            evidence=evidence,
            approval_request=request,
            approval_decision=response.decision,
        )
        return {
            "approval_decision": response.decision.value,
            "execution": execution.model_dump(mode="json"),
        }
    if not response.action_to_execute:
        raise ValueError("approved mitigation requires the authorized action")
    return {
        "approval_decision": response.decision.value,
        "action_to_execute": response.action_to_execute,
    }


class _MitigateAndVerifyNode:
    def __init__(self, executor: MitigationExecutor) -> None:
        self._executor = executor

    async def __call__(self, state: _InvestigationState) -> dict[str, dict[str, Any]]:
        request = ApprovalRequest.model_validate(state["approval_request"])
        evidence = EvidencePacket.model_validate(state["packet"])
        decision = ApprovalDecision(state["approval_decision"])
        mitigation, recovery = await self._executor(request.request_id)
        execution = WorkflowExecutionResult(
            status="completed",
            evidence=evidence,
            approval_request=request,
            approval_decision=decision,
            action_to_execute=state["action_to_execute"],
            mitigation=mitigation,
            recovery=recovery,
        )
        return {"execution": execution.model_dump(mode="json")}


def _route_after_gate(state: _InvestigationState) -> str:
    packet = EvidencePacket.model_validate(state["packet"])
    return "prepare_mitigation" if packet.passed else END


def _route_after_approval(state: _InvestigationState) -> str:
    return END if state.get("execution") is not None else "mitigation_and_recovery"


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


def build_approved_graph(
    investigators: dict[AgentRole, Investigator],
    commander: Commander,
    preparer: MitigationPreparer,
    executor: MitigationExecutor,
    checkpointer: _FileBackedSaver,
) -> CompiledStateGraph:
    """Build the full native graph through approval, mitigation, and recovery."""

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
    builder.add_node("prepare_mitigation", _PrepareMitigationNode(preparer))
    builder.add_node("approval", _approval_node)
    builder.add_node("mitigation_and_recovery", _MitigateAndVerifyNode(executor))
    builder.add_edge(specialist_nodes, AgentRole.INCIDENT_COMMANDER.value)
    builder.add_edge(AgentRole.INCIDENT_COMMANDER.value, "evidence_gate")
    builder.add_conditional_edges("evidence_gate", _route_after_gate)
    builder.add_edge("prepare_mitigation", "approval")
    builder.add_conditional_edges("approval", _route_after_approval)
    builder.add_edge("mitigation_and_recovery", END)
    return builder.compile(
        checkpointer=checkpointer,
        name="agent-lab-langgraph-explicit-approved-v1",
    )


class LangGraphRoleAgent:
    """One LangChain agent bound to exactly one role endpoint and shared model profile."""

    def __init__(
        self,
        role: AgentRole,
        endpoint: RoleEndpoint,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry = NOOP_WORKFLOW_TELEMETRY,
    ) -> None:
        if endpoint.role is not role:
            raise ValueError("role endpoint does not match the specialist agent")
        self.role = role
        self.endpoint = endpoint
        self.profile = profile
        self.telemetry = telemetry
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
            result = await self._call_tool(tool_name, arguments)
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
        await self.telemetry.model_started(actor=self.role.value, model=self.profile.name)
        response = await self._structured_client.ainvoke(
            [("system", render_role_prompt(self.role)), ("user", prompt)]
        )
        usage = _usage(response.get("raw") if isinstance(response, dict) else None)
        await self.telemetry.model_finished(
            actor=self.role.value,
            model=self.profile.name,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
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
        self.stats = RoleRunStats(
            model_calls=1,
            tool_calls=len(called_tools),
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        return finding

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self.telemetry.tool_started(actor=self.role.value, tool_name=name)
        try:
            result = await self._tools[name].ainvoke(arguments)
        except Exception:
            await self.telemetry.tool_finished(
                actor=self.role.value,
                tool_name=name,
                succeeded=False,
            )
            raise
        await self.telemetry.tool_finished(
            actor=self.role.value,
            tool_name=name,
            succeeded=True,
        )
        return result


class LangGraphCommanderAgent:
    """LangChain incident commander that synthesizes evidence without MCP mutation."""

    def __init__(
        self,
        endpoint: RoleEndpoint,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry = NOOP_WORKFLOW_TELEMETRY,
    ) -> None:
        if endpoint.role is not AgentRole.INCIDENT_COMMANDER:
            raise ValueError("commander endpoint does not match the incident commander")
        self.stats = RoleRunStats(model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0)
        self.profile = profile
        self.telemetry = telemetry
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
        self._called_tools: list[str] = []
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

    async def initialize(self) -> LangGraphCommanderAgent:
        tools = await self._mcp.get_tools(server_name="incident")
        expected = {tool.name for tool in tool_definitions_for_role(AgentRole.INCIDENT_COMMANDER)}
        observed = {tool.name for tool in tools}
        if observed != expected:
            raise RuntimeError(
                "incident commander MCP catalog mismatch: "
                f"expected={sorted(expected)}, observed={sorted(observed)}"
            )
        self._tools = {tool.name: tool for tool in tools}
        return self

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
        await self.telemetry.model_started(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            model=self.profile.name,
        )
        response = await self._structured_client.ainvoke(
            [("system", render_role_prompt(AgentRole.INCIDENT_COMMANDER)), ("user", prompt)]
        )
        usage = _usage(response.get("raw") if isinstance(response, dict) else None)
        await self.telemetry.model_finished(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            model=self.profile.name,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
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
        self.stats = RoleRunStats(
            model_calls=1,
            tool_calls=0,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        return synthesis

    async def prepare_mitigation(self, evidence: EvidencePacket) -> ApprovalRequest:
        synthesis = evidence.synthesis
        await self._call_tool(
            "submit_hypothesis",
            {
                "root_cause": synthesis.root_cause,
                "evidence_ids": synthesis.evidence_ids,
                "mitigation": synthesis.proposed_mitigation,
            },
        )
        result = await self._call_tool(
            "request_mitigation",
            {
                "proposed_action": synthesis.proposed_mitigation,
                "rationale": (
                    "The incident commander synthesized independently sourced specialist evidence."
                ),
                "evidence_ids": synthesis.evidence_ids,
            },
        )
        return ApprovalRequest.model_validate(
            _structured_payload(result, {"request_id", "proposed_action", "status"})
        )

    async def apply_and_verify(
        self,
        request_id: str,
        approval_token: str,
    ) -> tuple[MitigationResult, RecoveryCheckResult]:
        mitigation_result = await self._call_tool(
            "apply_mitigation",
            {"request_id": request_id, "approval_token": approval_token},
        )
        recovery_result = await self._call_tool(
            "verify_recovery",
            {"services": ["checkout-api"]},
        )
        mitigation = MitigationResult.model_validate(
            _structured_payload(mitigation_result, {"request_id", "effective", "action"})
        )
        recovery = RecoveryCheckResult.model_validate(
            _structured_payload(recovery_result, {"case_id", "recovered", "checks"})
        )
        return mitigation, recovery

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        actor = AgentRole.INCIDENT_COMMANDER.value
        await self.telemetry.tool_started(actor=actor, tool_name=name)
        try:
            result = await self._tools[name].ainvoke(arguments)
        except Exception:
            await self.telemetry.tool_finished(
                actor=actor,
                tool_name=name,
                succeeded=False,
            )
            raise
        await self.telemetry.tool_finished(
            actor=actor,
            tool_name=name,
            succeeded=True,
        )
        self._called_tools.append(name)
        self.stats = self.stats.model_copy(update={"tool_calls": len(self._called_tools)})
        return result


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


def _structured_payload(result: Any, required_keys: set[str]) -> dict[str, Any]:
    rendered = _render_tool_result(result)
    decoder = json.JSONDecoder()
    for index, character in enumerate(rendered):
        if character not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(rendered, index)
        except json.JSONDecodeError:
            continue
        found = _find_payload(value, required_keys)
        if found is not None:
            return found
    raise ValueError(f"MCP result did not contain required fields {sorted(required_keys)}")


def _find_payload(value: Any, required_keys: set[str]) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if required_keys.issubset(value):
            return value
        for child in value.values():
            found = _find_payload(child, required_keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_payload(child, required_keys)
            if found is not None:
                return found
    return None


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
    commander_agent = await LangGraphCommanderAgent(
        request.role_endpoints[AgentRole.INCIDENT_COMMANDER], request.manifest.model
    ).initialize()
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
            "approval_request": None,
            "approval_decision": None,
            "action_to_execute": None,
            "execution": None,
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


async def run_live_approved(
    request: RunRequest,
    approval_provider: ApprovalProvider | None = None,
    telemetry: WorkflowTelemetry = NOOP_WORKFLOW_TELEMETRY,
) -> dict[str, Any]:
    """Run through native approval, then apply mitigation and verify recovery."""

    required_roles = (*SPECIALIST_ROLES, AgentRole.INCIDENT_COMMANDER)
    missing = [role.value for role in required_roles if role not in request.role_endpoints]
    if missing:
        raise ValueError(f"run request is missing workflow endpoints: {missing}")
    agents = {
        role: await LangGraphRoleAgent(
            role,
            request.role_endpoints[role],
            request.manifest.model,
            telemetry,
        ).initialize()
        for role in SPECIALIST_ROLES
    }
    commander_agent = await LangGraphCommanderAgent(
        request.role_endpoints[AgentRole.INCIDENT_COMMANDER],
        request.manifest.model,
        telemetry,
    ).initialize()
    approval_tokens: dict[str, str] = {}

    async def execute(request_id: str) -> tuple[MitigationResult, RecoveryCheckResult]:
        token = approval_tokens.pop(request_id, None)
        if token is None:
            raise RuntimeError("approved mitigation token was not available in process memory")
        return await commander_agent.apply_and_verify(request_id, token)

    checkpointer = _FileBackedSaver(request.paths.checkpoints / "workflow.json")
    graph = build_approved_graph(
        {role: agent.investigate for role, agent in agents.items()},
        commander_agent.synthesize,
        commander_agent.prepare_mitigation,
        execute,
        checkpointer,
    )
    task = InvestigationTask(public_ticket=request.public_ticket)
    config = {"configurable": {"thread_id": str(request.manifest.run_id)}}
    initial = await graph.ainvoke(
        {
            "public_ticket": task.public_ticket,
            "attempt": task.attempt,
            "feedback": task.feedback,
            "findings": [],
            "synthesis": None,
            "packet": None,
            "approval_request": None,
            "approval_decision": None,
            "action_to_execute": None,
            "execution": None,
        },
        config,
    )
    if "__interrupt__" not in initial:
        raise RuntimeError("LangGraph did not persist an approval interrupt")
    snapshot = graph.get_state(config)
    approval_request = ApprovalRequest.model_validate(snapshot.values["approval_request"])
    if approval_provider is None:
        _emit_json_line(
            {
                "kind": "approval_requested",
                "request": approval_request.model_dump(mode="json"),
            }
        )
        response_line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not response_line:
            raise RuntimeError("approval response stream closed before a decision")
        response = ApprovalResponse.model_validate_json(response_line)
    else:
        response = ApprovalResponse.model_validate(await approval_provider(approval_request))
    if response.request_id != approval_request.request_id:
        raise ValueError("approval response request ID differs from the pending request")
    if response.decision in {ApprovalDecision.APPROVE, ApprovalDecision.EDIT}:
        if not response.approval_token or not response.action_to_execute:
            raise ValueError("approved response requires an action and an approval token")
        approval_tokens[response.request_id] = response.approval_token
    resumed = await graph.ainvoke(
        Command(resume=response.model_dump(mode="json")),
        config,
    )
    execution = WorkflowExecutionResult.model_validate(resumed["execution"])
    return {
        "framework": "langgraph",
        "model": request.manifest.model.name,
        "execution": execution.model_dump(mode="json"),
        "role_stats": {
            **{role.value: agent.stats.model_dump(mode="json") for role, agent in agents.items()},
            AgentRole.INCIDENT_COMMANDER.value: commander_agent.stats.model_dump(mode="json"),
        },
    }


def _emit_json_line(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


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
            "approval_request": None,
            "approval_decision": None,
            "action_to_execute": None,
            "execution": None,
        }
    )
    return EvidencePacket.model_validate(state["packet"]).model_dump(mode="json")


async def _deterministic_approved_probe() -> dict[str, Any]:
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

    async def prepare(evidence: EvidencePacket) -> ApprovalRequest:
        return ApprovalRequest(
            request_id="mit-deterministic-01",
            case_id="case-deterministic",
            proposed_action=evidence.synthesis.proposed_mitigation,
            rationale="grounded deterministic evidence",
            evidence_ids=evidence.synthesis.evidence_ids,
            requested_by=AgentRole.INCIDENT_COMMANDER.value,
            created_tick=1,
        )

    async def execute(_request_id: str) -> tuple[MitigationResult, RecoveryCheckResult]:
        mitigation = MitigationResult(
            case_id="case-deterministic",
            request_id="mit-deterministic-01",
            world_tick=2,
            action="apply the reversible runbook action",
            status="applied",
            effective=True,
        )
        recovery = RecoveryCheckResult.model_validate(
            {
                "evidence_id": "ev-recovery",
                "case_id": "case-deterministic",
                "world_tick": 3,
                "recovered": True,
                "checks": [
                    {
                        "evidence_id": "ev-health-checkout",
                        "world_tick": 3,
                        "service": "checkout-api",
                        "error_rate": 0.01,
                        "p95_ms": 260.0,
                        "saturation": 0.38,
                    }
                ],
                "summary": "customer-facing health recovered after mitigation",
            }
        )
        return mitigation, recovery

    with tempfile.TemporaryDirectory(prefix="agent-lab-langgraph-approved-probe-") as raw_path:
        saver = _FileBackedSaver(Path(raw_path) / "workflow.json")
        graph = build_approved_graph(investigators, synthesize, prepare, execute, saver)
        config = {"configurable": {"thread_id": "deterministic-approved-probe"}}
        initial = await graph.ainvoke(
            {
                "public_ticket": "deterministic probe",
                "attempt": 0,
                "feedback": (),
                "findings": [],
                "synthesis": None,
                "packet": None,
                "approval_request": None,
                "approval_decision": None,
                "action_to_execute": None,
                "execution": None,
            },
            config,
        )
        if "__interrupt__" not in initial:
            raise RuntimeError("deterministic LangGraph did not pause for approval")
        resumed = await graph.ainvoke(
            Command(
                resume={
                    "request_id": "mit-deterministic-01",
                    "decision": ApprovalDecision.APPROVE.value,
                    "action_to_execute": "apply the reversible runbook action",
                }
            ),
            config,
        )
        return WorkflowExecutionResult.model_validate(resumed["execution"]).model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deterministic-probe", action="store_true")
    mode.add_argument("--deterministic-approved-probe", action="store_true")
    mode.add_argument("--live-investigation", action="store_true")
    mode.add_argument("--live-approved-run", action="store_true")
    args = parser.parse_args()
    if args.deterministic_probe:
        payload = asyncio.run(_deterministic_probe())
    elif args.deterministic_approved_probe:
        payload = asyncio.run(_deterministic_approved_probe())
    elif args.live_approved_run:
        request_line = sys.stdin.buffer.readline()
        if not request_line:
            parser.error("live approved run requires one RunRequest JSON line")
        payload = asyncio.run(run_live_approved(RunRequest.model_validate_json(request_line)))
    else:
        payload = asyncio.run(
            run_live_investigation(RunRequest.model_validate_json(sys.stdin.read()))
        )
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
