"""Native LangChain create-agent implementation of the single-agent baseline."""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent_lab.baseline_contracts import (
    BASELINE_DISCOVERY_REPAIR_TURNS,
    BASELINE_PHASE_MODEL_CALL_LIMIT,
    BASELINE_PROVENANCE_CORRECTION_TURNS,
    BASELINE_RECURSION_LIMIT,
    BASELINE_SOURCE_ROLES,
    BaselineProposal,
    BaselineSourceObservation,
    active_baseline_sources,
    baseline_discovery_repair_prompt,
    baseline_evidence_allowlist,
    baseline_provenance_correction_prompt,
    baseline_synthesis_prompt,
    baseline_task_prompt,
    baseline_tool_names_for_source,
    evaluate_baseline_proposal,
    require_baseline_discovery,
    validate_baseline_provenance,
)
from agent_lab.contracts import AgentRole, ApprovalDecision
from agent_lab.prompt_bundle import render_role_prompt
from agent_lab.runner_host import ApprovalProvider
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.workflow_contracts import (
    ApprovalResponse,
    WorkflowExecutionResult,
    evidence_ids_from_text,
    render_tool_result,
)
from agent_lab.workflow_telemetry import WorkflowTelemetry
from langgraph_runner.chat_telemetry import InstrumentedChatOpenAI
from langgraph_runner.workflow_lane import LangGraphCommanderAgent


class _SourceTracker:
    def __init__(
        self,
        source_role: AgentRole,
        telemetry: WorkflowTelemetry,
    ) -> None:
        self.source_role = source_role
        self.telemetry = telemetry
        self.tool_calls = 0
        self.called_tools: list[str] = []
        self.evidence_ids: set[str] = set()

    def wrap(self, tool: BaseTool) -> BaseTool:
        async def invoke(**arguments: Any) -> Any:
            actor = AgentRole.INCIDENT_COMMANDER.value
            self.tool_calls += 1
            await self.telemetry.tool_started(actor=actor, tool_name=tool.name)
            try:
                result = await tool.ainvoke(arguments)
            except Exception:
                await self.telemetry.tool_finished(
                    actor=actor,
                    tool_name=tool.name,
                    succeeded=False,
                )
                raise
            await self.telemetry.tool_finished(
                actor=actor,
                tool_name=tool.name,
                succeeded=True,
            )
            self.called_tools.append(tool.name)
            self.evidence_ids.update(evidence_ids_from_text(render_tool_result(result)))
            return result

        return StructuredTool.from_function(
            coroutine=invoke,
            name=tool.name,
            description=tool.description or tool.name,
            args_schema=tool.args_schema,
        )


async def _source_tools(
    endpoint: RoleEndpoint,
    role: AgentRole,
    tracker: _SourceTracker,
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
    available = {tool.name: tool for tool in await client.get_tools(server_name="incident")}
    expected = set(baseline_tool_names_for_source(role))
    missing = expected - set(available)
    if missing:
        raise RuntimeError(f"{role.value} baseline MCP catalog is missing tools: {sorted(missing)}")
    return [tracker.wrap(available[name]) for name in sorted(expected)]


def _observations(
    trackers: dict[AgentRole, _SourceTracker],
) -> dict[AgentRole, BaselineSourceObservation]:
    return {
        role: BaselineSourceObservation(
            called_tools=frozenset(tracker.called_tools),
            evidence_ids=frozenset(tracker.evidence_ids),
        )
        for role, tracker in trackers.items()
    }


async def run_live_approved(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, Any]:
    """Run one model-driven agent, then the shared approval/recovery boundary."""

    required_roles = (*BASELINE_SOURCE_ROLES, AgentRole.INCIDENT_COMMANDER)
    missing = [role.value for role in required_roles if role not in request.role_endpoints]
    if missing:
        raise ValueError(f"run request is missing single-agent endpoints: {missing}")

    trackers: dict[AgentRole, _SourceTracker] = {}
    tools: list[BaseTool] = []
    for role in BASELINE_SOURCE_ROLES:
        tracker = _SourceTracker(role, telemetry)
        trackers[role] = tracker
        tools.extend(
            await _source_tools(
                request.role_endpoints[role],
                role,
                tracker,
            )
        )
    if len({tool.name for tool in tools}) != len(tools):
        raise RuntimeError("single-agent baseline tool union contains duplicate names")

    model = InstrumentedChatOpenAI(
        actor=AgentRole.INCIDENT_COMMANDER.value,
        profile=request.manifest.model,
        telemetry=telemetry,
        framework="langchain-single-agent",
    )
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=BASELINE_PHASE_MODEL_CALL_LIMIT,
                exit_behavior="end",
            )
        ],
        name="langchain_single_agent",
    )
    state = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": baseline_task_prompt(request.public_ticket),
                }
            ]
        },
        config={"recursion_limit": BASELINE_RECURSION_LIMIT},
    )
    discovery_attempt = 0
    for _ in range(BASELINE_DISCOVERY_REPAIR_TURNS):
        observations = _observations(trackers)
        active_sources = active_baseline_sources(observations)
        if len(active_sources) >= 2:
            break
        discovery_attempt += 1
        missing_roles = sorted(role.value for role in set(BASELINE_SOURCE_ROLES) - active_sources)
        state = await agent.ainvoke(
            {
                "messages": [
                    *state["messages"],
                    {
                        "role": "user",
                        "content": baseline_discovery_repair_prompt(missing_roles),
                    },
                ]
            },
            config={"recursion_limit": BASELINE_RECURSION_LIMIT},
        )
    observations = _observations(trackers)
    require_baseline_discovery(observations)
    synthesis_agent = create_agent(
        model=model,
        tools=[],
        system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
        middleware=[
            ModelCallLimitMiddleware(
                run_limit=BASELINE_PHASE_MODEL_CALL_LIMIT,
                exit_behavior="error",
            )
        ],
        response_format=ProviderStrategy(BaselineProposal),
        name="langchain_single_agent_synthesis",
    )
    state = await synthesis_agent.ainvoke(
        {
            "messages": [
                *state["messages"],
                {
                    "role": "user",
                    "content": baseline_synthesis_prompt(baseline_evidence_allowlist(observations)),
                },
            ]
        },
        config={"recursion_limit": BASELINE_RECURSION_LIMIT},
    )
    proposal = BaselineProposal.model_validate(state.get("structured_response"))
    correction_attempt = 0
    while True:
        observations = _observations(trackers)
        try:
            validate_baseline_provenance(proposal, observations)
            break
        except ValueError as exc:
            if correction_attempt >= BASELINE_PROVENANCE_CORRECTION_TURNS:
                raise
            correction_attempt += 1
            correction_agent = create_agent(
                model=model,
                tools=[],
                system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
                middleware=[
                    ModelCallLimitMiddleware(
                        run_limit=BASELINE_PHASE_MODEL_CALL_LIMIT,
                        exit_behavior="error",
                    )
                ],
                response_format=ProviderStrategy(BaselineProposal),
                name="langchain_single_agent_provenance_correction",
            )
            state = await correction_agent.ainvoke(
                {
                    "messages": [
                        *state["messages"],
                        {
                            "role": "user",
                            "content": baseline_provenance_correction_prompt(
                                exc,
                                baseline_evidence_allowlist(observations),
                            ),
                        },
                    ]
                },
                config={"recursion_limit": BASELINE_RECURSION_LIMIT},
            )
            proposal = BaselineProposal.model_validate(state.get("structured_response"))

    evidence = evaluate_baseline_proposal(proposal)
    if not evidence.passed:
        raise ValueError(f"single-agent evidence gate failed: {evidence.gate_reasons}")
    command_agent = await LangGraphCommanderAgent(
        request.role_endpoints[AgentRole.INCIDENT_COMMANDER],
        request.manifest.model,
        telemetry,
    ).initialize()
    approval_request = await command_agent.prepare_mitigation(evidence)
    approval = ApprovalResponse.model_validate(await approval_provider(approval_request))
    if approval.request_id != approval_request.request_id:
        raise ValueError("approval response request ID differs from the pending request")
    if approval.decision is ApprovalDecision.REJECT:
        execution = WorkflowExecutionResult(
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
        execution = WorkflowExecutionResult(
            status="completed",
            evidence=evidence,
            approval_request=approval_request,
            approval_decision=approval.decision,
            action_id_to_execute=approval.action_id_to_execute,
            action_to_execute=approval.action_to_execute,
            mitigation=mitigation,
            recovery=recovery,
        )

    discovery_tool_calls = sum(tracker.tool_calls for tracker in trackers.values())
    return {
        "framework": "langchain_single_agent",
        "model": request.manifest.model.name,
        "execution": execution.model_dump(mode="json"),
        "role_stats": {
            AgentRole.INCIDENT_COMMANDER.value: {
                "model_calls": model.lab_stats.model_calls,
                "tool_calls": discovery_tool_calls + command_agent.stats.tool_calls,
                "input_tokens": model.lab_stats.input_tokens,
                "output_tokens": model.lab_stats.output_tokens,
            }
        },
        "source_tool_calls": {
            role.value: list(tracker.called_tools) for role, tracker in trackers.items()
        },
        "provenance_correction_attempts": correction_attempt,
        "discovery_repair_attempts": discovery_attempt,
    }
