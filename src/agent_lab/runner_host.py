"""Reusable protocol loop for framework-native runner process entry points."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from agent_lab.contracts import EventKind, FrameworkLane, NormalizedEvent, TerminalStatus
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
    decode_runner_command,
    encode_protocol_message,
)


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


def _send(message: BaseModel) -> None:
    sys.stdout.buffer.write(encode_protocol_message(message))
    sys.stdout.buffer.flush()
