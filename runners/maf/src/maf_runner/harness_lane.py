"""Native Agent Framework Harness lane with role-scoped MCP specialists."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from agent_framework import (
    FileSystemAgentFileStore,
    MCPStreamableHTTPTool,
    Message,
    create_harness_agent,
)
from agent_framework.openai import OpenAIChatCompletionClient
from httpx import AsyncClient, Timeout
from openai import AsyncOpenAI

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
from maf_runner.workflow_lane import MafCommanderAgent, _evidence_ids, _render_tool_result


class _InstrumentedChatClient(OpenAIChatCompletionClient):
    def __init__(
        self,
        *,
        actor: str,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
    ) -> None:
        sdk = AsyncOpenAI(
            api_key="agent-lab-local",
            base_url=profile.base_url,
            timeout=profile.request_timeout_seconds,
            max_retries=0,
            default_headers={"X-Agent-Lab-Framework": "maf-harness"},
        )
        super().__init__(
            model=profile.name,
            async_client=sdk,
            function_invocation_configuration={
                "enabled": True,
                "max_iterations": 12,
                "max_function_calls": 6,
            },
        )
        self.actor = actor
        self.profile = profile
        self.telemetry = telemetry
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    async def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        if stream:
            raise ValueError("the harness protocol adapter requires non-streaming model calls")
        await self.telemetry.model_started(actor=self.actor, model=self.profile.name)
        response = await super().get_response(messages, stream=False, **kwargs)
        usage = response.usage_details or {}
        input_tokens = int(usage.get("input_token_count", 0))
        output_tokens = int(usage.get("output_token_count", 0))
        await self.telemetry.model_finished(
            actor=self.actor,
            model=self.profile.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self.model_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        return response


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
        self.called_tools: list[str] = []
        self.observed_evidence_ids: set[str] = set()

    async def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
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
        self.observed_evidence_ids.update(_evidence_ids(_render_tool_result(result)))
        return result


class _HarnessRoleRuntime:
    def __init__(
        self,
        role: AgentRole,
        endpoint: RoleEndpoint,
        profile: ModelProfile,
        telemetry: WorkflowTelemetry,
        workspace: Path,
    ) -> None:
        if endpoint.role is not role:
            raise ValueError("role endpoint does not match the harness specialist")
        self.role = role
        self._stack = AsyncExitStack()
        self.client = _InstrumentedChatClient(
            actor=role.value,
            profile=profile,
            telemetry=telemetry,
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
            agent_instructions=harness_specialist_prompt(role),
            tools=[self.mcp],
            max_context_window_tokens=profile.context_window_tokens,
            max_output_tokens=min(profile.max_output_tokens, 2048),
            file_memory_store=FileSystemAgentFileStore(workspace / "memory" / role.value),
            file_access_store=FileSystemAgentFileStore(workspace / "shared"),
            file_access_disable_readonly_tool_approval=True,
            file_access_disable_write_tool_approval=True,
            disable_web_search=True,
            disable_tool_auto_approval=True,
            default_options={
                "temperature": profile.temperature,
                "top_p": profile.top_p,
                "presence_penalty": profile.presence_penalty,
                "seed": profile.seed,
            },
        )

    async def __aenter__(self) -> _HarnessRoleRuntime:
        await self._stack.enter_async_context(self._http)
        await self._stack.enter_async_context(self.mcp)
        await self._stack.enter_async_context(self.agent)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.__aexit__(*exc_info)


def _validate_role_provenance(
    proposal: HarnessProposal,
    roles: dict[AgentRole, _HarnessRoleRuntime],
) -> None:
    for finding in proposal.findings:
        runtime = roles[finding.role]
        observed_tools = set(runtime.mcp.called_tools)
        if not observed_tools:
            raise ValueError(f"{finding.role.value} made no native MCP calls")
        if not set(finding.evidence_ids).issubset(runtime.mcp.observed_evidence_ids):
            raise ValueError(
                f"{finding.role.value} cited evidence outside its native MCP results: "
                f"cited={finding.evidence_ids}, "
                f"observed={sorted(runtime.mcp.observed_evidence_ids)}"
            )


async def run_live_approved(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, Any]:
    """Run native harness delegation, then the shared approval/recovery boundary."""

    required_roles = (*SPECIALIST_ROLES, AgentRole.INCIDENT_COMMANDER)
    missing = [role.value for role in required_roles if role not in request.role_endpoints]
    if missing:
        raise ValueError(f"run request is missing harness endpoints: {missing}")
    workspace = request.paths.workspace
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.chmod(0o700)

    async with AsyncExitStack() as stack:
        roles: dict[AgentRole, _HarnessRoleRuntime] = {}
        for role in sorted(SPECIALIST_ROLES, key=lambda item: item.value):
            roles[role] = await stack.enter_async_context(
                _HarnessRoleRuntime(
                    role,
                    request.role_endpoints[role],
                    request.manifest.model,
                    telemetry,
                    workspace,
                )
            )
        commander_client = _InstrumentedChatClient(
            actor=AgentRole.INCIDENT_COMMANDER.value,
            profile=request.manifest.model,
            telemetry=telemetry,
        )
        commander = create_harness_agent(
            commander_client,
            name=AgentRole.INCIDENT_COMMANDER.value,
            description=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
            agent_instructions=render_role_prompt(AgentRole.INCIDENT_COMMANDER),
            max_context_window_tokens=request.manifest.model.context_window_tokens,
            max_output_tokens=min(request.manifest.model.max_output_tokens, 4096),
            file_memory_store=FileSystemAgentFileStore(workspace / "memory" / "commander"),
            file_access_store=FileSystemAgentFileStore(workspace / "shared"),
            file_access_disable_readonly_tool_approval=True,
            file_access_disable_write_tool_approval=True,
            background_agents=[runtime.agent for runtime in roles.values()],
            disable_web_search=True,
            disable_tool_auto_approval=True,
            default_options={
                "temperature": request.manifest.model.temperature,
                "top_p": request.manifest.model.top_p,
                "presence_penalty": request.manifest.model.presence_penalty,
                "seed": request.manifest.model.seed,
            },
        )
        await stack.enter_async_context(commander)
        session = commander.create_session()
        await commander.run(
            harness_task_prompt(request.public_ticket),
            session=session,
        )
        for _attempt in range(2):
            active_roles = {role for role, runtime in roles.items() if runtime.mcp.called_tools}
            if len(active_roles) >= 2:
                break
            missing_roles = sorted(
                (role.value for role in SPECIALIST_ROLES - active_roles),
            )
            await commander.run(
                "The deterministic evidence gate is not yet satisfied: fewer than two "
                "specialist roles have made real MCP calls. Delegate now to at least one of "
                f"these remaining specialists and retrieve its completed result: {missing_roles}. "
                "Do not invent the result yourself.",
                session=session,
            )
        evidence_allowlist = "\n".join(
            f"- {role.value}: {sorted(runtime.mcp.observed_evidence_ids)}"
            for role, runtime in sorted(roles.items(), key=lambda item: item[0].value)
            if runtime.mcp.called_tools
        )
        response = await commander.run(
            "Using only the completed background-task results already in this session, return "
            "the HarnessProposal now. Include no finding that was not returned by a specialist. "
            "Opaque evidence IDs must be copied byte-for-byte; do not replace them with dates, "
            "descriptions, or invented aliases. The control plane observed this role-specific "
            f"evidence-ID allowlist:\n{evidence_allowlist}\n"
            "Every finding must cite one or more exact IDs from its own role's list, and the "
            "commander synthesis must cite exact IDs from at least two included roles.",
            session=session,
            options={
                "temperature": request.manifest.model.temperature,
                "top_p": request.manifest.model.top_p,
                "presence_penalty": request.manifest.model.presence_penalty,
                "max_tokens": min(request.manifest.model.max_output_tokens, 4096),
                "seed": request.manifest.model.seed,
                "response_format": HarnessProposal,
                "tool_choice": "none",
            },
        )
        proposal = HarnessProposal.model_validate(response.value)
        try:
            _validate_role_provenance(proposal, roles)
        except ValueError as exc:
            response = await commander.run(
                "The previous structured proposal failed the deterministic provenance "
                f"validator: {exc}. Return one corrected HarnessProposal. Copy only exact "
                "evidence IDs from this role-specific allowlist and keep findings scoped to "
                f"their roles:\n{evidence_allowlist}",
                session=session,
                options={
                    "temperature": request.manifest.model.temperature,
                    "top_p": request.manifest.model.top_p,
                    "presence_penalty": request.manifest.model.presence_penalty,
                    "max_tokens": min(request.manifest.model.max_output_tokens, 4096),
                    "seed": request.manifest.model.seed,
                    "response_format": HarnessProposal,
                    "tool_choice": "none",
                },
            )
            proposal = HarnessProposal.model_validate(response.value)
            _validate_role_provenance(proposal, roles)
        evidence = evaluate_harness_proposal(proposal)
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
            "framework": "maf_harness",
            "model": request.manifest.model.name,
            "execution": execution.model_dump(mode="json"),
            "role_stats": {
                **{
                    role.value: {
                        "model_calls": runtime.client.model_calls,
                        "tool_calls": len(runtime.mcp.called_tools),
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
