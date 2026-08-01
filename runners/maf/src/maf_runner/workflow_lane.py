"""Native MAF fan-out investigation and deterministic evidence join."""

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from agent_framework import (
    Agent,
    Executor,
    FileCheckpointStorage,
    MCPStreamableHTTPTool,
    Workflow,
    WorkflowBuilder,
    WorkflowCheckpoint,
    WorkflowContext,
    handler,
    response_handler,
)
from agent_framework.openai import OpenAIChatCompletionClient
from httpx import AsyncClient, Timeout
from openai import AsyncOpenAI
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


class SynthesisPacket(LaneModel):
    findings: list[SpecialistFinding]
    synthesis: CommanderSynthesis
    attempt: int = Field(default=0, ge=0, le=1)


class EvidencePacket(LaneModel):
    passed: bool
    findings: list[SpecialistFinding]
    synthesis: CommanderSynthesis
    evidence_ids: list[str]
    independent_roles: list[AgentRole]
    gate_reasons: list[str]
    attempt: int = Field(ge=0, le=1)


class ApprovalEnvelope(LaneModel):
    evidence: EvidencePacket
    request: ApprovalRequest


class AuthorizedEnvelope(LaneModel):
    evidence: EvidencePacket
    request: ApprovalRequest
    decision: ApprovalDecision
    action_to_execute: str


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
    def validate_terminal_state(self) -> "WorkflowExecutionResult":
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


class _TrackingMCPStreamableHTTPTool(MCPStreamableHTTPTool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.called_tools: list[str] = []

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        self.called_tools.append(tool_name)
        return await super().call_tool(tool_name, **kwargs)


class _OwnerOnlyCheckpointStorage(FileCheckpointStorage):
    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        checkpoint_id = await super().save(checkpoint)
        (self.storage_path / f"{checkpoint_id}.json").chmod(0o600)
        return checkpoint_id


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


class _Commander(Executor):
    def __init__(self, commander: Commander) -> None:
        super().__init__(AgentRole.INCIDENT_COMMANDER.value)
        self._commander = commander

    @handler
    async def synthesize(
        self,
        payloads: list[dict[str, Any]],
        ctx: WorkflowContext[dict[str, Any]],
    ) -> None:
        findings = sorted(
            (SpecialistFinding.model_validate(payload) for payload in payloads),
            key=lambda item: item.role.value,
        )
        synthesis = await self._commander(findings)
        await ctx.send_message(
            SynthesisPacket(findings=findings, synthesis=synthesis).model_dump(mode="json")
        )


class _EvidenceGate(Executor):
    def __init__(self, *, continue_on_pass: bool = False) -> None:
        super().__init__("evidence_gate")
        self._continue_on_pass = continue_on_pass

    @handler
    async def evaluate(
        self,
        payload: dict[str, Any],
        ctx: WorkflowContext[dict[str, Any], dict[str, Any]],
    ) -> None:
        synthesis_packet = SynthesisPacket.model_validate(payload)
        findings = synthesis_packet.findings
        synthesis = synthesis_packet.synthesis
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
            attempt=synthesis_packet.attempt,
        )
        if self._continue_on_pass and packet.passed:
            await ctx.send_message(packet.model_dump(mode="json"))
        else:
            await ctx.yield_output(packet.model_dump(mode="json"))


class _PrepareMitigation(Executor):
    def __init__(self, preparer: MitigationPreparer) -> None:
        super().__init__("prepare_mitigation")
        self._preparer = preparer

    @handler
    async def prepare(
        self,
        payload: dict[str, Any],
        ctx: WorkflowContext[dict[str, Any]],
    ) -> None:
        evidence = EvidencePacket.model_validate(payload)
        request = await self._preparer(evidence)
        await ctx.send_message(
            ApprovalEnvelope(evidence=evidence, request=request).model_dump(mode="json")
        )


class _Approval(Executor):
    def __init__(self) -> None:
        super().__init__("approval")

    @handler
    async def request_approval(
        self,
        payload: dict[str, Any],
        ctx: WorkflowContext[dict, dict],
    ) -> None:
        envelope = ApprovalEnvelope.model_validate(payload)
        await ctx.request_info(
            envelope.model_dump(mode="json"),
            dict,
            request_id=envelope.request.request_id,
        )

    @response_handler(
        request=dict,
        response=dict,
        output=dict[str, Any],
        workflow_output=dict[str, Any],
    )
    async def receive_decision(
        self,
        original_request: dict[str, Any],
        response: dict[str, Any],
        ctx: WorkflowContext,
    ) -> None:
        envelope = ApprovalEnvelope.model_validate(original_request)
        decision = ApprovalResponse.model_validate(response)
        if decision.request_id != envelope.request.request_id:
            raise ValueError("approval response request ID differs from the pending request")
        if decision.decision is ApprovalDecision.REJECT:
            await ctx.yield_output(
                WorkflowExecutionResult(
                    status="approval_rejected",
                    evidence=envelope.evidence,
                    approval_request=envelope.request,
                    approval_decision=decision.decision,
                ).model_dump(mode="json")
            )
            return
        if not decision.action_to_execute:
            raise ValueError("approved mitigation requires the authorized action")
        await ctx.send_message(
            AuthorizedEnvelope(
                evidence=envelope.evidence,
                request=envelope.request,
                decision=decision.decision,
                action_to_execute=decision.action_to_execute,
            ).model_dump(mode="json")
        )


class _MitigateAndVerify(Executor):
    def __init__(self, executor: MitigationExecutor) -> None:
        super().__init__("mitigation_and_recovery")
        self._executor = executor

    @handler
    async def mitigate(
        self,
        payload: dict[str, Any],
        ctx: WorkflowContext[Any, dict[str, Any]],
    ) -> None:
        envelope = AuthorizedEnvelope.model_validate(payload)
        mitigation, recovery = await self._executor(envelope.request.request_id)
        await ctx.yield_output(
            WorkflowExecutionResult(
                status="completed",
                evidence=envelope.evidence,
                approval_request=envelope.request,
                approval_decision=envelope.decision,
                action_to_execute=envelope.action_to_execute,
                mitigation=mitigation,
                recovery=recovery,
            ).model_dump(mode="json")
        )


def build_investigation_workflow(
    investigators: dict[AgentRole, Investigator], commander: Commander
) -> Workflow:
    """Build the native parallel investigation, commander synthesis, and evidence gate."""

    if set(investigators) != set(SPECIALIST_ROLES):
        missing = sorted(role.value for role in set(SPECIALIST_ROLES) - set(investigators))
        extra = sorted(role.value for role in set(investigators) - set(SPECIALIST_ROLES))
        raise ValueError(f"investigator role mismatch: missing={missing}, extra={extra}")
    dispatcher = _Dispatcher(id="investigation_dispatch")
    specialists = [_Specialist(role, investigators[role]) for role in SPECIALIST_ROLES]
    commander_node = _Commander(commander)
    gate = _EvidenceGate()
    return (
        WorkflowBuilder(
            start_executor=dispatcher,
            name="agent-lab-maf-explicit-investigation-v1",
            max_iterations=20,
            output_from=[gate],
        )
        .add_fan_out_edges(dispatcher, specialists)
        .add_fan_in_edges(specialists, commander_node)
        .add_edge(commander_node, gate)
        .build()
    )


def build_approved_workflow(
    investigators: dict[AgentRole, Investigator],
    commander: Commander,
    preparer: MitigationPreparer,
    executor: MitigationExecutor,
    checkpoint_storage: FileCheckpointStorage,
) -> Workflow:
    """Build the full native workflow through approval, mitigation, and recovery."""

    if set(investigators) != set(SPECIALIST_ROLES):
        missing = sorted(role.value for role in set(SPECIALIST_ROLES) - set(investigators))
        extra = sorted(role.value for role in set(investigators) - set(SPECIALIST_ROLES))
        raise ValueError(f"investigator role mismatch: missing={missing}, extra={extra}")
    dispatcher = _Dispatcher(id="investigation_dispatch")
    specialists = [_Specialist(role, investigators[role]) for role in SPECIALIST_ROLES]
    commander_node = _Commander(commander)
    gate = _EvidenceGate(continue_on_pass=True)
    prepare = _PrepareMitigation(preparer)
    approval = _Approval()
    mitigation = _MitigateAndVerify(executor)
    return (
        WorkflowBuilder(
            start_executor=dispatcher,
            name="agent-lab-maf-explicit-approved-v1",
            max_iterations=30,
            checkpoint_storage=checkpoint_storage,
            output_from=[gate, approval, mitigation],
        )
        .add_fan_out_edges(dispatcher, specialists)
        .add_fan_in_edges(specialists, commander_node)
        .add_edge(commander_node, gate)
        .add_edge(gate, prepare)
        .add_edge(prepare, approval)
        .add_edge(approval, mitigation)
        .build()
    )


class MafRoleAgent:
    """One MAF agent bound to exactly one role endpoint and shared model profile."""

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

    async def __aenter__(self) -> "MafRoleAgent":
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
            result = await self._call_tool(tool_name, **arguments)
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
        usage = response.usage_details or {}
        await self.telemetry.model_finished(
            actor=self.role.value,
            model=self.profile.name,
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
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
        self.stats = RoleRunStats(
            model_calls=sum(
                1 for message in response.messages if getattr(message, "role", None) == "assistant"
            ),
            tool_calls=len(tool_names),
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
        )
        return finding

    async def _call_tool(self, tool_name: str, **arguments: Any) -> Any:
        await self.telemetry.tool_started(actor=self.role.value, tool_name=tool_name)
        try:
            result = await self._mcp.call_tool(tool_name, **arguments)
        except Exception:
            await self.telemetry.tool_finished(
                actor=self.role.value,
                tool_name=tool_name,
                succeeded=False,
            )
            raise
        await self.telemetry.tool_finished(
            actor=self.role.value,
            tool_name=tool_name,
            succeeded=True,
        )
        return result


class MafCommanderAgent:
    """MAF incident commander that synthesizes specialist evidence without MCP mutation."""

    def __init__(
        self,
        endpoint: RoleEndpoint,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry = NOOP_WORKFLOW_TELEMETRY,
    ) -> None:
        if endpoint.role is not AgentRole.INCIDENT_COMMANDER:
            raise ValueError("commander endpoint does not match the incident commander")
        self.profile = profile
        self.telemetry = telemetry
        self.stats = RoleRunStats(model_calls=0, tool_calls=0, input_tokens=0, output_tokens=0)
        self._stack = AsyncExitStack()
        sdk = AsyncOpenAI(
            api_key="agent-lab-local",
            base_url=profile.base_url,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            default_headers={"X-Agent-Lab-Framework": "maf"},
        )
        client = OpenAIChatCompletionClient(model=profile.name, async_client=sdk)
        self.agent = Agent(
            client,
            name=AgentRole.INCIDENT_COMMANDER.value,
            instructions=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
            tools=[],
        )
        self._mcp_http_client = AsyncClient(
            headers={"Authorization": f"Bearer {endpoint.token}"},
            follow_redirects=False,
            timeout=Timeout(profile.request_timeout_seconds),
        )
        self._mcp = _TrackingMCPStreamableHTTPTool(
            name="incident-commander",
            url=endpoint.url,
            allowed_tools=[
                tool.name for tool in tool_definitions_for_role(AgentRole.INCIDENT_COMMANDER)
            ],
            load_prompts=False,
            request_timeout=int(profile.request_timeout_seconds),
            approval_mode="never_require",
            http_client=self._mcp_http_client,
        )

    async def __aenter__(self) -> "MafCommanderAgent":
        await self._stack.enter_async_context(self._mcp_http_client)
        await self._stack.enter_async_context(self._mcp)
        await self._stack.enter_async_context(self.agent)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.__aexit__(*exc_info)

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
        response = await self.agent.run(
            prompt,
            options={
                "temperature": self.profile.temperature,
                "top_p": self.profile.top_p,
                "presence_penalty": self.profile.presence_penalty,
                "max_tokens": min(self.profile.max_output_tokens, 2048),
                "seed": self.profile.seed,
                "response_format": CommanderSynthesis,
                "tool_choice": "none",
            },
        )
        usage = response.usage_details or {}
        await self.telemetry.model_finished(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            model=self.profile.name,
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
        )
        if not isinstance(response.value, CommanderSynthesis):
            raise TypeError("incident commander did not return a CommanderSynthesis")
        self.stats = RoleRunStats(
            model_calls=sum(
                1 for message in response.messages if getattr(message, "role", None) == "assistant"
            ),
            tool_calls=0,
            input_tokens=int(usage.get("input_token_count", 0)),
            output_tokens=int(usage.get("output_token_count", 0)),
        )
        return response.value

    async def prepare_mitigation(self, evidence: EvidencePacket) -> ApprovalRequest:
        synthesis = evidence.synthesis
        await self._call_tool(
            "submit_hypothesis",
            root_cause=synthesis.root_cause,
            evidence_ids=synthesis.evidence_ids,
            mitigation=synthesis.proposed_mitigation,
        )
        result = await self._call_tool(
            "request_mitigation",
            proposed_action=synthesis.proposed_mitigation,
            rationale=(
                "The incident commander synthesized independently sourced specialist evidence."
            ),
            evidence_ids=synthesis.evidence_ids,
        )
        self._update_tool_stats()
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
            request_id=request_id,
            approval_token=approval_token,
        )
        recovery_result = await self._call_tool(
            "verify_recovery",
            services=["checkout-api"],
        )
        self._update_tool_stats()
        mitigation = MitigationResult.model_validate(
            _structured_payload(mitigation_result, {"request_id", "effective", "action"})
        )
        recovery = RecoveryCheckResult.model_validate(
            _structured_payload(recovery_result, {"case_id", "recovered", "checks"})
        )
        return mitigation, recovery

    def _update_tool_stats(self) -> None:
        self.stats = self.stats.model_copy(update={"tool_calls": len(self._mcp.called_tools)})

    async def _call_tool(self, tool_name: str, **arguments: Any) -> Any:
        actor = AgentRole.INCIDENT_COMMANDER.value
        await self.telemetry.tool_started(actor=actor, tool_name=tool_name)
        try:
            result = await self._mcp.call_tool(tool_name, **arguments)
        except Exception:
            await self.telemetry.tool_finished(
                actor=actor,
                tool_name=tool_name,
                succeeded=False,
            )
            raise
        await self.telemetry.tool_finished(
            actor=actor,
            tool_name=tool_name,
            succeeded=True,
        )
        return result


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
    async with AsyncExitStack() as stack:
        agents: dict[AgentRole, MafRoleAgent] = {}
        for role in SPECIALIST_ROLES:
            agents[role] = await stack.enter_async_context(
                MafRoleAgent(role, request.role_endpoints[role], request.manifest.model)
            )
        commander_agent = await stack.enter_async_context(
            MafCommanderAgent(
                request.role_endpoints[AgentRole.INCIDENT_COMMANDER], request.manifest.model
            )
        )
        workflow = build_investigation_workflow(
            {role: agent.investigate for role, agent in agents.items()},
            commander_agent.synthesize,
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
                **{
                    role.value: agent.stats.model_dump(mode="json")
                    for role, agent in agents.items()
                },
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
    request.paths.checkpoints.mkdir(parents=True, exist_ok=True, mode=0o700)
    request.paths.checkpoints.chmod(0o700)
    storage = _OwnerOnlyCheckpointStorage(request.paths.checkpoints)
    approval_tokens: dict[str, str] = {}

    async with AsyncExitStack() as stack:
        agents: dict[AgentRole, MafRoleAgent] = {}
        for role in SPECIALIST_ROLES:
            agents[role] = await stack.enter_async_context(
                MafRoleAgent(
                    role,
                    request.role_endpoints[role],
                    request.manifest.model,
                    telemetry,
                )
            )
        commander_agent = await stack.enter_async_context(
            MafCommanderAgent(
                request.role_endpoints[AgentRole.INCIDENT_COMMANDER],
                request.manifest.model,
                telemetry,
            )
        )

        async def execute(request_id: str) -> tuple[MitigationResult, RecoveryCheckResult]:
            token = approval_tokens.pop(request_id, None)
            if token is None:
                raise RuntimeError("approved mitigation token was not available in process memory")
            return await commander_agent.apply_and_verify(request_id, token)

        workflow = build_approved_workflow(
            {role: agent.investigate for role, agent in agents.items()},
            commander_agent.synthesize,
            commander_agent.prepare_mitigation,
            execute,
            storage,
        )
        initial = await workflow.run(
            InvestigationTask(public_ticket=request.public_ticket).model_dump(mode="json")
        )
        requests = initial.get_request_info_events()
        latest = await storage.get_latest(workflow_name="agent-lab-maf-explicit-approved-v1")
        if len(requests) != 1 or latest is None:
            raise RuntimeError("MAF workflow did not persist exactly one approval request")
        approval_event = requests[0]
        envelope = ApprovalEnvelope.model_validate(approval_event.data)
        if approval_provider is None:
            _emit_json_line(
                {
                    "kind": "approval_requested",
                    "request": envelope.request.model_dump(mode="json"),
                }
            )
            response_line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not response_line:
                raise RuntimeError("approval response stream closed before a decision")
            response = ApprovalResponse.model_validate_json(response_line)
        else:
            response = ApprovalResponse.model_validate(await approval_provider(envelope.request))
        if response.request_id != envelope.request.request_id:
            raise ValueError("approval response request ID differs from the pending request")
        if response.decision in {ApprovalDecision.APPROVE, ApprovalDecision.EDIT}:
            if not response.approval_token or not response.action_to_execute:
                raise ValueError("approved response requires an action and an approval token")
            approval_tokens[response.request_id] = response.approval_token
        resumed = await workflow.run(
            checkpoint_id=latest.checkpoint_id,
            checkpoint_storage=storage,
            responses={response.request_id: response.model_dump(mode="json")},
        )
        outputs = [
            WorkflowExecutionResult.model_validate(output)
            for output in resumed.get_outputs()
            if isinstance(output, dict) and "status" in output
        ]
        if len(outputs) != 1:
            raise RuntimeError(f"MAF approved workflow produced {len(outputs)} terminal outputs")
        return {
            "framework": "maf_workflow",
            "model": request.manifest.model.name,
            "execution": outputs[0].model_dump(mode="json"),
            "role_stats": {
                **{
                    role.value: agent.stats.model_dump(mode="json")
                    for role, agent in agents.items()
                },
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

    result = await build_investigation_workflow(investigators, synthesize).run(
        InvestigationTask(public_ticket="deterministic probe").model_dump(mode="json")
    )
    outputs = [EvidencePacket.model_validate(output) for output in result.get_outputs()]
    return outputs[0].model_dump(mode="json")


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

    with tempfile.TemporaryDirectory(prefix="agent-lab-maf-approved-probe-") as raw_path:
        storage = _OwnerOnlyCheckpointStorage(Path(raw_path))
        workflow = build_approved_workflow(
            investigators,
            synthesize,
            prepare,
            execute,
            storage,
        )
        initial = await workflow.run(
            InvestigationTask(public_ticket="deterministic probe").model_dump(mode="json")
        )
        requests = initial.get_request_info_events()
        latest = await storage.get_latest(workflow_name="agent-lab-maf-explicit-approved-v1")
        if len(requests) != 1 or latest is None:
            raise RuntimeError("deterministic MAF workflow did not pause for approval")
        request_id = requests[0].request_id
        resumed = await workflow.run(
            checkpoint_id=latest.checkpoint_id,
            checkpoint_storage=storage,
            responses={
                request_id: {
                    "request_id": request_id,
                    "decision": ApprovalDecision.APPROVE.value,
                    "action_to_execute": "apply the reversible runbook action",
                }
            },
        )
        outputs = [
            WorkflowExecutionResult.model_validate(output)
            for output in resumed.get_outputs()
            if isinstance(output, dict) and "status" in output
        ]
        if len(outputs) != 1:
            raise RuntimeError("deterministic MAF workflow did not complete after approval")
        return outputs[0].model_dump(mode="json")


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
