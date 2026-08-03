"""Native Deep Agents lane with role-scoped MCP subagents."""

from __future__ import annotations

import asyncio
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_lab.contracts import AgentRole, ApprovalDecision, ModelProfile
from agent_lab.harness_contracts import (
    HARNESS_DELEGATION_RECURSION_LIMIT,
    HARNESS_DELEGATION_REPAIR_TURNS,
    HARNESS_PHASE_MODEL_CALL_LIMIT,
    HARNESS_SPECIALIST_TOOL_RESPONSE_LIMIT,
    HARNESS_SYNTHESIS_RECURSION_LIMIT,
    HarnessApprovalResponse,
    HarnessExecutionResult,
    HarnessProposal,
    HarnessRoleObservation,
    active_harness_roles,
    evaluate_harness_proposal,
    harness_delegation_repair_prompt,
    harness_provenance_allowlist,
    harness_provenance_correction_prompt,
    harness_specialist_prompt,
    harness_specialist_roles,
    harness_synthesis_prompt,
    harness_task_prompt,
    require_harness_delegation,
    validate_harness_role_provenance,
)
from agent_lab.prompt_bundle import get_role_definition, render_role_prompt
from agent_lab.runner_host import ApprovalProvider
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.tool_catalog import tool_definitions_for_role
from agent_lab.workflow_contracts import evidence_ids_from_text, render_tool_result
from agent_lab.workflow_telemetry import WorkflowTelemetry
from langgraph_runner.chat_telemetry import InstrumentedChatOpenAI
from langgraph_runner.workflow_lane import LangGraphCommanderAgent


class _ToolTracker:
    def __init__(self, actor: str, telemetry: WorkflowTelemetry) -> None:
        self.actor = actor
        self.telemetry = telemetry
        self.tool_calls = 0
        self.called_tools: list[str] = []
        self.observed_evidence_ids: set[str] = set()
        self.delegation_observed = False

    def wrap(self, tool: BaseTool) -> BaseTool:
        async def invoke(**arguments: Any) -> Any:
            self.tool_calls += 1
            if not self.delegation_observed:
                await self.telemetry.handoff(
                    source_actor=AgentRole.INCIDENT_COMMANDER.value,
                    target_actor=self.actor,
                    phase="specialist_delegated",
                )
                self.delegation_observed = True
            await self.telemetry.tool_started(actor=self.actor, tool_name=tool.name)
            try:
                result = await tool.ainvoke(arguments)
            except Exception:
                await self.telemetry.tool_finished(
                    actor=self.actor,
                    tool_name=tool.name,
                    succeeded=False,
                )
                raise
            await self.telemetry.tool_finished(
                actor=self.actor,
                tool_name=tool.name,
                succeeded=True,
            )
            self.called_tools.append(tool.name)
            self.observed_evidence_ids.update(evidence_ids_from_text(render_tool_result(result)))
            return result

        return StructuredTool.from_function(
            coroutine=invoke,
            name=tool.name,
            description=tool.description or tool.name,
            args_schema=tool.args_schema,
        )


class _SubagentTaskTracker(AgentMiddleware):
    """Retain native task-tool executions until exceptional exits are reaped."""

    def __init__(self) -> None:
        super().__init__()
        self._active_tasks: set[asyncio.Task[Any]] = set()

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        if request.tool_call["name"] != "task":
            return await handler(request)
        task = asyncio.current_task()
        if task is None:
            return await handler(request)
        self._active_tasks.add(task)
        try:
            return await handler(request)
        finally:
            self._active_tasks.discard(task)

    async def cancel_and_drain(self) -> None:
        """Cancel and reap task-tool executions left by parallel failure."""

        await asyncio.sleep(0)
        while self._active_tasks:
            tasks = tuple(self._active_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class _SpecialistCompletionMiddleware(AgentMiddleware):
    """Mechanically remove specialist tools after the frozen evidence phase."""

    def __init__(self) -> None:
        super().__init__()
        self._dispatched_responses = 0

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        tools_enabled = self._dispatched_responses < HARNESS_SPECIALIST_TOOL_RESPONSE_LIMIT
        self._dispatched_responses += 1
        if not tools_enabled:
            request = request.override(tools=[], tool_choice="none")
        return await handler(request)


def _chat_model(
    profile: ModelProfile,
    *,
    actor: str,
    telemetry: WorkflowTelemetry,
) -> InstrumentedChatOpenAI:
    return InstrumentedChatOpenAI(
        actor=actor,
        profile=profile,
        telemetry=telemetry,
        framework="deep-agents",
    )


def _phase_model_call_limit_middleware() -> ModelCallLimitMiddleware:
    """Return a fresh native limiter for one harness actor phase."""

    return ModelCallLimitMiddleware(
        run_limit=HARNESS_PHASE_MODEL_CALL_LIMIT,
        exit_behavior="error",
    )


async def _role_tools(
    endpoint: RoleEndpoint,
    role: AgentRole,
    tracker: _ToolTracker,
) -> list[BaseTool]:
    client = MultiServerMCPClient(
        {
            "incident": {
                "transport": "streamable_http",
                "url": endpoint.url,
                "headers": {"Authorization": f"Bearer {endpoint.token}"},
            }
        }
    )
    tools = await client.get_tools(server_name="incident")
    expected = {tool.name for tool in tool_definitions_for_role(role)}
    observed = {tool.name for tool in tools}
    if observed != expected:
        raise RuntimeError(
            f"{role.value} MCP catalog mismatch: "
            f"expected={sorted(expected)}, observed={sorted(observed)}"
        )
    return [tracker.wrap(tool) for tool in tools]


def _role_observations(
    trackers: dict[AgentRole, _ToolTracker],
) -> dict[AgentRole, HarnessRoleObservation]:
    return {
        role: HarnessRoleObservation(
            called_tools=frozenset(tracker.called_tools),
            evidence_ids=frozenset(tracker.observed_evidence_ids),
        )
        for role, tracker in trackers.items()
    }


async def run_live_approved(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, Any]:
    """Run native Deep Agents delegation, then shared approval and recovery."""

    configured_roles = harness_specialist_roles(request.harness_ablation)
    required_roles = (
        *sorted(configured_roles, key=lambda item: item.value),
        AgentRole.INCIDENT_COMMANDER,
    )
    missing = [role.value for role in required_roles if role not in request.role_endpoints]
    if missing:
        raise ValueError(f"run request is missing harness endpoints: {missing}")
    workspace = request.paths.workspace
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.chmod(0o700)

    register_harness_profile(
        "openai",
        HarnessProfile(
            excluded_tools=frozenset({"execute"}),
            excluded_middleware=(
                frozenset({"SummarizationMiddleware"})
                if request.harness_ablation.context_compaction == "disabled"
                else frozenset()
            ),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    trackers: dict[AgentRole, _ToolTracker] = {}
    models: dict[AgentRole, InstrumentedChatOpenAI] = {}
    subagents: list[SubAgent] = []
    for role in sorted(configured_roles, key=lambda item: item.value):
        tracker = _ToolTracker(role.value, telemetry)
        model = _chat_model(
            request.manifest.model,
            actor=role.value,
            telemetry=telemetry,
        )
        trackers[role] = tracker
        models[role] = model
        subagents.append(
            SubAgent(
                name=role.value,
                description=get_role_definition(role).responsibility,
                system_prompt=harness_specialist_prompt(
                    role,
                    request.harness_ablation.prompt_variant,
                ),
                tools=await _role_tools(request.role_endpoints[role], role, tracker),
                model=model,
                middleware=[
                    _phase_model_call_limit_middleware(),
                    _SpecialistCompletionMiddleware(),
                ],
            )
        )

    commander_model = _chat_model(
        request.manifest.model,
        actor=AgentRole.INCIDENT_COMMANDER.value,
        telemetry=telemetry,
    )
    backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    subagent_task_tracker = _SubagentTaskTracker()
    delegation_graph = create_deep_agent(
        model=commander_model,
        system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
        middleware=[_phase_model_call_limit_middleware(), subagent_task_tracker],
        subagents=subagents,
        backend=backend,
        name=AgentRole.INCIDENT_COMMANDER.value,
    )
    try:
        state = await delegation_graph.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": harness_task_prompt(
                            request.public_ticket,
                            request.harness_ablation.prompt_variant,
                            request.harness_ablation.reviewer_enabled,
                        ),
                    }
                ]
            },
            config={"recursion_limit": HARNESS_DELEGATION_RECURSION_LIMIT},
        )
        for _attempt in range(HARNESS_DELEGATION_REPAIR_TURNS):
            observations = _role_observations(trackers)
            observed_roles = active_harness_roles(observations)
            if len(observed_roles) >= 2:
                break
            missing_roles = sorted(role.value for role in configured_roles - observed_roles)
            state = await delegation_graph.ainvoke(
                {
                    "messages": [
                        *state["messages"],
                        {
                            "role": "user",
                            "content": harness_delegation_repair_prompt(missing_roles),
                        },
                    ]
                },
                config={"recursion_limit": HARNESS_DELEGATION_RECURSION_LIMIT},
            )
    finally:
        await subagent_task_tracker.cancel_and_drain()
    observations = _role_observations(trackers)
    require_harness_delegation(observations)

    evidence_allowlist = harness_provenance_allowlist(observations)
    synthesis_graph = create_deep_agent(
        model=commander_model,
        system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
        middleware=[_phase_model_call_limit_middleware()],
        backend=backend,
        response_format=ProviderStrategy(HarnessProposal),
        name=AgentRole.INCIDENT_COMMANDER.value,
    )
    synthesis_state = await synthesis_graph.ainvoke(
        {
            "messages": [
                *state["messages"],
                {
                    "role": "user",
                    "content": harness_synthesis_prompt(evidence_allowlist),
                },
            ]
        },
        config={"recursion_limit": HARNESS_SYNTHESIS_RECURSION_LIMIT},
    )
    proposal = HarnessProposal.model_validate(synthesis_state.get("structured_response"))
    correction_attempt = 0
    while True:
        try:
            validate_harness_role_provenance(proposal, observations)
            break
        except ValueError as exc:
            if correction_attempt >= request.harness_ablation.provenance_correction_turns:
                raise
            correction_attempt += 1
            synthesis_state = await synthesis_graph.ainvoke(
                {
                    "messages": [
                        *synthesis_state["messages"],
                        {
                            "role": "user",
                            "content": harness_provenance_correction_prompt(
                                exc,
                                evidence_allowlist,
                            ),
                        },
                    ]
                },
                config={"recursion_limit": HARNESS_SYNTHESIS_RECURSION_LIMIT},
            )
            proposal = HarnessProposal.model_validate(synthesis_state.get("structured_response"))
    for finding in proposal.findings:
        await telemetry.handoff(
            source_actor=finding.role.value,
            target_actor=AgentRole.INCIDENT_COMMANDER.value,
            phase="specialist_finding_returned",
        )
    evidence = evaluate_harness_proposal(proposal, attempt=correction_attempt)
    if not evidence.passed:
        raise ValueError(f"harness evidence gate failed: {evidence.gate_reasons}")

    command_agent = await LangGraphCommanderAgent(
        request.role_endpoints[AgentRole.INCIDENT_COMMANDER],
        request.manifest.model,
        telemetry,
    ).initialize()
    approval_request = await command_agent.prepare_mitigation(evidence)
    approval = HarnessApprovalResponse.model_validate(await approval_provider(approval_request))
    if approval.request_id != approval_request.request_id:
        raise ValueError("approval response request ID differs from the pending request")
    if approval.decision is ApprovalDecision.REJECT:
        execution = HarnessExecutionResult(
            status="approval_rejected",
            evidence=evidence,
            approval_request=approval_request,
            approval_decision=approval.decision,
        )
    else:
        if (
            not approval.approval_token
            or not approval.action_id_to_execute
            or not approval.action_to_execute
        ):
            raise ValueError("approved response requires an action ID, description, and token")
        mitigation, recovery = await command_agent.apply_and_verify(
            approval.request_id,
            approval.approval_token,
        )
        execution = HarnessExecutionResult(
            status="completed",
            evidence=evidence,
            approval_request=approval_request,
            approval_decision=approval.decision,
            action_id_to_execute=approval.action_id_to_execute,
            action_to_execute=approval.action_to_execute,
            mitigation=mitigation,
            recovery=recovery,
        )

    return {
        "framework": "deep_agents",
        "model": request.manifest.model.name,
        "execution": execution.model_dump(mode="json"),
        "role_stats": {
            **{
                role.value: {
                    "model_calls": models[role].lab_stats.model_calls,
                    "tool_calls": trackers[role].tool_calls,
                    "input_tokens": models[role].lab_stats.input_tokens,
                    "output_tokens": models[role].lab_stats.output_tokens,
                }
                for role in sorted(configured_roles, key=lambda item: item.value)
            },
            AgentRole.INCIDENT_COMMANDER.value: {
                "model_calls": commander_model.lab_stats.model_calls,
                "tool_calls": command_agent.stats.tool_calls,
                "input_tokens": commander_model.lab_stats.input_tokens,
                "output_tokens": commander_model.lab_stats.output_tokens,
            },
        },
    }
