"""Reusable protocol loop for framework-native runner process entry points."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import traceback
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from agent_lab.contracts import (
    ApprovalRequest,
    EventKind,
    EventVisibility,
    FrameworkLane,
    NormalizedEvent,
    TerminalStatus,
)
from agent_lab.model_request_policy import effective_request_policy
from agent_lab.native_tracing import FrameworkInstrumentor, NativeTracingSession
from agent_lab.runner_protocol import (
    ApprovalCommand,
    CancelCommand,
    CheckpointCapability,
    ErrorMessage,
    EventMessage,
    FinalizeCommand,
    HeartbeatMessage,
    PrepareCommand,
    PreparedMessage,
    PreparedRun,
    RunnerUsage,
    RunRequest,
    ShutdownCommand,
    StartCommand,
    TerminalMessage,
    UsageMessage,
    decode_runner_command,
    encode_protocol_message,
)
from agent_lab.workflow_errors import EvidenceGateFailed
from agent_lab.workflow_telemetry import WorkflowTelemetry

ApprovalProvider = Callable[[ApprovalRequest], Awaitable[dict[str, Any]]]
WorkflowProtocolExecutor = Callable[
    [RunRequest, ApprovalProvider, WorkflowTelemetry],
    Awaitable[dict[str, Any]],
]


class WorkflowBudgetExceeded(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WorkflowTerminated(RuntimeError):
    """An outer control-plane terminal decision delivered through the protocol."""

    def __init__(self, status: TerminalStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class WorkflowProtocolSession(WorkflowTelemetry):
    """Emit one native workflow execution through the normalized protocol."""

    def __init__(self, descriptor: NativeRunnerDescriptor, request: RunRequest) -> None:
        self.descriptor = descriptor
        self.request = request
        self.sequence = 0
        self.usage = RunnerUsage()
        self.events: list[NormalizedEvent] = []
        self._agent_events: dict[str, list[NormalizedEvent]] = {}
        self._model_events: dict[str, NormalizedEvent] = {}
        self._tool_events: dict[tuple[str, str], list[NormalizedEvent]] = {}
        self._last_failed_operation: TerminalStatus | None = None

    def start(self) -> None:
        self.emit(
            EventKind.RUN_STARTED,
            actor=self.descriptor.name,
            payload={"execution_mode": "native_workflow"},
        )

    async def handoff(
        self,
        *,
        source_actor: str,
        target_actor: str,
        phase: str,
    ) -> None:
        self.emit(
            EventKind.HANDOFF,
            actor=source_actor,
            payload={
                "source_actor": source_actor,
                "target_actor": target_actor,
                "source_namespace": self._actor_namespace(source_actor),
                "target_namespace": self._actor_namespace(target_actor),
                "phase": phase,
            },
        )

    async def model_started(self, *, actor: str, model: str) -> None:
        self._last_failed_operation = None
        self._advance_usage("model_calls", 1, "model_call_budget_exhausted")
        agent_event = self.emit(
            EventKind.AGENT_STARTED,
            actor=actor,
            payload={"phase": "model_turn", "namespace": self._actor_namespace(actor)},
        )
        self._agent_events.setdefault(actor, []).append(agent_event)
        event = self.emit(
            EventKind.MODEL_REQUESTED,
            actor=actor,
            parent_event_id=agent_event.event_id,
            payload={
                "model": model,
                "namespace": self._actor_namespace(actor),
            },
        )
        self._model_events[actor] = event

    async def model_finished(
        self,
        *,
        actor: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        output_limit_exceeded = self._record_usage("output_tokens", output_tokens)
        parent = self._model_events.pop(actor, None)
        self.emit(
            EventKind.MODEL_RESPONDED,
            actor=actor,
            parent_event_id=parent.event_id if parent is not None else None,
            payload={
                "model": model,
                "namespace": self._actor_namespace(actor),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        pending_agents = self._agent_events.get(actor, [])
        agent_parent = pending_agents.pop(0) if pending_agents else None
        if not pending_agents:
            self._agent_events.pop(actor, None)
        self.emit(
            EventKind.AGENT_FINISHED,
            actor=actor,
            parent_event_id=(agent_parent.event_id if agent_parent is not None else None),
            payload={"phase": "model_turn", "namespace": self._actor_namespace(actor)},
        )
        self.emit(
            EventKind.METRIC_RECORDED,
            actor=actor,
            payload={
                "name": "model_token_usage",
                "model": model,
                "namespace": self._actor_namespace(actor),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )
        if output_limit_exceeded:
            raise WorkflowBudgetExceeded("output_token_budget_exhausted")

    async def tool_started(self, *, actor: str, tool_name: str) -> None:
        self._last_failed_operation = None
        self._advance_usage("tool_calls", 1, "tool_call_budget_exhausted")
        requested = self.emit(
            EventKind.TOOL_REQUESTED,
            actor=actor,
            payload={
                "tool_name": tool_name,
                "namespace": self._actor_namespace(actor),
            },
        )
        self.emit(
            EventKind.TOOL_STARTED,
            actor=actor,
            parent_event_id=requested.event_id,
            payload={
                "tool_name": tool_name,
                "namespace": self._actor_namespace(actor),
            },
        )
        self._tool_events.setdefault((actor, tool_name), []).append(requested)

    async def tool_finished(
        self,
        *,
        actor: str,
        tool_name: str,
        succeeded: bool,
    ) -> None:
        pending = self._tool_events.get((actor, tool_name), [])
        parent = pending.pop(0) if pending else None
        if not pending:
            self._tool_events.pop((actor, tool_name), None)
        self.emit(
            EventKind.TOOL_FINISHED if succeeded else EventKind.TOOL_FAILED,
            actor=actor,
            parent_event_id=parent.event_id if parent is not None else None,
            payload={
                "tool_name": tool_name,
                "namespace": self._actor_namespace(actor),
            },
        )
        self._last_failed_operation = None if succeeded else TerminalStatus.TOOL_SERVICE_FAILURE

    def classify_unhandled_failure(self) -> TerminalStatus:
        """Classify an escaped exception using the last observable operation boundary."""

        if self._tool_events or self._last_failed_operation is TerminalStatus.TOOL_SERVICE_FAILURE:
            return TerminalStatus.TOOL_SERVICE_FAILURE
        if self._model_events:
            return TerminalStatus.MODEL_FAILURE
        return TerminalStatus.RUNNER_FAILURE

    def _actor_namespace(self, actor: str) -> str:
        return f"{self.request.manifest.lane.value}/{actor}"

    async def request_approval(self, request: ApprovalRequest) -> dict[str, Any]:
        self._advance_usage(
            "approval_requests",
            1,
            "approval_request_budget_exhausted",
        )
        self.emit(
            EventKind.APPROVAL_REQUESTED,
            actor=request.requested_by,
            payload=request.model_dump(mode="json"),
        )
        self.emit(
            EventKind.CHECKPOINT_SAVED,
            actor=self.descriptor.name,
            payload={"reason": "awaiting_approval", "request_id": request.request_id},
        )
        _send(
            HeartbeatMessage(
                run_id=self.request.manifest.run_id,
                phase="awaiting_approval",
            )
        )
        command = await _read_command()
        if isinstance(command, CancelCommand):
            raise WorkflowTerminated(command.status, command.reason)
        if isinstance(command, ShutdownCommand):
            raise asyncio.CancelledError("control_plane_shutdown")
        if not isinstance(command, ApprovalCommand):
            raise ValueError(f"{command.kind} received while awaiting approval")
        if command.run_id != self.request.manifest.run_id:
            raise ValueError("approval run ID differs from prepared run")
        if command.request_id != request.request_id:
            raise ValueError("approval request ID differs from pending request")
        self.emit(
            EventKind.APPROVAL_DECIDED,
            actor=command.decided_by,
            payload={
                "request_id": command.request_id,
                "decision": command.decision.value,
                "decided_by": command.decided_by,
                "decided_tick": command.decided_tick,
                "action_id_to_execute": (
                    command.action_id_to_execute.value
                    if command.action_id_to_execute is not None
                    else None
                ),
                "action_to_execute": command.action_to_execute,
            },
        )
        return {
            "request_id": command.request_id,
            "decision": command.decision.value,
            "action_id_to_execute": (
                command.action_id_to_execute.value
                if command.action_id_to_execute is not None
                else None
            ),
            "action_to_execute": command.action_to_execute,
            "approval_token": command.approval_token,
        }

    async def finish(self, result: dict[str, Any]) -> TerminalStatus:
        execution = result.get("execution")
        if not isinstance(execution, dict):
            raise ValueError("workflow result is missing terminal execution state")
        status = (
            TerminalStatus.APPROVAL_REJECTED
            if execution.get("status") == "approval_rejected"
            else TerminalStatus.COMPLETED
        )
        destination = self.request.paths.state / "workflow-execution.json"
        _write_private_json(destination, result)
        self.emit(
            EventKind.STATE_UPDATED,
            actor=self.descriptor.name,
            visibility=EventVisibility.INTERNAL,
            payload={
                "phase": "workflow_terminal",
                "status": execution.get("status"),
                "state_file": "state/workflow-execution.json",
            },
        )
        _send(
            HeartbeatMessage(
                run_id=self.request.manifest.run_id,
                phase="awaiting_finalization",
            )
        )
        command = await _read_command()
        if isinstance(command, CancelCommand):
            raise WorkflowTerminated(command.status, command.reason)
        if isinstance(command, ShutdownCommand):
            raise asyncio.CancelledError("control_plane_shutdown")
        if not isinstance(command, FinalizeCommand):
            raise ValueError(f"{command.kind} received while awaiting finalization")
        if command.run_id != self.request.manifest.run_id:
            raise ValueError("finalization run ID differs from prepared run")
        for artifact in sorted(
            command.finalization.artifacts,
            key=lambda item: item.path,
        ):
            self.emit(
                EventKind.ARTIFACT_WRITTEN,
                actor=self.descriptor.name,
                payload={"artifact": artifact.path, "sha256": artifact.sha256},
            )
        finished_payload: dict[str, Any] = {
            "status": status.value,
            "execution_mode": "native_workflow",
        }
        if command.finalization.task_success is not None:
            finished_payload["task_success"] = command.finalization.task_success
        if command.finalization.total_score is not None:
            finished_payload["total_score"] = command.finalization.total_score
        self.emit(
            EventKind.RUN_FINISHED,
            actor=self.descriptor.name,
            payload=finished_payload,
        )
        return status

    def fail(self, status: TerminalStatus, reason: str) -> None:
        self.emit(
            EventKind.ERROR,
            actor=self.descriptor.name,
            payload={"reason": reason},
        )
        self.emit(
            EventKind.RUN_FINISHED,
            actor=self.descriptor.name,
            payload={"status": status.value, "execution_mode": "native_workflow"},
        )

    def emit(
        self,
        kind: EventKind,
        *,
        actor: str,
        payload: dict[str, Any],
        visibility: EventVisibility = EventVisibility.PUBLIC,
        parent_event_id=None,
    ) -> NormalizedEvent:
        trace_id, span_id = _current_trace_context()
        event = NormalizedEvent(
            run_id=self.request.manifest.run_id,
            sequence=self.sequence,
            lane=self.request.manifest.lane,
            kind=kind,
            actor=actor,
            parent_event_id=parent_event_id,
            trace_id=trace_id,
            span_id=span_id,
            visibility=visibility,
            payload=payload,
        )
        self.sequence += 1
        self.events.append(event)
        _send(EventMessage(event=event))
        return event

    def terminal(self, status: TerminalStatus, reason: str) -> None:
        _send(
            TerminalMessage(
                run_id=self.request.manifest.run_id,
                status=status,
                reason=reason,
                usage=self.usage,
            )
        )

    def _advance_usage(self, field: str, increment: int, reason: str) -> None:
        if increment < 0:
            raise ValueError("usage increments cannot be negative")
        observed = getattr(self.usage, field) + increment
        if observed > getattr(self.request.manifest.budget, field):
            raise WorkflowBudgetExceeded(reason)
        self._record_usage(field, increment)

    def _record_usage(self, field: str, increment: int) -> bool:
        observed = getattr(self.usage, field) + increment
        self.usage = self.usage.model_copy(update={field: observed})
        _send(UsageMessage(run_id=self.request.manifest.run_id, usage=self.usage))
        return observed > getattr(self.request.manifest.budget, field)


class NativeRunnerDescriptor(BaseModel):
    """Static native metadata captured before a framework run starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    supported_lanes: tuple[FrameworkLane, ...]
    framework_versions: dict[str, str]
    contract_hashes: dict[str, str]
    topology: dict[FrameworkLane, dict[str, object]]
    normalized_topology: dict[FrameworkLane, dict[str, object]]
    checkpoint: dict[FrameworkLane, CheckpointCapability]

    @model_validator(mode="after")
    def validate_lane_metadata(self) -> NativeRunnerDescriptor:
        supported = set(self.supported_lanes)
        for label, lane_map in (
            ("topology", self.topology),
            ("normalized topology", self.normalized_topology),
            ("checkpoint", self.checkpoint),
        ):
            if set(lane_map) != supported:
                raise ValueError(f"{label} lanes must exactly match supported lanes")
        return self

    def prepared_run(self, request: RunRequest) -> PreparedRun:
        lane = request.manifest.lane
        if lane not in self.supported_lanes:
            raise ValueError(f"{self.name} does not support lane {lane.value}")
        topology_payload = json.dumps(
            self.topology[lane],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return PreparedRun(
            run_id=request.manifest.run_id,
            lane=lane,
            topology_sha256=hashlib.sha256(topology_payload).hexdigest(),
            effective_prompt_sha256=request.manifest.prompt_bundle_sha256,
            tool_catalog_sha256=request.manifest.tool_catalog_sha256,
            effective_request_policy_sha256=effective_request_policy(
                request.manifest.model,
                lane,
            )["policy_sha256"],
            execution_provenance_sha256=request.manifest.execution_provenance_sha256,
            framework_versions=self.framework_versions,
            checkpoint=self.checkpoint[lane],
        )


async def run_workflow_protocol(
    descriptor: NativeRunnerDescriptor,
    executor: WorkflowProtocolExecutor,
    *,
    instrument_framework: FrameworkInstrumentor | None = None,
) -> int:
    """Serve one real native workflow through the shared lifecycle protocol."""

    try:
        prepare = await _read_command()
    except Exception as exc:
        _send(ErrorMessage(error=f"invalid command: {type(exc).__name__}"))
        return 2
    if not isinstance(prepare, PrepareCommand):
        _send(ErrorMessage(error=f"{prepare.kind} received before prepare"))
        return 2
    request = prepare.request
    try:
        prepared = descriptor.prepared_run(request)
    except ValueError as exc:
        _send(ErrorMessage(run_id=request.manifest.run_id, error=str(exc)))
        return 2
    _send(PreparedMessage(prepared=prepared))

    start = await _read_command()
    if isinstance(start, ShutdownCommand):
        return 0
    if isinstance(start, CancelCommand):
        _send(
            TerminalMessage(
                run_id=request.manifest.run_id,
                status=start.status,
                reason=start.reason,
                usage=RunnerUsage(),
            )
        )
        return await _await_shutdown(request)
    if not isinstance(start, StartCommand):
        _send(
            ErrorMessage(
                run_id=request.manifest.run_id,
                error=f"{start.kind} received before start",
            )
        )
        return 2
    if start.run_id != request.manifest.run_id:
        _send(
            ErrorMessage(
                run_id=request.manifest.run_id,
                error="start run_id differs from prepared run",
            )
        )
        return 2

    session = WorkflowProtocolSession(descriptor, request)
    tracing = (
        NativeTracingSession(request, instrument_framework)
        if instrument_framework is not None
        else None
    )
    if tracing is not None:
        tracing.start()
    session.start()
    try:
        result = await executor(request, session.request_approval, session)
        terminal_status = await session.finish(result)
        terminal_reason = (
            "native_workflow_approval_rejected"
            if terminal_status is TerminalStatus.APPROVAL_REJECTED
            else "native_workflow_completed"
        )
    except WorkflowBudgetExceeded as exc:
        terminal_status = TerminalStatus.BUDGET_EXHAUSTED
        terminal_reason = exc.reason
        session.fail(terminal_status, terminal_reason)
    except WorkflowTerminated as exc:
        terminal_status = exc.status
        terminal_reason = exc.reason
        session.fail(terminal_status, terminal_reason)
    except EvidenceGateFailed as exc:
        terminal_status = TerminalStatus.RUNNER_FAILURE
        terminal_reason = exc.reason
        session.fail(terminal_status, terminal_reason)
    except asyncio.CancelledError as exc:
        terminal_status = TerminalStatus.CANCELLED
        terminal_reason = str(exc) or "native_workflow_cancelled"
        session.fail(terminal_status, terminal_reason)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        terminal_status = session.classify_unhandled_failure()
        reason_prefix = {
            TerminalStatus.MODEL_FAILURE: "native_model_failed",
            TerminalStatus.TOOL_SERVICE_FAILURE: "native_tool_service_failed",
            TerminalStatus.RUNNER_FAILURE: "native_workflow_failed",
        }[terminal_status]
        terminal_reason = f"{reason_prefix}:{type(exc).__name__}"
        session.fail(terminal_status, terminal_reason)
    if tracing is not None:
        tracing.finish(session.events)
    session.terminal(terminal_status, terminal_reason)
    return await _await_shutdown(request)


def run_protocol_probe(
    descriptor: NativeRunnerDescriptor,
    *,
    instrument_framework: FrameworkInstrumentor | None = None,
) -> int:
    """Serve the lifecycle protocol without invoking a model or incident tool."""

    request: RunRequest | None = None
    started = False
    terminal_sent = False
    usage = RunnerUsage()
    tracing: NativeTracingSession | None = None

    for line in sys.stdin.buffer:
        try:
            command = decode_runner_command(line)
        except Exception as exc:
            _send(ErrorMessage(error=f"invalid command: {type(exc).__name__}"))
            return 2

        if isinstance(command, PrepareCommand):
            if request is not None:
                _send(
                    ErrorMessage(
                        run_id=command.request.manifest.run_id,
                        error="prepare received more than once",
                    )
                )
                continue
            request = command.request
            try:
                prepared = descriptor.prepared_run(request)
            except ValueError as exc:
                _send(
                    ErrorMessage(
                        run_id=request.manifest.run_id,
                        error=str(exc),
                    )
                )
                continue
            _send(PreparedMessage(prepared=prepared))
            continue

        if isinstance(command, ShutdownCommand):
            return 0

        if request is None:
            _send(ErrorMessage(error=f"{command.kind} received before prepare"))
            continue
        if _command_run_id(command) != request.manifest.run_id:
            _send(
                ErrorMessage(
                    run_id=request.manifest.run_id,
                    error=f"{command.kind} run_id differs from prepared run",
                )
            )
            continue

        if isinstance(command, StartCommand):
            if started:
                _send(
                    ErrorMessage(
                        run_id=request.manifest.run_id,
                        error="start received more than once",
                    )
                )
                continue
            started = True
            if instrument_framework is not None:
                tracing = NativeTracingSession(request, instrument_framework)
                tracing.start()
            events = [
                _event(
                    request,
                    sequence=0,
                    kind=EventKind.RUN_STARTED,
                    actor=descriptor.name,
                    payload={"execution_mode": "protocol_probe"},
                ),
                _event(
                    request,
                    sequence=1,
                    kind=EventKind.RUN_FINISHED,
                    actor=descriptor.name,
                    payload={
                        "status": TerminalStatus.COMPLETED.value,
                        "execution_mode": "protocol_probe",
                    },
                ),
            ]
            _send(EventMessage(event=events[0]))
            _send(EventMessage(event=events[1]))
            if tracing is not None:
                tracing.finish(events)
            _send(
                TerminalMessage(
                    run_id=request.manifest.run_id,
                    status=TerminalStatus.COMPLETED,
                    reason="native_protocol_probe_completed",
                    usage=usage,
                )
            )
            terminal_sent = True
        elif isinstance(command, ApprovalCommand):
            _send(
                HeartbeatMessage(
                    run_id=request.manifest.run_id,
                    phase="protocol_probe_approval_ignored",
                )
            )
        elif isinstance(command, CancelCommand) and not terminal_sent:
            if started:
                _send(
                    EventMessage(
                        event=_event(
                            request,
                            sequence=1,
                            kind=EventKind.RUN_FINISHED,
                            actor=descriptor.name,
                            payload={
                                "status": command.status.value,
                                "execution_mode": "protocol_probe",
                            },
                        )
                    )
                )
            _send(
                TerminalMessage(
                    run_id=request.manifest.run_id,
                    status=command.status,
                    reason=command.reason,
                    usage=usage,
                )
            )
            terminal_sent = True
    return 0


def descriptor_payload(descriptor: NativeRunnerDescriptor) -> dict[str, object]:
    """Return JSON-safe metadata for orchestration preflight."""

    return descriptor.model_dump(mode="json")


def _event(
    request: RunRequest,
    *,
    sequence: int,
    kind: EventKind,
    actor: str,
    payload: Mapping[str, object],
) -> NormalizedEvent:
    trace_id, span_id = _current_trace_context()
    return NormalizedEvent(
        run_id=request.manifest.run_id,
        sequence=sequence,
        lane=request.manifest.lane,
        kind=kind,
        actor=actor,
        trace_id=trace_id,
        span_id=span_id,
        payload=dict(payload),
    )


def _command_run_id(
    command: StartCommand | ApprovalCommand | FinalizeCommand | CancelCommand,
):
    return command.run_id


async def _read_command():
    line = await asyncio.to_thread(sys.stdin.buffer.readline)
    if not line:
        raise EOFError("runner command stream closed")
    return decode_runner_command(line)


async def _await_shutdown(request: RunRequest) -> int:
    while True:
        try:
            command = await _read_command()
        except EOFError:
            return 0
        if isinstance(command, ShutdownCommand):
            return 0
        if isinstance(command, CancelCommand):
            continue
        _send(
            ErrorMessage(
                run_id=request.manifest.run_id,
                error=f"{command.kind} received after terminal",
            )
        )


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    path.chmod(0o600)


def _current_trace_context() -> tuple[str | None, str | None]:
    """Read the active OTel context lazily so the core package needs no SDK dependency."""

    try:
        from opentelemetry import trace
    except ImportError:
        return None, None
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}"


def _send(message: BaseModel) -> None:
    sys.stdout.buffer.write(encode_protocol_message(message))
    sys.stdout.buffer.flush()
