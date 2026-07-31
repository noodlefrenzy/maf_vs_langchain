"""Protocol-conformant mock runner used by control-plane lifecycle tests."""

from __future__ import annotations

import argparse
import hashlib
import sys

from agent_lab.contracts import TerminalStatus
from agent_lab.runner_protocol import (
    CancelCommand,
    CheckpointCapability,
    ErrorMessage,
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


def run(mode: str, *, budget_field: str) -> int:
    request: RunRequest | None = None
    terminal_sent = False
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
            if mode == "budget":
                usage_values = {
                    "model_calls": 0,
                    "tool_calls": 0,
                    "output_tokens": 0,
                    "approval_requests": 0,
                }
                usage_values[budget_field] = getattr(request.manifest.budget, budget_field) + 1
                _send(
                    UsageMessage(
                        run_id=command.run_id,
                        usage=RunnerUsage(**usage_values),
                    )
                )
                continue
            if mode in {"timeout", "approval_timeout", "cancellation"}:
                _send(
                    HeartbeatMessage(
                        run_id=command.run_id,
                        phase=("awaiting_approval" if mode == "approval_timeout" else "running"),
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
            _send(
                TerminalMessage(
                    run_id=command.run_id,
                    status=TerminalStatus.COMPLETED,
                    reason="mock_runner_completed",
                    usage=usage,
                )
            )
            terminal_sent = True
        elif isinstance(command, CancelCommand):
            if not terminal_sent:
                _send(
                    TerminalMessage(
                        run_id=command.run_id,
                        status=TerminalStatus.CANCELLED,
                        reason=command.reason,
                        usage=RunnerUsage(),
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
            "crash",
            "cancellation",
            "budget",
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
