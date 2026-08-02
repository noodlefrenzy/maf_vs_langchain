"""Protocol-conformant mock runner used by control-plane lifecycle tests."""

from __future__ import annotations

import argparse
import hashlib
import sys

from agent_lab.contracts import EventKind, NormalizedEvent, TerminalStatus
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


def _send(message) -> None:
    sys.stdout.buffer.write(encode_protocol_message(message))
    sys.stdout.buffer.flush()


def _mock_event(
    request: RunRequest,
    *,
    sequence: int,
    kind: EventKind,
    payload: dict[str, object],
) -> NormalizedEvent:
    return NormalizedEvent(
        run_id=request.manifest.run_id,
        sequence=sequence,
        lane=request.manifest.lane,
        kind=kind,
        actor="mock-runner",
        payload=payload,
    )


def run(mode: str, *, budget_field: str) -> int:
    request: RunRequest | None = None
    terminal_sent = False
    started = False
    usage = RunnerUsage()
    event_sequence = 0

    def emit_event(kind: EventKind, payload: dict[str, object]) -> None:
        nonlocal event_sequence
        if request is None:
            raise RuntimeError("cannot emit an event before prepare")
        _send(
            EventMessage(
                event=_mock_event(
                    request,
                    sequence=event_sequence,
                    kind=kind,
                    payload=payload,
                )
            )
        )
        event_sequence += 1

    for line in sys.stdin.buffer:
        try:
            command = decode_runner_command(line)
        except Exception as exc:
            _send(ErrorMessage(error=f"invalid command: {type(exc).__name__}"))
            return 2

        if isinstance(command, PrepareCommand):
            request = command.request
            topology = hashlib.sha256(f"mock:{request.manifest.lane.value}".encode()).hexdigest()
            _send(
                PreparedMessage(
                    prepared=PreparedRun(
                        run_id=request.manifest.run_id,
                        lane=request.manifest.lane,
                        topology_sha256=topology,
                        effective_prompt_sha256=request.manifest.prompt_bundle_sha256,
                        tool_catalog_sha256=request.manifest.tool_catalog_sha256,
                        framework_versions={"mock-runner": "0.1.0"},
                        checkpoint=CheckpointCapability(
                            resumable=True,
                            restart_safe=True,
                            format="mock-json",
                        ),
                    )
                )
            )
        elif isinstance(command, StartCommand):
            if request is None:
                _send(ErrorMessage(run_id=command.run_id, error="start before prepare"))
                return 2
            if mode == "crash":
                return 17
            started = True
            emit_event(EventKind.RUN_STARTED, {"execution_mode": mode})
            if mode == "oversized_line":
                sys.stdout.buffer.write(b"x" * 4096 + b"\n")
                sys.stdout.buffer.flush()
                continue
            if mode == "budget":
                usage_values = {
                    "model_calls": 0,
                    "tool_calls": 0,
                    "output_tokens": 0,
                    "approval_requests": 0,
                }
                usage_values[budget_field] = getattr(request.manifest.budget, budget_field) + 1
                usage = RunnerUsage(**usage_values)
                _send(
                    UsageMessage(
                        run_id=command.run_id,
                        usage=usage,
                    )
                )
                event_kinds = {
                    "model_calls": EventKind.MODEL_REQUESTED,
                    "tool_calls": EventKind.TOOL_REQUESTED,
                    "output_tokens": EventKind.MODEL_RESPONDED,
                    "approval_requests": EventKind.APPROVAL_REQUESTED,
                }
                event_kind = event_kinds[budget_field]
                event_count = usage_values[budget_field]
                for index in range(event_count if budget_field != "output_tokens" else 1):
                    payload: dict[str, object] = {"index": index}
                    if budget_field == "output_tokens":
                        payload["output_tokens"] = event_count
                    emit_event(event_kind, payload)
                continue
            if mode == "model_timeout":
                usage = RunnerUsage(model_calls=1)
                _send(UsageMessage(run_id=command.run_id, usage=usage))
                emit_event(EventKind.MODEL_REQUESTED, {"model": "local-test"})
                emit_event(EventKind.ERROR, {"reason": "model_request_timeout"})
                emit_event(
                    EventKind.RUN_FINISHED,
                    {
                        "status": TerminalStatus.MODEL_FAILURE.value,
                        "execution_mode": mode,
                    },
                )
                _send(
                    TerminalMessage(
                        run_id=command.run_id,
                        status=TerminalStatus.MODEL_FAILURE,
                        reason="model_request_timeout",
                        usage=usage,
                    )
                )
                terminal_sent = True
                continue
            if mode in {"timeout", "approval_timeout", "cancellation"}:
                _send(
                    HeartbeatMessage(
                        run_id=command.run_id,
                        phase=("awaiting_approval" if mode == "approval_timeout" else "running"),
                    )
                )
                continue
            if mode == "approval_success":
                usage = RunnerUsage(approval_requests=1)
                _send(UsageMessage(run_id=command.run_id, usage=usage))
                emit_event(
                    EventKind.APPROVAL_REQUESTED,
                    {
                        "request_id": "mit-control-plane-01",
                        "proposed_action": "rollback checkout-api",
                    },
                )
                _send(
                    HeartbeatMessage(
                        run_id=command.run_id,
                        phase="awaiting_approval",
                    )
                )
                continue
            usage = RunnerUsage(
                model_calls=1,
                tool_calls=2,
                output_tokens=32,
                approval_requests=1,
            )
            _send(UsageMessage(run_id=command.run_id, usage=usage))
            emit_event(
                EventKind.RUN_FINISHED,
                {
                    "status": TerminalStatus.COMPLETED.value,
                    "execution_mode": mode,
                },
            )
            _send(
                TerminalMessage(
                    run_id=command.run_id,
                    status=TerminalStatus.COMPLETED,
                    reason="mock_runner_completed",
                    usage=usage,
                )
            )
            terminal_sent = True
        elif isinstance(command, ApprovalCommand):
            if request is None:
                _send(ErrorMessage(run_id=command.run_id, error="approval before prepare"))
                return 2
            if mode != "approval_success":
                _send(
                    HeartbeatMessage(
                        run_id=command.run_id,
                        phase="unexpected_approval",
                    )
                )
                continue
            emit_event(
                EventKind.APPROVAL_DECIDED,
                {
                    "request_id": command.request_id,
                    "decision": command.decision.value,
                    "action_to_execute": command.action_to_execute,
                    "decided_by": command.decided_by,
                    "decided_tick": command.decided_tick,
                },
            )
            emit_event(
                EventKind.RUN_FINISHED,
                {
                    "status": TerminalStatus.COMPLETED.value,
                    "execution_mode": "approval_success",
                },
            )
            _send(
                TerminalMessage(
                    run_id=command.run_id,
                    status=TerminalStatus.COMPLETED,
                    reason="mock_approval_completed",
                    usage=usage,
                )
            )
            terminal_sent = True
        elif isinstance(command, CancelCommand):
            if not terminal_sent:
                if started:
                    emit_event(EventKind.ERROR, {"reason": command.reason})
                    emit_event(
                        EventKind.RUN_FINISHED,
                        {
                            "status": command.status.value,
                            "execution_mode": mode,
                        },
                    )
                _send(
                    TerminalMessage(
                        run_id=command.run_id,
                        status=command.status,
                        reason=command.reason,
                        usage=usage,
                    )
                )
                terminal_sent = True
        elif isinstance(command, ShutdownCommand):
            return 0
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=[
            "success",
            "timeout",
            "approval_timeout",
            "model_timeout",
            "crash",
            "cancellation",
            "oversized_line",
            "budget",
            "approval_success",
        ],
        default="success",
    )
    parser.add_argument(
        "--budget-field",
        choices=["model_calls", "tool_calls", "output_tokens", "approval_requests"],
        default="model_calls",
    )
    args = parser.parse_args()
    raise SystemExit(run(args.mode, budget_field=args.budget_field))


if __name__ == "__main__":
    main()
