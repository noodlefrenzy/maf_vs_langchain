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
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from agent_lab.contracts import AgentRole, ApprovalDecision, ApprovalRequest, ModelProfile
from agent_lab.model_request_policy import langchain_model_kwargs, model_request_headers
from agent_lab.prompt_bundle import render_role_prompt
from agent_lab.runner_host import ApprovalProvider
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.scenario import MitigationActionId
from agent_lab.tool_catalog import (
    MitigationResult,
    RecoveryCheckResult,
    tool_definitions_for_role,
)
from agent_lab.workflow_contracts import (
    SPECIALIST_ROLES,
    ApprovalResponse,
    CommanderSynthesis,
    EvidencePacket,
    InvestigationTask,
    RoleRunStats,
    SpecialistFinding,
    WorkflowExecutionResult,
    commander_synthesis_prompt,
    evaluate_evidence,
    evidence_ids_from_text,
    invoke_tool_with_policy,
    observation_calls,
    render_tool_result,
    specialist_observation_prompt,
    structured_tool_payload,
)
from agent_lab.workflow_errors import EvidenceGateFailed
from agent_lab.workflow_telemetry import NOOP_WORKFLOW_TELEMETRY, WorkflowTelemetry
from langgraph_runner.chat_telemetry import (
    ProviderMetricsChatOpenAI,
    provider_telemetry_from_message,
)
from langgraph_runner.checkpoint_compat import _FileBackedSaver

Investigator = Callable[["InvestigationTask"], Awaitable["SpecialistFinding"]]
Commander = Callable[[list["SpecialistFinding"]], Awaitable["CommanderSynthesis"]]
MitigationPreparer = Callable[["EvidencePacket"], Awaitable[ApprovalRequest]]
MitigationExecutor = Callable[[str], Awaitable[tuple[MitigationResult, RecoveryCheckResult]]]


class _InvestigationState(TypedDict):
    public_ticket: str
    findings: Annotated[list[dict[str, Any]], operator.add]
    synthesis: dict[str, Any] | None
    packet: dict[str, Any] | None
    approval_request: dict[str, Any] | None
    approval_decision: str | None
    action_id_to_execute: str | None
    action_to_execute: str | None
    execution: dict[str, Any] | None


class _SpecialistNode:
    def __init__(self, role: AgentRole, investigator: Investigator) -> None:
        self._role = role
        self._investigator = investigator

    async def __call__(self, state: _InvestigationState) -> dict[str, list[dict[str, Any]]]:
        task = InvestigationTask(
            public_ticket=state["public_ticket"],
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
    packet = evaluate_evidence(findings, synthesis)
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
    if not response.action_id_to_execute or not response.action_to_execute:
        raise ValueError("approved mitigation requires the authorized action ID and description")
    return {
        "approval_decision": response.decision.value,
        "action_id_to_execute": response.action_id_to_execute.value,
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
            action_id_to_execute=MitigationActionId(state["action_id_to_execute"]),
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
        self._called_tools: list[str] = []
        client = ProviderMetricsChatOpenAI(
            api_key="agent-lab-local",
            default_headers=model_request_headers("langgraph", role.value),
            **langchain_model_kwargs(profile),
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
        await self.telemetry.handoff(
            source_actor=AgentRole.INCIDENT_COMMANDER.value,
            target_actor=self.role.value,
            phase="specialist_delegated",
        )
        observations = []
        observed_evidence_ids: set[str] = set()
        for tool_name, arguments in observation_calls(self.role):
            result = await self._call_tool(tool_name, arguments)
            rendered = render_tool_result(result)
            observed_evidence_ids.update(evidence_ids_from_text(rendered))
            observations.append({"tool": tool_name, "result": rendered})
        prompt = specialist_observation_prompt(task, self.role, observations)
        self.stats = self.stats.model_dispatched()
        await self.telemetry.model_started(actor=self.role.value, model=self.profile.name)
        response = await self._structured_client.ainvoke(
            [("system", render_role_prompt(self.role)), ("user", prompt)]
        )
        raw_response = response.get("raw") if isinstance(response, dict) else None
        usage = _usage(raw_response)
        await self.telemetry.model_finished(
            actor=self.role.value,
            model=self.profile.name,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
            parser_outcome=(
                "structured_parse_succeeded"
                if isinstance(response, dict)
                and response.get("parsing_error") is None
                and isinstance(response.get("parsed"), SpecialistFinding)
                else "structured_parse_failed"
            ),
            **provider_telemetry_from_message(raw_response),
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
        tool_names = set(self._called_tools)
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
        self.stats = self.stats.model_completed(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
        )
        await self.telemetry.handoff(
            source_actor=self.role.value,
            target_actor=AgentRole.INCIDENT_COMMANDER.value,
            phase="specialist_finding_returned",
        )
        return finding

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async def call() -> Any:
            self._called_tools.append(name)
            return await self._tools[name].ainvoke(arguments)

        async def attempt_started() -> None:
            self.stats = self.stats.tool_dispatched()
            await self.telemetry.tool_started(actor=self.role.value, tool_name=name)

        async def attempt_finished(succeeded: bool) -> None:
            await self.telemetry.tool_finished(
                actor=self.role.value,
                tool_name=name,
                succeeded=succeeded,
            )

        return await invoke_tool_with_policy(
            call,
            attempt_started=attempt_started,
            attempt_finished=attempt_finished,
        )


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
        client = ProviderMetricsChatOpenAI(
            api_key="agent-lab-local",
            default_headers=model_request_headers(
                "langgraph",
                AgentRole.INCIDENT_COMMANDER.value,
            ),
            **langchain_model_kwargs(profile),
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
        prompt = commander_synthesis_prompt(findings)
        self.stats = self.stats.model_dispatched()
        await self.telemetry.model_started(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            model=self.profile.name,
        )
        response = await self._structured_client.ainvoke(
            [("system", render_role_prompt(AgentRole.INCIDENT_COMMANDER)), ("user", prompt)]
        )
        raw_response = response.get("raw") if isinstance(response, dict) else None
        usage = _usage(raw_response)
        await self.telemetry.model_finished(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            model=self.profile.name,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            reasoning_tokens=usage["reasoning_tokens"],
            parser_outcome=(
                "structured_parse_succeeded"
                if isinstance(response, dict)
                and response.get("parsing_error") is None
                and isinstance(response.get("parsed"), CommanderSynthesis)
                else "structured_parse_failed"
            ),
            **provider_telemetry_from_message(raw_response),
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
        self.stats = self.stats.model_completed(
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
                "proposed_action_id": synthesis.proposed_mitigation_id.value,
                "proposed_action": synthesis.proposed_mitigation,
                "rationale": (
                    "The incident commander synthesized independently sourced specialist evidence."
                ),
                "evidence_ids": synthesis.evidence_ids,
            },
        )
        return ApprovalRequest.model_validate(
            structured_tool_payload(
                result,
                {"request_id", "proposed_action_id", "proposed_action", "status"},
            )
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
            structured_tool_payload(
                mitigation_result,
                {"request_id", "effective", "action_id", "action"},
            )
        )
        recovery = RecoveryCheckResult.model_validate(
            structured_tool_payload(recovery_result, {"case_id", "recovered", "checks"})
        )
        return mitigation, recovery

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        actor = AgentRole.INCIDENT_COMMANDER.value

        async def call() -> Any:
            self._called_tools.append(name)
            return await self._tools[name].ainvoke(arguments)

        async def attempt_started() -> None:
            self.stats = self.stats.tool_dispatched()
            await self.telemetry.tool_started(actor=actor, tool_name=name)

        async def attempt_finished(succeeded: bool) -> None:
            await self.telemetry.tool_finished(
                actor=actor,
                tool_name=name,
                succeeded=succeeded,
            )

        return await invoke_tool_with_policy(
            call,
            attempt_started=attempt_started,
            attempt_finished=attempt_finished,
        )


class _Usage(TypedDict):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None
    reasoning_tokens: int | None


def _usage(raw: Any) -> _Usage:
    if not isinstance(raw, AIMessage):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
    usage = raw.usage_metadata or {}
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cached_input_tokens": (
            int(input_details["cache_read"])
            if input_details.get("cache_read") is not None
            else None
        ),
        "reasoning_tokens": (
            int(output_details["reasoning"])
            if output_details.get("reasoning") is not None
            else None
        ),
    }


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
            "findings": [],
            "synthesis": None,
            "packet": None,
            "approval_request": None,
            "approval_decision": None,
            "action_id_to_execute": None,
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
            "findings": [],
            "synthesis": None,
            "packet": None,
            "approval_request": None,
            "approval_decision": None,
            "action_id_to_execute": None,
            "action_to_execute": None,
            "execution": None,
        },
        config,
    )
    if "__interrupt__" not in initial:
        packet_payload = initial.get("packet")
        if packet_payload is not None:
            packet = EvidencePacket.model_validate(packet_payload)
            if not packet.passed:
                raise EvidenceGateFailed(packet.gate_reasons)
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
        if (
            not response.approval_token
            or not response.action_id_to_execute
            or not response.action_to_execute
        ):
            raise ValueError("approved response requires an action ID, description, and token")
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
            proposed_mitigation_id=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
            proposed_mitigation="apply the reversible runbook action",
            recovery_checks=["verify affected service health"],
            evidence_sufficient=True,
        )

    graph = build_investigation_graph(investigators, synthesize)
    state = await graph.ainvoke(
        {
            "public_ticket": "deterministic probe",
            "findings": [],
            "synthesis": None,
            "packet": None,
            "approval_request": None,
            "approval_decision": None,
            "action_id_to_execute": None,
            "action_to_execute": None,
            "execution": None,
        }
    )
    return EvidencePacket.model_validate(state["packet"]).model_dump(mode="json")


async def _deterministic_approval_probe(
    decision: ApprovalDecision,
) -> dict[str, Any]:
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
            proposed_mitigation_id=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
            proposed_mitigation="apply the reversible runbook action",
            recovery_checks=["verify affected service health"],
            evidence_sufficient=True,
        )

    async def prepare(evidence: EvidencePacket) -> ApprovalRequest:
        return ApprovalRequest(
            request_id="mit-deterministic-01",
            case_id="case-deterministic",
            proposed_action_id=evidence.synthesis.proposed_mitigation_id,
            proposed_action=evidence.synthesis.proposed_mitigation,
            rationale="grounded deterministic evidence",
            evidence_ids=evidence.synthesis.evidence_ids,
            requested_by=AgentRole.INCIDENT_COMMANDER.value,
            created_tick=1,
        )

    async def execute(_request_id: str) -> tuple[MitigationResult, RecoveryCheckResult]:
        if decision is ApprovalDecision.REJECT:
            raise AssertionError("rejected approval must not reach mitigation")
        mitigation = MitigationResult(
            case_id="case-deterministic",
            request_id="mit-deterministic-01",
            world_tick=2,
            action_id=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
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
                "findings": [],
                "synthesis": None,
                "packet": None,
                "approval_request": None,
                "approval_decision": None,
                "action_id_to_execute": None,
                "action_to_execute": None,
                "execution": None,
            },
            config,
        )
        if "__interrupt__" not in initial:
            raise RuntimeError("deterministic LangGraph did not pause for approval")
        response: dict[str, str] = {
            "request_id": "mit-deterministic-01",
            "decision": decision.value,
        }
        if decision is not ApprovalDecision.REJECT:
            response["action_id_to_execute"] = (
                MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY.value
            )
            response["action_to_execute"] = "apply the reversible runbook action"
        resumed = await graph.ainvoke(
            Command(resume=response),
            config,
        )
        return WorkflowExecutionResult.model_validate(resumed["execution"]).model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--deterministic-probe", action="store_true")
    mode.add_argument("--deterministic-approved-probe", action="store_true")
    mode.add_argument("--deterministic-rejected-probe", action="store_true")
    mode.add_argument("--live-investigation", action="store_true")
    mode.add_argument("--live-approved-run", action="store_true")
    args = parser.parse_args()
    if args.deterministic_probe:
        payload = asyncio.run(_deterministic_probe())
    elif args.deterministic_approved_probe:
        payload = asyncio.run(_deterministic_approval_probe(ApprovalDecision.APPROVE))
    elif args.deterministic_rejected_probe:
        payload = asyncio.run(_deterministic_approval_probe(ApprovalDecision.REJECT))
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
