import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_lab.contracts import (
    AgentRole,
    ApprovalDecision,
    ApprovalRecord,
    Budget,
    FrameworkLane,
    ModelProfile,
    RunManifest,
    TerminalStatus,
)
from agent_lab.control_plane import ControlPlane, resolve_lane_order
from agent_lab.runner_protocol import (
    PrepareCommand,
    RoleEndpoint,
    RunRequest,
    encode_protocol_message,
    redacted_request_payload,
)


def _manifest(*, wall_clock_seconds: int = 5) -> RunManifest:
    return RunManifest(
        experiment_id="control_plane_test",
        lane=FrameworkLane.MAF_WORKFLOW,
        scenario="incident_response",
        scenario_generator_version="0.1.0",
        scenario_seed=42,
        repetition=0,
        model=ModelProfile(
            name="local-test",
            base_url="http://127.0.0.1:8000/v1",
            temperature=0.6,
            top_p=0.95,
            max_output_tokens=1000,
            context_window_tokens=8192,
            request_timeout_seconds=60,
        ),
        budget=Budget(
            wall_clock_seconds=wall_clock_seconds,
            model_calls=2,
            tool_calls=4,
            output_tokens=100,
            approval_requests=1,
        ),
        source_commit="0" * 40,
        source_tree_sha256="5" * 64,
        framework_versions={"mock-runner": "0.1.0"},
        dependency_lock_sha256="4" * 64,
        prompt_bundle_sha256="1" * 64,
        tool_catalog_sha256="2" * 64,
        world_manifest_sha256="3" * 64,
    )


def _request(
    control_plane: ControlPlane,
    manifest: RunManifest,
) -> RunRequest:
    _bundle, paths = control_plane.allocate_run(manifest)
    return RunRequest(
        manifest=manifest,
        paths=paths,
        role_endpoints={
            AgentRole.INCIDENT_COMMANDER: RoleEndpoint(
                role=AgentRole.INCIDENT_COMMANDER,
                url="http://127.0.0.1:8765/runs/test/commander/mcp",
                token="test-secret-token",
                expires_at=2_000_000_000,
            )
        },
        public_ticket="A seeded checkout incident requires investigation.",
        order_seed=17,
    )


def _command(mode: str, *, budget_field: str | None = None) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_lab.mock_runner",
        "--mode",
        mode,
    ]
    if budget_field is not None:
        command.extend(["--budget-field", budget_field])
    return command


def test_mock_runner_success_and_isolated_paths(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(control_plane.execute(_command("success"), request))

    assert result.terminal.status is TerminalStatus.COMPLETED
    assert result.terminal.used_model_calls == 1
    assert result.graceful_shutdown is True
    assert result.prepared is not None
    assert result.prepared.tool_catalog_sha256 == request.manifest.tool_catalog_sha256
    assert (result.paths.run_root / "world").is_dir()
    assert result.paths.workspace.is_dir()
    assert result.paths.checkpoints.is_dir()
    assert result.paths.state.is_dir()
    assert result.paths.traces.is_dir()
    terminal = json.loads((result.paths.bundle / "terminal.json").read_text())
    assert terminal["status"] == "completed"
    assert result.stderr_path.read_text() == ""
    prepared = json.loads((result.paths.bundle / "native" / "prepared-run.json").read_text())
    redacted_request = (result.paths.bundle / "native" / "run-request.redacted.json").read_text()
    assert prepared["tool_catalog_sha256"] == request.manifest.tool_catalog_sha256
    assert "test-secret-token" not in redacted_request
    assert '"token": "[REDACTED]"' in redacted_request


def test_mock_runner_wall_timeout_is_budget_exhaustion(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path, graceful_shutdown_seconds=0.5)
    request = _request(control_plane, _manifest(wall_clock_seconds=1))

    result = asyncio.run(control_plane.execute(_command("timeout"), request))

    assert result.terminal.status is TerminalStatus.BUDGET_EXHAUSTED
    assert result.terminal.reason == "wall_clock_budget_exhausted"
    assert result.graceful_shutdown is True


def test_mock_runner_approval_timeout_has_distinct_terminal_state(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path, graceful_shutdown_seconds=0.5)
    request = _request(control_plane, _manifest(wall_clock_seconds=1))

    result = asyncio.run(control_plane.execute(_command("approval_timeout"), request))

    assert result.terminal.status is TerminalStatus.APPROVAL_TIMEOUT
    assert result.terminal.reason == "approval_deadline_exhausted"


def test_control_plane_delivers_token_bound_approval_without_persisting_secret(
    tmp_path: Path,
) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())
    approval_token = "approval-secret-that-must-not-be-persisted"

    async def approve(event):
        assert event.payload["request_id"] == "mit-control-plane-01"
        return ApprovalRecord(
            request_id="mit-control-plane-01",
            case_id="case-control-plane",
            decision=ApprovalDecision.EDIT,
            decided_by="control-plane-test-reviewer",
            decided_tick=7,
            action_to_execute="rollback checkout-api and cap retries",
            approval_token=approval_token,
        )

    result = asyncio.run(
        control_plane.execute(
            _command("approval_success"),
            request,
            approval_handler=approve,
        )
    )

    assert result.terminal.status is TerminalStatus.COMPLETED
    assert result.terminal.reason == "mock_approval_completed"
    events_text = (result.paths.bundle / "events.jsonl").read_text()
    assert approval_token not in events_text
    events = [json.loads(line) for line in events_text.splitlines()]
    assert [event["kind"] for event in events] == [
        "run.started",
        "approval.requested",
        "approval.decided",
        "run.finished",
    ]
    assert events[2]["payload"] == {
        "action_to_execute": "rollback checkout-api and cap retries",
        "decided_by": "control-plane-test-reviewer",
        "decided_tick": 7,
        "decision": "edit",
        "request_id": "mit-control-plane-01",
    }
    assert approval_token not in result.stderr_path.read_text()


def test_control_plane_rejects_mismatched_approval_record(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    async def mismatched_approval(_event):
        return ApprovalRecord(
            request_id="different-request",
            case_id="case-control-plane",
            decision=ApprovalDecision.REJECT,
            decided_by="control-plane-test-reviewer",
            decided_tick=7,
        )

    result = asyncio.run(
        control_plane.execute(
            _command("approval_success"),
            request,
            approval_handler=mismatched_approval,
        )
    )

    assert result.terminal.status is TerminalStatus.RUNNER_FAILURE
    assert result.terminal.reason == "approval_handler_failed:ValueError"


def test_mock_runner_crash_is_runner_failure(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(control_plane.execute(_command("crash"), request))

    assert result.terminal.status is TerminalStatus.RUNNER_FAILURE
    assert result.terminal.reason == "runner_exited_without_terminal:17"


def test_runner_launch_failure_is_terminalized(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(
        control_plane.execute(
            [str(tmp_path / "missing-runner-executable")],
            request,
        )
    )

    assert result.terminal.status is TerminalStatus.RUNNER_FAILURE
    assert result.terminal.reason == "runner_launch_failed:FileNotFoundError"
    assert (result.paths.bundle / "terminal.json").is_file()


def test_mock_runner_external_cancellation(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())
    cancel_event = asyncio.Event()

    async def execute_and_cancel():
        task = asyncio.create_task(
            control_plane.execute(
                _command("cancellation"),
                request,
                cancel_event=cancel_event,
            )
        )
        await asyncio.sleep(0.05)
        cancel_event.set()
        return await task

    result = asyncio.run(execute_and_cancel())

    assert result.terminal.status is TerminalStatus.CANCELLED
    assert result.terminal.reason == "control_plane_cancelled"
    assert result.graceful_shutdown is True


@pytest.mark.parametrize(
    ("field", "reason", "used"),
    [
        ("model_calls", "model_call_budget_exhausted", 3),
        ("tool_calls", "tool_call_budget_exhausted", 5),
        ("output_tokens", "output_token_budget_exhausted", 101),
        ("approval_requests", "approval_request_budget_exhausted", 2),
    ],
)
def test_mock_runner_usage_limits_fail_closed(
    tmp_path: Path,
    field: str,
    reason: str,
    used: int,
) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(
        control_plane.execute(
            _command("budget", budget_field=field),
            request,
        )
    )

    assert result.terminal.status is TerminalStatus.BUDGET_EXHAUSTED
    assert result.terminal.reason == reason
    if field == "model_calls":
        assert result.terminal.used_model_calls == used
    elif field == "tool_calls":
        assert result.terminal.used_tool_calls == used
    elif field == "output_tokens":
        assert result.terminal.used_output_tokens == used


def test_lane_order_is_recorded_and_deterministic() -> None:
    lanes = [FrameworkLane.MAF_WORKFLOW, FrameworkLane.LANGGRAPH]

    first = resolve_lane_order(
        lanes,
        policy="randomized",
        order_seed=20260731,
        repetition=2,
    )
    second = resolve_lane_order(
        lanes,
        policy="randomized",
        order_seed=20260731,
        repetition=2,
    )

    assert first == second
    assert set(first.lanes) == set(lanes)
    assert first.order_seed == 20260731


def test_request_redaction_and_both_runner_environments(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())
    command = PrepareCommand(request=request)
    encoded = encode_protocol_message(command)
    script = (
        "import sys;"
        "from agent_lab.runner_protocol import decode_runner_command;"
        "command=decode_runner_command(sys.stdin.buffer.readline());"
        "print(command.request.manifest.lane.value)"
    )

    assert "test-secret-token" not in json.dumps(redacted_request_payload(request))
    for project in ("runners/maf", "runners/langgraph"):
        result = subprocess.run(
            ["uv", "run", "--project", project, "python", "-c", script],
            input=encoded,
            capture_output=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert result.stdout.decode().strip() == FrameworkLane.MAF_WORKFLOW.value
