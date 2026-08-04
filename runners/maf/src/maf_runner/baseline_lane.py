"""Native MAF primitive-agent implementation of the single-agent baseline."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from agent_framework import Agent, MCPStreamableHTTPTool
from httpx import AsyncClient, Timeout

from agent_lab.baseline_contracts import (
    BASELINE_DISCOVERY_REPAIR_TURNS,
    BASELINE_PHASE_MODEL_CALL_LIMIT,
    BASELINE_PROVENANCE_CORRECTION_TURNS,
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
from agent_lab.model_request_policy import maf_request_options
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
from maf_runner.chat_telemetry import InstrumentedChatClient
from maf_runner.workflow_lane import MafCommanderAgent


class _TrackedSourceMCP(MCPStreamableHTTPTool):
    """Record which source endpoint returned each tool observation."""

    def __init__(
        self,
        *args: Any,
        source_role: AgentRole,
        telemetry: WorkflowTelemetry,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.source_role = source_role
        self.telemetry = telemetry
        self.tool_calls = 0
        self.called_tools: list[str] = []
        self.evidence_ids: set[str] = set()

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        actor = AgentRole.INCIDENT_COMMANDER.value
        self.tool_calls += 1
        await self.telemetry.tool_started(actor=actor, tool_name=tool_name)
        try:
            result = await super().call_tool(tool_name, **kwargs)
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
        self.called_tools.append(tool_name)
        self.evidence_ids.update(evidence_ids_from_text(render_tool_result(result)))
        return result


def _observations(
    sources: dict[AgentRole, _TrackedSourceMCP],
) -> dict[AgentRole, BaselineSourceObservation]:
    return {
        role: BaselineSourceObservation(
            called_tools=frozenset(source.called_tools),
            evidence_ids=frozenset(source.evidence_ids),
        )
        for role, source in sources.items()
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

    async with AsyncExitStack() as stack:
        sources: dict[AgentRole, _TrackedSourceMCP] = {}
        for role in BASELINE_SOURCE_ROLES:
            endpoint: RoleEndpoint = request.role_endpoints[role]
            http = AsyncClient(
                headers={"Authorization": f"Bearer {endpoint.token}"},
                follow_redirects=False,
                timeout=Timeout(request.manifest.model.request_timeout_seconds),
            )
            await stack.enter_async_context(http)
            source = _TrackedSourceMCP(
                name=f"baseline-{role.value}",
                url=endpoint.url,
                allowed_tools=list(baseline_tool_names_for_source(role)),
                load_prompts=False,
                request_timeout=int(request.manifest.model.request_timeout_seconds),
                approval_mode="never_require",
                http_client=http,
                source_role=role,
                telemetry=telemetry,
            )
            sources[role] = await stack.enter_async_context(source)

        model = InstrumentedChatClient(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            profile=request.manifest.model,
            telemetry=telemetry,
            framework="maf-single-agent",
            model_call_limit=BASELINE_PHASE_MODEL_CALL_LIMIT,
            function_call_limit=request.manifest.budget.tool_calls,
        )
        agent = Agent(
            model,
            name="maf_single_agent",
            instructions=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
            tools=list(sources.values()),
        )
        await stack.enter_async_context(agent)
        session = agent.create_session()
        await agent.run(
            baseline_task_prompt(request.public_ticket),
            session=session,
        )
        discovery_attempt = 0
        for _ in range(BASELINE_DISCOVERY_REPAIR_TURNS):
            observations = _observations(sources)
            active_sources = active_baseline_sources(observations)
            if len(active_sources) >= 2:
                break
            discovery_attempt += 1
            missing_roles = sorted(
                role.value for role in set(BASELINE_SOURCE_ROLES) - active_sources
            )
            await agent.run(
                baseline_discovery_repair_prompt(missing_roles),
                session=session,
            )
        observations = _observations(sources)
        require_baseline_discovery(observations)
        response = await agent.run(
            baseline_synthesis_prompt(baseline_evidence_allowlist(observations)),
            session=session,
            options={
                **maf_request_options(request.manifest.model),
                "response_format": BaselineProposal,
                "tool_choice": "none",
            },
        )
        proposal = BaselineProposal.model_validate(response.value)
        correction_attempt = 0
        while True:
            observations = _observations(sources)
            try:
                validate_baseline_provenance(proposal, observations)
                break
            except ValueError as exc:
                if correction_attempt >= BASELINE_PROVENANCE_CORRECTION_TURNS:
                    raise
                correction_attempt += 1
                response = await agent.run(
                    baseline_provenance_correction_prompt(
                        exc,
                        baseline_evidence_allowlist(observations),
                    ),
                    session=session,
                    options={
                        **maf_request_options(request.manifest.model),
                        "response_format": BaselineProposal,
                        "tool_choice": "none",
                    },
                )
                proposal = BaselineProposal.model_validate(response.value)

        evidence = evaluate_baseline_proposal(proposal)
        if not evidence.passed:
            raise ValueError(f"single-agent evidence gate failed: {evidence.gate_reasons}")
        command_agent = await stack.enter_async_context(
            MafCommanderAgent(
                request.role_endpoints[AgentRole.INCIDENT_COMMANDER],
                request.manifest.model,
                telemetry,
            )
        )
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

        discovery_tool_calls = sum(source.tool_calls for source in sources.values())
        return {
            "framework": "maf_single_agent",
            "model": request.manifest.model.name,
            "execution": execution.model_dump(mode="json"),
            "role_stats": {
                AgentRole.INCIDENT_COMMANDER.value: {
                    "model_calls": model.model_calls,
                    "tool_calls": discovery_tool_calls + command_agent.stats.tool_calls,
                    "input_tokens": model.input_tokens,
                    "output_tokens": model.output_tokens,
                }
            },
            "source_tool_calls": {
                role.value: list(source.called_tools) for role, source in sources.items()
            },
            "provenance_correction_attempts": correction_attempt,
            "discovery_repair_attempts": discovery_attempt,
        }
