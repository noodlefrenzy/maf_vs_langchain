"""Native Deep Agents lane with role-scoped MCP subagents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agent_lab.contracts import AgentRole, ApprovalDecision, ModelProfile
from agent_lab.harness_contracts import (
    SPECIALIST_ROLES,
    HarnessApprovalResponse,
    HarnessExecutionResult,
    HarnessProposal,
    evaluate_harness_proposal,
    harness_specialist_prompt,
    harness_task_prompt,
)
from agent_lab.prompt_bundle import get_role_definition, render_role_prompt
from agent_lab.runner_host import ApprovalProvider
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.tool_catalog import tool_definitions_for_role
from agent_lab.workflow_telemetry import WorkflowTelemetry
from langgraph_runner.workflow_lane import (
    LangGraphCommanderAgent,
    _evidence_ids,
    _render_tool_result,
)

_HARNESS_SPECIALIST_TOOLS = {
    "edit_file",
    "glob",
    "grep",
    "ls",
    "read_file",
    "write_file",
}


@dataclass
class _ModelStats:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class _InstrumentedChatOpenAI(ChatOpenAI):
    _actor: str = PrivateAttr()
    _lab_profile: ModelProfile = PrivateAttr()
    _telemetry: WorkflowTelemetry = PrivateAttr()
    _stats: _ModelStats = PrivateAttr(default_factory=_ModelStats)

    def __init__(
        self,
        *,
        actor: str,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
    ) -> None:
        super().__init__(
            model=profile.name,
            api_key="agent-lab-local",
            base_url=profile.base_url,
            temperature=profile.temperature,
            top_p=profile.top_p,
            presence_penalty=profile.presence_penalty,
            max_completion_tokens=min(profile.max_output_tokens, 4096),
            seed=profile.seed,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            stream_usage=True,
            default_headers={"X-Agent-Lab-Framework": "deep-agents"},
        )
        self._actor = actor
        self._lab_profile = profile
        self._telemetry = telemetry

    @property
    def lab_stats(self) -> _ModelStats:
        return self._stats

    async def _agenerate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        await self._telemetry.model_started(
            actor=self._actor,
            model=self._lab_profile.name,
        )
        self._stats.model_calls += 1
        response = await super()._agenerate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )
        input_tokens = 0
        output_tokens = 0
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
        if input_tokens == 0 and output_tokens == 0:
            token_usage = (response.llm_output or {}).get("token_usage", {})
            input_tokens = int(token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)))
            output_tokens = int(
                token_usage.get("completion_tokens", token_usage.get("output_tokens", 0))
            )
        await self._telemetry.model_finished(
            actor=self._actor,
            model=self._lab_profile.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self._stats.input_tokens += input_tokens
        self._stats.output_tokens += output_tokens
        return response


class _ToolTracker:
    def __init__(self, actor: str, telemetry: WorkflowTelemetry) -> None:
        self.actor = actor
        self.telemetry = telemetry
        self.called_tools: list[str] = []
        self.observed_evidence_ids: set[str] = set()

    def wrap(self, tool: BaseTool) -> BaseTool:
        async def invoke(**arguments: Any) -> Any:
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
            self.observed_evidence_ids.update(_evidence_ids(_render_tool_result(result)))
            return result

        return StructuredTool.from_function(
            coroutine=invoke,
            name=tool.name,
            description=tool.description or tool.name,
            args_schema=tool.args_schema,
        )


def _chat_model(
    profile: ModelProfile,
    *,
    actor: str,
    telemetry: WorkflowTelemetry,
) -> _InstrumentedChatOpenAI:
    return _InstrumentedChatOpenAI(
        actor=actor,
        profile=profile,
        telemetry=telemetry,
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


def _validate_role_provenance(
    proposal: HarnessProposal,
    trackers: dict[AgentRole, _ToolTracker],
) -> None:
    for finding in proposal.findings:
        tracker = trackers[finding.role]
        observed_tools = set(tracker.called_tools)
        if not observed_tools:
            raise ValueError(f"{finding.role.value} made no native MCP calls")
        unknown_tools = set(finding.tools_used) - observed_tools - _HARNESS_SPECIALIST_TOOLS
        if unknown_tools:
            raise ValueError(
                f"{finding.role.value} cited tools outside its native MCP calls: "
                f"unknown={sorted(unknown_tools)}, observed={sorted(observed_tools)}"
            )
        if not set(finding.evidence_ids).issubset(tracker.observed_evidence_ids):
            raise ValueError(
                f"{finding.role.value} cited evidence outside its native MCP results: "
                f"cited={finding.evidence_ids}, "
                f"observed={sorted(tracker.observed_evidence_ids)}"
            )


async def run_live_approved(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, Any]:
    """Run native Deep Agents delegation, then shared approval and recovery."""

    required_roles = (*SPECIALIST_ROLES, AgentRole.INCIDENT_COMMANDER)
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
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    trackers: dict[AgentRole, _ToolTracker] = {}
    models: dict[AgentRole, _InstrumentedChatOpenAI] = {}
    subagents: list[SubAgent] = []
    for role in sorted(SPECIALIST_ROLES, key=lambda item: item.value):
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
                system_prompt=harness_specialist_prompt(role),
                tools=await _role_tools(request.role_endpoints[role], role, tracker),
                model=model,
            )
        )

    commander_model = _chat_model(
        request.manifest.model,
        actor=AgentRole.INCIDENT_COMMANDER.value,
        telemetry=telemetry,
    )
    backend = FilesystemBackend(root_dir=workspace, virtual_mode=True)
    delegation_graph = create_deep_agent(
        model=commander_model,
        system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
        middleware=[ModelCallLimitMiddleware(run_limit=6, exit_behavior="end")],
        subagents=subagents,
        backend=backend,
        name=AgentRole.INCIDENT_COMMANDER.value,
    )
    state = await delegation_graph.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": harness_task_prompt(request.public_ticket),
                }
            ]
        },
        config={"recursion_limit": 64},
    )
    for _attempt in range(2):
        active_roles = {role for role, tracker in trackers.items() if tracker.called_tools}
        if len(active_roles) >= 2:
            break
        missing_roles = sorted(role.value for role in SPECIALIST_ROLES - active_roles)
        state = await delegation_graph.ainvoke(
            {
                "messages": [
                    *state["messages"],
                    {
                        "role": "user",
                        "content": (
                            "The deterministic evidence gate is not yet satisfied: fewer than "
                            "two specialist roles have made real MCP calls. Delegate now to at "
                            "least one of these remaining specialists and retrieve its completed "
                            f"result: {missing_roles}. Do not invent the result yourself."
                        ),
                    },
                ]
            },
            config={"recursion_limit": 64},
        )
    active_roles = {role for role, tracker in trackers.items() if tracker.called_tools}
    if len(active_roles) < 2:
        raise ValueError("Deep Agents delegation did not satisfy the two-specialist MCP gate")

    evidence_allowlist = "\n".join(
        f"- {role.value}: {sorted(tracker.observed_evidence_ids)}"
        for role, tracker in sorted(trackers.items(), key=lambda item: item[0].value)
        if tracker.called_tools
    )
    synthesis_graph = create_deep_agent(
        model=commander_model,
        system_prompt=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
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
                    "content": (
                        "Using only the completed specialist task results already in this "
                        "conversation, return the HarnessProposal now. Include no finding that "
                        "was not returned by a specialist. Opaque evidence IDs must be copied "
                        "byte-for-byte; do not replace them with dates, descriptions, or "
                        "invented aliases. The control plane observed this role-specific "
                        f"evidence-ID allowlist:\n{evidence_allowlist}\nEvery finding must cite "
                        "one or more exact IDs from its own role's list, and the commander "
                        "synthesis must cite exact IDs from at least two included roles."
                    ),
                },
            ]
        },
        config={"recursion_limit": 16},
    )
    proposal = HarnessProposal.model_validate(synthesis_state.get("structured_response"))
    _validate_role_provenance(proposal, trackers)
    evidence = evaluate_harness_proposal(proposal)
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
        if not approval.approval_token or not approval.action_to_execute:
            raise ValueError("approved response requires an action and approval token")
        mitigation, recovery = await command_agent.apply_and_verify(
            approval.request_id,
            approval.approval_token,
        )
        execution = HarnessExecutionResult(
            status="completed",
            evidence=evidence,
            approval_request=approval_request,
            approval_decision=approval.decision,
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
                    "tool_calls": len(trackers[role].called_tools),
                    "input_tokens": models[role].lab_stats.input_tokens,
                    "output_tokens": models[role].lab_stats.output_tokens,
                }
                for role in sorted(SPECIALIST_ROLES, key=lambda item: item.value)
            },
            AgentRole.INCIDENT_COMMANDER.value: {
                "model_calls": commander_model.lab_stats.model_calls,
                "tool_calls": command_agent.stats.tool_calls,
                "input_tokens": commander_model.lab_stats.input_tokens,
                "output_tokens": commander_model.lab_stats.output_tokens,
            },
        },
    }
