"""Reusable protocol loop for framework-native runner process entry points."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_lab.contracts import (
    ApprovalRequest,
    EventKind,
    EventVisibility,
    FrameworkLane,
    NormalizedEvent,
    TerminalStatus,
)
from agent_lab.runner_protocol import (
    ApprovalCommand,
    CancelCommand,
    CheckpointCapability,
    ErrorMessage,
    EventMessage,
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


class WorkflowProtocolSession(WorkflowTelemetry):
    """Emit one native workflow execution through the normalized protocol."""

    def __init__(self, descriptor: NativeRunnerDescriptor, request: RunRequest) -> None:
        self.descriptor = descriptor
        self.request = request
        self.sequence = 0
        self.usage = RunnerUsage()
        self._model_events: dict[str, NormalizedEvent] = {}
        self._tool_events: dict[tuple[str, str], list[NormalizedEvent]] = {}

    def start(self) -> None:
        self.emit(
            EventKind.RUN_STARTED,
            actor=self.descriptor.name,
            payload={"execution_mode": "native_workflow"},
        )

    async def model_started(self, *, actor: str, model: str) -> None:
        self._advance_usage("model_calls", 1, "model_call_budget_exhausted")
        event = self.emit(
            EventKind.MODEL_REQUESTED,
            actor=actor,
            payload={"model": model},
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
        self._advance_usage(
            "output_tokens",
            output_tokens,
            "output_token_budget_exhausted",
        )
        parent = self._model_events.pop(actor, None)
        self.emit(
            EventKind.MODEL_RESPONDED,
            actor=actor,
            parent_event_id=parent.event_id if parent is not None else None,
            payload={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    async def tool_started(self, *, actor: str, tool_name: str) -> None:
        self._advance_usage("tool_calls", 1, "tool_call_budget_exhausted")
        requested = self.emit(
            EventKind.TOOL_REQUESTED,
            actor=actor,
            payload={"tool_name": tool_name},
        )
        self.emit(
            EventKind.TOOL_STARTED,
            actor=actor,
            parent_event_id=requested.event_id,
            payload={"tool_name": tool_name},
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
            payload={"tool_name": tool_name},
        )

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
            raise asyncio.CancelledError(command.reason)
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
                "action_to_execute": command.action_to_execute,
            },
        )
        return {
            "request_id": command.request_id,
            "decision": command.decision.value,
            "action_to_execute": command.action_to_execute,
            "approval_token": command.approval_token,
        }

    def finish(self, result: dict[str, Any]) -> TerminalStatus:
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
        self.emit(
            EventKind.RUN_FINISHED,
            actor=self.descriptor.name,
            payload={"status": status.value, "execution_mode": "native_workflow"},
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
        event = NormalizedEvent(
            run_id=self.request.manifest.run_id,
            sequence=self.sequence,
            lane=self.request.manifest.lane,
            kind=kind,
            actor=actor,
            parent_event_id=parent_event_id,
            visibility=visibility,
            payload=payload,
        )
        self.sequence += 1
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
        self.usage = self.usage.model_copy(update={field: observed})
        _send(UsageMessage(run_id=self.request.manifest.run_id, usage=self.usage))
        if observed > getattr(self.request.manifest.budget, field):
            raise WorkflowBudgetExceeded(reason)


class NativeRunnerDescriptor(BaseModel):
    """Static native metadata captured before a framework run starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    supported_lanes: tuple[FrameworkLane, ...]
    framework_versions: dict[str, str]
    topology: dict[FrameworkLane, dict[str, object]]
    checkpoint: dict[FrameworkLane, CheckpointCapability]

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
            framework_versions=self.framework_versions,
            checkpoint=self.checkpoint[lane],
        )


async def run_workflow_protocol(
    descriptor: NativeRunnerDescriptor,
    executor: WorkflowProtocolExecutor,
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
    session.start()
    try:
        result = await executor(request, session.request_approval, session)
        terminal_status = session.finish(result)
        terminal_reason = (
            "native_workflow_approval_rejected"
            if terminal_status is TerminalStatus.APPROVAL_REJECTED
            else "native_workflow_completed"
        )
    except WorkflowBudgetExceeded as exc:
        terminal_status = TerminalStatus.BUDGET_EXHAUSTED
        terminal_reason = exc.reason
        session.fail(terminal_status, terminal_reason)
    except asyncio.CancelledError as exc:
        terminal_status = TerminalStatus.CANCELLED
        terminal_reason = str(exc) or "native_workflow_cancelled"
        session.fail(terminal_status, terminal_reason)
    except Exception as exc:
        terminal_status = TerminalStatus.RUNNER_FAILURE
        terminal_reason = f"native_workflow_failed:{type(exc).__name__}"
        session.fail(terminal_status, terminal_reason)
    session.terminal(terminal_status, terminal_reason)
    return await _await_shutdown(request)


def run_protocol_probe(descriptor: NativeRunnerDescriptor) -> int:
    """Serve the lifecycle protocol without invoking a model or incident tool."""

    request: RunRequest | None = None
    started = False
    terminal_sent = False
    usage = RunnerUsage()

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
            _send(
                EventMessage(
                    event=_event(
                        request,
                        sequence=0,
                        kind=EventKind.RUN_STARTED,
                        actor=descriptor.name,
                        payload={"execution_mode": "protocol_probe"},
                    )
                )
            )
            _send(
                EventMessage(
                    event=_event(
                        request,
                        sequence=1,
                        kind=EventKind.RUN_FINISHED,
                        actor=descriptor.name,
                        payload={
                            "status": TerminalStatus.COMPLETED.value,
                            "execution_mode": "protocol_probe",
                        },
                    )
                )
            )
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
                                "status": TerminalStatus.CANCELLED.value,
                                "execution_mode": "protocol_probe",
                            },
                        )
                    )
                )
            _send(
                TerminalMessage(
                    run_id=request.manifest.run_id,
                    status=TerminalStatus.CANCELLED,
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
    return NormalizedEvent(
        run_id=request.manifest.run_id,
        sequence=sequence,
        lane=request.manifest.lane,
        kind=kind,
        actor=actor,
        payload=dict(payload),
    )


def _command_run_id(
    command: StartCommand | ApprovalCommand | CancelCommand,
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


def _send(message: BaseModel) -> None:
    sys.stdout.buffer.write(encode_protocol_message(message))
    sys.stdout.buffer.flush()
