"""Framework-neutral runner process lifecycle and outer budget enforcement."""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_lab.contracts import FrameworkLane, RunManifest, RunTerminal, TerminalStatus
from agent_lab.run_bundle import RunBundle
from agent_lab.runner_protocol import (
    CancelCommand,
    ErrorMessage,
    EventMessage,
    HeartbeatMessage,
    PrepareCommand,
    PreparedMessage,
    PreparedRun,
    RunnerUsage,
    RunPaths,
    RunRequest,
    ShutdownCommand,
    StartCommand,
    TerminalMessage,
    UsageMessage,
    decode_runner_message,
    encode_protocol_message,
    redacted_request_payload,
)


class LaneOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_seed: int
    repetition: int = Field(ge=0)
    lanes: tuple[FrameworkLane, ...]


class RunnerProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal: RunTerminal
    prepared: PreparedRun | None
    paths: RunPaths
    graceful_shutdown: bool
    stderr_path: Path


def resolve_lane_order(
    lanes: Sequence[FrameworkLane],
    *,
    policy: str,
    order_seed: int,
    repetition: int,
) -> LaneOrder:
    """Resolve and record a deterministic lane order for one repetition."""

    if not lanes:
        raise ValueError("at least one lane is required")
    if len(set(lanes)) != len(lanes):
        raise ValueError("lane order input contains duplicates")
    ordered = list(lanes)
    if policy == "randomized":
        random.Random(f"{order_seed}:{repetition}").shuffle(ordered)
    elif policy == "alternating":
        if (order_seed + repetition) % 2:
            ordered.reverse()
    elif policy != "fixed":
        raise ValueError(f"unsupported lane order policy {policy!r}")
    return LaneOrder(
        order_seed=order_seed,
        repetition=repetition,
        lanes=tuple(ordered),
    )


class ControlPlane:
    """Launch one runner in an isolated directory and fail closed on limits."""

    def __init__(
        self,
        runs_root: Path,
        *,
        graceful_shutdown_seconds: float = 1.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if graceful_shutdown_seconds <= 0:
            raise ValueError("graceful_shutdown_seconds must be positive")
        self.runs_root = runs_root.resolve()
        self.graceful_shutdown_seconds = graceful_shutdown_seconds
        self.environment = dict(environment) if environment is not None else _safe_environment()

    def allocate_run(self, manifest: RunManifest) -> tuple[RunBundle, RunPaths]:
        """Create all per-run paths exclusively before launching a process."""

        bundle = RunBundle.create(self.runs_root, manifest)
        workspace = bundle.path / "workspace"
        world = bundle.path / "world"
        state = bundle.path / "state"
        traces = bundle.path / "native" / "traces"
        workspace.mkdir()
        world.mkdir(mode=0o700)
        state.mkdir()
        traces.mkdir()
        paths = RunPaths(
            run_root=bundle.path.resolve(),
            bundle=bundle.path.resolve(),
            workspace=workspace.resolve(),
            checkpoints=(bundle.path / "checkpoints").resolve(),
            state=state.resolve(),
            traces=traces.resolve(),
        )
        return bundle, paths

    async def execute(
        self,
        command: Sequence[str],
        request: RunRequest,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> RunnerProcessResult:
        """Run the JSON-lines lifecycle until one documented terminal state."""

        if not command:
            raise ValueError("runner command must not be empty")
        bundle = RunBundle(request.paths.bundle, request.manifest)
        if request.paths.bundle != request.paths.run_root:
            raise ValueError("bundle and run_root must identify the allocated run directory")
        if not (request.paths.bundle / "manifest.json").is_file():
            raise ValueError("run bundle must be allocated before execution")

        started = time.monotonic()
        native_path = request.paths.bundle / "native"
        request_path = native_path / "run-request.redacted.json"
        request_path.write_text(
            json.dumps(redacted_request_payload(request), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=request.paths.workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **self.environment,
                    "AGENT_LAB_RUN_ID": str(request.manifest.run_id),
                    "AGENT_LAB_WORKSPACE": str(request.paths.workspace),
                    "AGENT_LAB_CHECKPOINTS": str(request.paths.checkpoints),
                    "AGENT_LAB_STATE": str(request.paths.state),
                    "AGENT_LAB_TRACES": str(request.paths.traces),
                    "PYTHONUNBUFFERED": "1",
                },
            )
        except OSError as exc:
            stderr_path = native_path / "runner.stderr.log"
            stderr_path.write_text(
                f"runner launch failed: {type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            terminal = RunTerminal(
                run_id=request.manifest.run_id,
                status=TerminalStatus.RUNNER_FAILURE,
                reason=f"runner_launch_failed:{type(exc).__name__}",
                used_model_calls=0,
                used_tool_calls=0,
                used_output_tokens=0,
                elapsed_seconds=time.monotonic() - started,
            )
            bundle.write_terminal(terminal)
            return RunnerProcessResult(
                terminal=terminal,
                prepared=None,
                paths=request.paths,
                graceful_shutdown=True,
                stderr_path=stderr_path,
            )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("runner process pipes were not created")
        stderr_task = asyncio.create_task(process.stderr.read())

        prepared: PreparedRun | None = None
        usage = RunnerUsage()
        terminal_status: TerminalStatus | None = None
        terminal_reason = ""
        expected_event_sequence = 0
        awaiting_approval = False
        deadline = started + request.manifest.budget.wall_clock_seconds

        await self._send(process, PrepareCommand(request=request))
        try:
            while terminal_status is None:
                wait_result, line = await self._next_line(
                    process,
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                if wait_result == "timeout":
                    if awaiting_approval:
                        terminal_status = TerminalStatus.APPROVAL_TIMEOUT
                        terminal_reason = "approval_deadline_exhausted"
                    else:
                        terminal_status = TerminalStatus.BUDGET_EXHAUSTED
                        terminal_reason = "wall_clock_budget_exhausted"
                    break
                if wait_result == "cancelled":
                    terminal_status = TerminalStatus.CANCELLED
                    terminal_reason = "control_plane_cancelled"
                    break
                if not line:
                    return_code = await process.wait()
                    terminal_status = TerminalStatus.RUNNER_FAILURE
                    terminal_reason = f"runner_exited_without_terminal:{return_code}"
                    break

                try:
                    message = decode_runner_message(line)
                except Exception as exc:
                    terminal_status = TerminalStatus.RUNNER_FAILURE
                    terminal_reason = f"invalid_runner_message:{type(exc).__name__}"
                    break

                message_run_id = _message_run_id(message)
                if message_run_id is not None and message_run_id != request.manifest.run_id:
                    terminal_status = TerminalStatus.RUNNER_FAILURE
                    terminal_reason = "runner_message_run_id_mismatch"
                    break

                if isinstance(message, PreparedMessage):
                    if prepared is not None:
                        terminal_status = TerminalStatus.RUNNER_FAILURE
                        terminal_reason = "runner_prepared_more_than_once"
                        break
                    prepared = message.prepared
                    if (
                        prepared.run_id != request.manifest.run_id
                        or prepared.lane is not request.manifest.lane
                        or prepared.effective_prompt_sha256 != request.manifest.prompt_bundle_sha256
                        or prepared.tool_catalog_sha256 != request.manifest.tool_catalog_sha256
                    ):
                        terminal_status = TerminalStatus.INVALID_PROVENANCE
                        terminal_reason = "prepared_run_provenance_mismatch"
                        break
                    (native_path / "prepared-run.json").write_text(
                        prepared.model_dump_json(indent=2) + "\n",
                        encoding="utf-8",
                    )
                    await self._send(
                        process,
                        StartCommand(run_id=request.manifest.run_id),
                    )
                elif isinstance(message, UsageMessage):
                    if prepared is None:
                        terminal_status = TerminalStatus.RUNNER_FAILURE
                        terminal_reason = "runner_usage_before_prepare"
                        break
                    violation = _usage_violation(usage, message.usage, request.manifest)
                    if violation:
                        terminal_status = TerminalStatus.BUDGET_EXHAUSTED
                        terminal_reason = violation
                        usage = message.usage
                        break
                    usage = message.usage
                elif isinstance(message, EventMessage):
                    if prepared is None:
                        terminal_status = TerminalStatus.RUNNER_FAILURE
                        terminal_reason = "runner_event_before_prepare"
                        break
                    event = message.event
                    if (
                        event.run_id != request.manifest.run_id
                        or event.lane is not request.manifest.lane
                        or event.sequence != expected_event_sequence
                    ):
                        terminal_status = TerminalStatus.RUNNER_FAILURE
                        terminal_reason = "invalid_normalized_event_sequence_or_identity"
                        break
                    with (request.paths.bundle / "events.jsonl").open(
                        "a",
                        encoding="utf-8",
                    ) as handle:
                        handle.write(event.model_dump_json() + "\n")
                    expected_event_sequence += 1
                    if event.kind.value == "approval.requested":
                        awaiting_approval = True
                    elif event.kind.value == "approval.decided":
                        awaiting_approval = False
                elif isinstance(message, TerminalMessage):
                    if prepared is None:
                        terminal_status = TerminalStatus.RUNNER_FAILURE
                        terminal_reason = "runner_terminal_before_prepare"
                        break
                    violation = _usage_violation(usage, message.usage, request.manifest)
                    usage = message.usage
                    if violation:
                        terminal_status = TerminalStatus.BUDGET_EXHAUSTED
                        terminal_reason = violation
                    else:
                        terminal_status = message.status
                        terminal_reason = message.reason
                elif isinstance(message, ErrorMessage):
                    terminal_status = TerminalStatus.RUNNER_FAILURE
                    terminal_reason = f"runner_protocol_error:{message.error}"
                elif isinstance(message, HeartbeatMessage):
                    if message.phase == "awaiting_approval":
                        awaiting_approval = True
                    continue

            graceful = await self._stop_process(
                process,
                run_id=request.manifest.run_id,
                cancel_reason=(
                    None
                    if terminal_status
                    in {
                        TerminalStatus.COMPLETED,
                        TerminalStatus.APPROVAL_REJECTED,
                    }
                    else terminal_reason
                ),
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

        stderr = await stderr_task
        stderr_path = native_path / "runner.stderr.log"
        stderr_path.write_bytes(stderr)
        elapsed = time.monotonic() - started
        terminal = RunTerminal(
            run_id=request.manifest.run_id,
            status=terminal_status or TerminalStatus.RUNNER_FAILURE,
            reason=terminal_reason or "runner_stopped_without_reason",
            used_model_calls=usage.model_calls,
            used_tool_calls=usage.tool_calls,
            used_output_tokens=usage.output_tokens,
            elapsed_seconds=elapsed,
        )
        bundle.write_terminal(terminal)
        return RunnerProcessResult(
            terminal=terminal,
            prepared=prepared,
            paths=request.paths,
            graceful_shutdown=graceful,
            stderr_path=stderr_path,
        )

    async def _next_line(
        self,
        process: asyncio.subprocess.Process,
        *,
        deadline: float,
        cancel_event: asyncio.Event | None,
    ) -> tuple[str, bytes]:
        assert process.stdout is not None
        remaining = max(0.0, deadline - time.monotonic())
        line_task = asyncio.create_task(process.stdout.readline())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        tasks = {line_task}
        if cancel_task is not None:
            tasks.add(cancel_task)
        done, pending = await asyncio.wait(
            tasks,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if not done:
            return "timeout", b""
        if cancel_task is not None and cancel_task in done and cancel_task.result():
            line_task.cancel()
            return "cancelled", b""
        return "line", line_task.result()

    async def _send(self, process: asyncio.subprocess.Process, command: BaseModel) -> None:
        if process.stdin is None or process.stdin.is_closing():
            return
        process.stdin.write(encode_protocol_message(command))
        await process.stdin.drain()

    async def _stop_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        run_id,
        cancel_reason: str | None,
    ) -> bool:
        if process.returncode is not None:
            return True
        if cancel_reason is not None:
            await self._send(
                process,
                CancelCommand(run_id=run_id, reason=cancel_reason),
            )
        await self._send(process, ShutdownCommand(run_id=run_id))
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.graceful_shutdown_seconds,
            )
            return True
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.graceful_shutdown_seconds,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
            return False


def _usage_violation(
    previous: RunnerUsage,
    observed: RunnerUsage,
    manifest: RunManifest,
) -> str | None:
    if not observed.is_monotonic_from(previous):
        return "runner_usage_counters_regressed"
    limits = manifest.budget
    checks = (
        ("model_call_budget_exhausted", observed.model_calls, limits.model_calls),
        ("tool_call_budget_exhausted", observed.tool_calls, limits.tool_calls),
        ("output_token_budget_exhausted", observed.output_tokens, limits.output_tokens),
        (
            "approval_request_budget_exhausted",
            observed.approval_requests,
            limits.approval_requests,
        ),
    )
    return next((reason for reason, used, limit in checks if used > limit), None)


def _message_run_id(message) -> object | None:
    if isinstance(message, PreparedMessage):
        return message.prepared.run_id
    if isinstance(
        message,
        (UsageMessage, HeartbeatMessage, TerminalMessage, ErrorMessage),
    ):
        return message.run_id
    if isinstance(message, EventMessage):
        return message.event.run_id
    return None


def _safe_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "WINDIR",
    )
    return {name: os.environ[name] for name in allowed if name in os.environ}
