"""Native Agent Framework Harness lane with role-scoped MCP specialists."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from agent_framework import (
    FileSystemAgentFileStore,
    MCPStreamableHTTPTool,
    background_tasks_running,
    background_tasks_running_message,
    create_harness_agent,
)
from httpx import AsyncClient, Timeout

from agent_lab.contracts import AgentRole, ApprovalDecision, ModelProfile
from agent_lab.harness_contracts import (
    HARNESS_DELEGATION_REPAIR_TURNS,
    HARNESS_PHASE_MODEL_CALL_LIMIT,
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
from agent_lab.model_request_policy import maf_request_options
from agent_lab.prompt_bundle import get_role_definition, render_role_prompt
from agent_lab.runner_host import ApprovalProvider
from agent_lab.runner_protocol import RoleEndpoint, RunRequest
from agent_lab.tool_catalog import tool_definitions_for_role
from agent_lab.workflow_contracts import evidence_ids_from_text, render_tool_result
from agent_lab.workflow_telemetry import WorkflowTelemetry
from maf_runner.chat_telemetry import InstrumentedChatClient
from maf_runner.workflow_lane import MafCommanderAgent


class _TrackedRoleMCP(MCPStreamableHTTPTool):
    def __init__(
        self,
        *args: Any,
        actor: str,
        telemetry: WorkflowTelemetry,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.telemetry = telemetry
        self.tool_calls = 0
        self.called_tools: list[str] = []
        self.observed_evidence_ids: set[str] = set()
        self.delegation_observed = False

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        self.tool_calls += 1
        if not self.delegation_observed:
            await self.telemetry.handoff(
                source_actor=AgentRole.INCIDENT_COMMANDER.value,
                target_actor=self.actor,
                phase="specialist_delegated",
            )
            self.delegation_observed = True
        await self.telemetry.tool_started(actor=self.actor, tool_name=tool_name)
        try:
            result = await super().call_tool(tool_name, **kwargs)
        except Exception:
            await self.telemetry.tool_finished(
                actor=self.actor,
                tool_name=tool_name,
                succeeded=False,
            )
            raise
        await self.telemetry.tool_finished(
            actor=self.actor,
            tool_name=tool_name,
            succeeded=True,
        )
        self.called_tools.append(tool_name)
        self.observed_evidence_ids.update(evidence_ids_from_text(render_tool_result(result)))
        return result


class _TrackedBackgroundAgent:
    """Retain native background tasks until the adapter can drain them."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self._active_tasks: set[asyncio.Task[Any]] = set()

    @property
    def name(self) -> str | None:
        return self._agent.name

    @property
    def description(self) -> str | None:
        return self._agent.description

    def create_session(self) -> Any:
        return self._agent.create_session()

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        task = asyncio.current_task()
        if task is None:
            return await self._agent.run(*args, **kwargs)
        self._active_tasks.add(task)
        try:
            return await self._agent.run(*args, **kwargs)
        finally:
            self._active_tasks.discard(task)

    async def drain(self) -> None:
        """Wait for every native task that was active at phase exit."""

        await asyncio.sleep(0)
        while self._active_tasks:
            await asyncio.gather(*tuple(self._active_tasks), return_exceptions=True)

    async def cancel_and_drain(self) -> None:
        """Cancel and reap any task left after an exceptional phase exit."""

        await asyncio.sleep(0)
        while self._active_tasks:
            tasks = tuple(self._active_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class _HarnessRoleRuntime:
    def __init__(
        self,
        role: AgentRole,
        endpoint: RoleEndpoint,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
        workspace: Path,
        prompt_variant: Literal["canonical", "paraphrase_a"],
        context_compaction: Literal["native", "disabled"],
    ) -> None:
        if endpoint.role is not role:
            raise ValueError("role endpoint does not match the harness specialist")
        self.role = role
        self._stack = AsyncExitStack()
        self.client = InstrumentedChatClient(
            actor=role.value,
            profile=profile,
            telemetry=telemetry,
            framework="maf-harness",
            model_call_limit=HARNESS_PHASE_MODEL_CALL_LIMIT,
        )
        self._http = AsyncClient(
            headers={"Authorization": f"Bearer {endpoint.token}"},
            follow_redirects=False,
            timeout=Timeout(profile.request_timeout_seconds),
        )
        self.mcp = _TrackedRoleMCP(
            name=f"incident-{role.value}",
            url=endpoint.url,
            allowed_tools=[tool.name for tool in tool_definitions_for_role(role)],
            load_prompts=False,
            request_timeout=int(profile.request_timeout_seconds),
            approval_mode="never_require",
            http_client=self._http,
            actor=role.value,
            telemetry=telemetry,
        )
        self.agent = create_harness_agent(
            self.client,
            name=role.value,
            description=get_role_definition(role).responsibility,
            agent_instructions=harness_specialist_prompt(role, prompt_variant),
            tools=[self.mcp],
            max_context_window_tokens=profile.context_window_tokens,
            max_output_tokens=profile.max_output_tokens,
            disable_compaction=context_compaction == "disabled",
            file_memory_store=FileSystemAgentFileStore(workspace / "memory" / role.value),
            file_access_store=FileSystemAgentFileStore(workspace / "shared"),
            file_access_disable_readonly_tool_approval=True,
            file_access_disable_write_tool_approval=True,
            disable_web_search=True,
            disable_tool_auto_approval=True,
            default_options=maf_request_options(profile),
        )
        self.background_agent = _TrackedBackgroundAgent(self.agent)

    async def __aenter__(self) -> _HarnessRoleRuntime:
        await self._stack.enter_async_context(self._http)
        await self._stack.enter_async_context(self.mcp)
        await self._stack.enter_async_context(self.agent)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.__aexit__(*exc_info)


def _role_observations(
    roles: dict[AgentRole, _HarnessRoleRuntime],
) -> dict[AgentRole, HarnessRoleObservation]:
    return {
        role: HarnessRoleObservation(
            called_tools=frozenset(runtime.mcp.called_tools),
            evidence_ids=frozenset(runtime.mcp.observed_evidence_ids),
        )
        for role, runtime in roles.items()
    }


async def _drain_background_roles(roles: dict[AgentRole, _HarnessRoleRuntime]) -> None:
    await asyncio.gather(*(runtime.background_agent.drain() for runtime in roles.values()))


async def _cancel_background_roles(roles: dict[AgentRole, _HarnessRoleRuntime]) -> None:
    await asyncio.gather(
        *(runtime.background_agent.cancel_and_drain() for runtime in roles.values())
    )


async def run_live_approved(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, Any]:
    """Run native harness delegation, then the shared approval/recovery boundary."""

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

    async with AsyncExitStack() as stack:
        roles: dict[AgentRole, _HarnessRoleRuntime] = {}
        for role in sorted(configured_roles, key=lambda item: item.value):
            roles[role] = await stack.enter_async_context(
                _HarnessRoleRuntime(
                    role,
                    request.role_endpoints[role],
                    request.manifest.model,
                    telemetry,
                    workspace,
                    request.harness_ablation.prompt_variant,
                    request.harness_ablation.context_compaction,
                )
            )
        commander_client = InstrumentedChatClient(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            profile=request.manifest.model,
            telemetry=telemetry,
            framework="maf-harness",
            model_call_limit=HARNESS_PHASE_MODEL_CALL_LIMIT,
        )
        commander = create_harness_agent(
            commander_client,
            name=AgentRole.INCIDENT_COMMANDER.value,
            description=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
            agent_instructions=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
            max_context_window_tokens=request.manifest.model.context_window_tokens,
            max_output_tokens=request.manifest.model.max_output_tokens,
            disable_compaction=request.harness_ablation.context_compaction == "disabled",
            file_memory_store=FileSystemAgentFileStore(workspace / "memory" / "commander"),
            file_access_store=FileSystemAgentFileStore(workspace / "shared"),
            file_access_disable_readonly_tool_approval=True,
            file_access_disable_write_tool_approval=True,
            background_agents=[runtime.background_agent for runtime in roles.values()],
            disable_web_search=True,
            disable_tool_auto_approval=True,
            loop_should_continue=background_tasks_running(),
            loop_next_message=background_tasks_running_message,
            loop_max_iterations=HARNESS_PHASE_MODEL_CALL_LIMIT,
            default_options=maf_request_options(request.manifest.model),
        )
        await stack.enter_async_context(commander)
        session = commander.create_session()
        try:
            with commander_client.bounded_phase():
                await commander.run(
                    harness_task_prompt(
                        request.public_ticket,
                        request.harness_ablation.prompt_variant,
                        request.harness_ablation.reviewer_enabled,
                    ),
                    session=session,
                )
            await _drain_background_roles(roles)
            for _attempt in range(HARNESS_DELEGATION_REPAIR_TURNS):
                observations = _role_observations(roles)
                observed_roles = active_harness_roles(observations)
                if len(observed_roles) >= 2:
                    break
                missing_roles = sorted(role.value for role in configured_roles - observed_roles)
                with commander_client.bounded_phase():
                    await commander.run(
                        harness_delegation_repair_prompt(missing_roles),
                        session=session,
                    )
                await _drain_background_roles(roles)
        finally:
            await _cancel_background_roles(roles)
        observations = _role_observations(roles)
        require_harness_delegation(observations)
        evidence_allowlist = harness_provenance_allowlist(observations)
        with commander_client.bounded_phase():
            response = await commander.run(
                harness_synthesis_prompt(evidence_allowlist),
                session=session,
                options={
                    **maf_request_options(request.manifest.model),
                    "response_format": HarnessProposal,
                    "tool_choice": "none",
                },
            )
        proposal = HarnessProposal.model_validate(response.value)
        correction_attempt = 0
        while True:
            try:
                validate_harness_role_provenance(proposal, observations)
                break
            except ValueError as exc:
                if correction_attempt >= request.harness_ablation.provenance_correction_turns:
                    raise
                correction_attempt += 1
                with commander_client.bounded_phase():
                    response = await commander.run(
                        harness_provenance_correction_prompt(exc, evidence_allowlist),
                        session=session,
                        options={
                            **maf_request_options(request.manifest.model),
                            "response_format": HarnessProposal,
                            "tool_choice": "none",
                        },
                    )
                proposal = HarnessProposal.model_validate(response.value)
        for finding in proposal.findings:
            await telemetry.handoff(
                source_actor=finding.role.value,
                target_actor=AgentRole.INCIDENT_COMMANDER.value,
                phase="specialist_finding_returned",
            )
        evidence = evaluate_harness_proposal(proposal, attempt=correction_attempt)
        if not evidence.passed:
            raise ValueError(f"harness evidence gate failed: {evidence.gate_reasons}")

        command_agent = await stack.enter_async_context(
            MafCommanderAgent(
                request.role_endpoints[AgentRole.INCIDENT_COMMANDER],
                request.manifest.model,
                telemetry,
            )
        )
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
            "framework": "maf_harness",
            "model": request.manifest.model.name,
            "execution": execution.model_dump(mode="json"),
            "role_stats": {
                **{
                    role.value: {
                        "model_calls": runtime.client.model_calls,
                        "tool_calls": runtime.mcp.tool_calls,
                        "input_tokens": runtime.client.input_tokens,
                        "output_tokens": runtime.client.output_tokens,
                    }
                    for role, runtime in roles.items()
                },
                AgentRole.INCIDENT_COMMANDER.value: {
                    "model_calls": commander_client.model_calls,
                    "tool_calls": command_agent.stats.tool_calls,
                    "input_tokens": commander_client.input_tokens,
                    "output_tokens": commander_client.output_tokens,
                },
            },
        }
