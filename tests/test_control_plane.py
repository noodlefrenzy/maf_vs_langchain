import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_lab.bundle_validation import validate_bundle
from agent_lab.contract_bundle import sha256_payload
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
from agent_lab.model_request_policy import effective_request_policy
from agent_lab.run_bundle import RunBundle
from agent_lab.runner_protocol import (
    FinalizationResult,
    FinalizedArtifact,
    PrepareCommand,
    RoleEndpoint,
    RunRequest,
    encode_protocol_message,
    redacted_request_payload,
)
from agent_lab.scenario import MitigationActionId


def _manifest(*, wall_clock_seconds: int = 5) -> RunManifest:
    model = ModelProfile(
        name="local-test",
        base_url="http://127.0.0.1:8000/v1",
        temperature=0.6,
        top_p=0.95,
        max_output_tokens=1000,
        context_window_tokens=8192,
        request_timeout_seconds=60,
    )
    return RunManifest(
        experiment_id="control_plane_test",
        lane=FrameworkLane.MAF_WORKFLOW,
        scenario="incident_response",
        scenario_generator_version="0.1.0",
        scenario_seed=42,
        repetition=0,
        model=model,
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
        effective_request_policy_sha256=effective_request_policy(
            model,
            FrameworkLane.MAF_WORKFLOW,
        )["policy_sha256"],
        execution_provenance_sha256="7" * 64,
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


def _assert_bundle_valid(result) -> None:
    validation = validate_bundle(result.paths.bundle)
    assert validation.valid, [
        (finding.level.value, finding.code, finding.message) for finding in validation.findings
    ]


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
    _assert_bundle_valid(result)


def test_mock_runner_approval_timeout_has_distinct_terminal_state(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path, graceful_shutdown_seconds=0.5)
    request = _request(control_plane, _manifest(wall_clock_seconds=1))

    result = asyncio.run(control_plane.execute(_command("approval_timeout"), request))

    assert result.terminal.status is TerminalStatus.APPROVAL_TIMEOUT
    assert result.terminal.reason == "approval_deadline_exhausted"
    _assert_bundle_valid(result)


def test_mock_runner_model_timeout_is_model_failure(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(control_plane.execute(_command("model_timeout"), request))

    assert result.terminal.status is TerminalStatus.MODEL_FAILURE
    assert result.terminal.reason == "model_request_timeout"
    assert result.terminal.used_model_calls == 1
    assert result.graceful_shutdown is True
    _assert_bundle_valid(result)


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
            action_id_to_execute=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
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
        "action_id_to_execute": "rollback_checkout_retry_policy",
        "action_to_execute": "rollback checkout-api and cap retries",
        "decided_by": "control-plane-test-reviewer",
        "decided_tick": 7,
        "decision": "edit",
        "request_id": "mit-control-plane-01",
    }
    assert approval_token not in result.stderr_path.read_text()


def test_real_workflow_rejection_produces_a_valid_operational_bundle(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    async def reject(event):
        return ApprovalRecord(
            request_id=str(event.payload["request_id"]),
            case_id="case-protocol-stub",
            decision=ApprovalDecision.REJECT,
            decided_by="control-plane-test-reviewer",
            decided_tick=7,
        )

    result = asyncio.run(
        control_plane.execute(
            [sys.executable, "-m", "agent_lab.workflow_protocol_stub"],
            request,
            approval_handler=reject,
        )
    )

    assert result.terminal.status is TerminalStatus.APPROVAL_REJECTED
    assert result.terminal.reason == "native_workflow_approval_rejected"
    _assert_bundle_valid(result)


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


def test_real_workflow_protocol_session_normalizes_usage_and_keeps_token_ephemeral(
    tmp_path: Path,
) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())
    approval_token = "real-session-approval-secret"

    async def approve(event):
        return ApprovalRecord(
            request_id=str(event.payload["request_id"]),
            case_id="case-protocol-stub",
            decision=ApprovalDecision.EDIT,
            decided_by="protocol-session-test-reviewer",
            decided_tick=5,
            action_id_to_execute=MitigationActionId.ROLLBACK_CHECKOUT_RETRY_POLICY,
            action_to_execute="rollback checkout-api and cap retries",
            approval_token=approval_token,
        )

    async def complete(_event):
        artifact = {"status": "hermetic-finalization-complete"}
        RunBundle(request.paths.bundle, request.manifest).write_artifact(
            "protocol-stub.json",
            artifact,
        )
        return FinalizationResult(
            artifacts=[
                FinalizedArtifact(
                    path="artifacts/protocol-stub.json",
                    sha256=sha256_payload(artifact),
                )
            ],
            task_success=True,
            total_score=0.75,
        )

    result = asyncio.run(
        control_plane.execute(
            [sys.executable, "-m", "agent_lab.workflow_protocol_stub"],
            request,
            approval_handler=approve,
            completion_handler=complete,
        )
    )

    assert result.terminal.status is TerminalStatus.COMPLETED
    assert result.terminal.used_model_calls == 1
    assert result.terminal.used_tool_calls == 1
    assert result.terminal.used_output_tokens == 12
    events_text = (result.paths.bundle / "events.jsonl").read_text()
    assert approval_token not in events_text
    events = [json.loads(line) for line in events_text.splitlines()]
    assert [event["kind"] for event in events] == [
        "run.started",
        "agent.handoff",
        "tool.requested",
        "tool.started",
        "tool.finished",
        "agent.started",
        "model.requested",
        "model.responded",
        "agent.finished",
        "metric.recorded",
        "agent.handoff",
        "approval.requested",
        "checkpoint.saved",
        "approval.decided",
        "state.updated",
        "artifact.written",
        "run.finished",
    ]
    handoffs = [event for event in events if event["kind"] == "agent.handoff"]
    assert [event["payload"] for event in handoffs] == [
        {
            "phase": "specialist_delegated",
            "source_actor": "incident_commander",
            "source_namespace": "maf_workflow/incident_commander",
            "target_actor": "telemetry_investigator",
            "target_namespace": "maf_workflow/telemetry_investigator",
        },
        {
            "phase": "specialist_finding_returned",
            "source_actor": "telemetry_investigator",
            "source_namespace": "maf_workflow/telemetry_investigator",
            "target_actor": "incident_commander",
            "target_namespace": "maf_workflow/incident_commander",
        },
    ]
    metric = next(event for event in events if event["kind"] == "metric.recorded")
    assert metric["payload"] == {
        "name": "model_token_usage",
        "model": "local-test",
        "namespace": "maf_workflow/telemetry_investigator",
        "input_tokens": 24,
        "output_tokens": 12,
    }
    for event in events:
        if (
            event["kind"].startswith(("agent.", "model.", "tool."))
            and event["kind"] != "agent.handoff"
        ):
            assert event["payload"]["namespace"] == ("maf_workflow/telemetry_investigator")
    agent_started = next(event for event in events if event["kind"] == "agent.started")
    model_requested = next(event for event in events if event["kind"] == "model.requested")
    model_responded = next(event for event in events if event["kind"] == "model.responded")
    agent_finished = next(event for event in events if event["kind"] == "agent.finished")
    assert model_requested["parent_event_id"] == agent_started["event_id"]
    assert model_responded["parent_event_id"] == model_requested["event_id"]
    assert agent_finished["parent_event_id"] == agent_started["event_id"]
    state_path = result.paths.state / "workflow-execution.json"
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert approval_token not in state_path.read_text()
    assert json.loads((result.paths.bundle / "artifacts/protocol-stub.json").read_text()) == {
        "status": "hermetic-finalization-complete"
    }


@pytest.mark.parametrize(
    ("failure_mode", "status", "reason", "event_kinds"),
    [
        (
            "model_timeout",
            TerminalStatus.MODEL_FAILURE,
            "native_model_failed:TimeoutError",
            ["run.started", "agent.started", "model.requested", "error", "run.finished"],
        ),
        (
            "tool_service",
            TerminalStatus.TOOL_SERVICE_FAILURE,
            "native_tool_service_failed:ConnectionError",
            [
                "run.started",
                "tool.requested",
                "tool.started",
                "tool.failed",
                "error",
                "run.finished",
            ],
        ),
        (
            "gate_failure",
            TerminalStatus.RUNNER_FAILURE,
            'evidence_gate_failed:["incident commander marked the evidence insufficient",'
            '"commander synthesis must cite at least two specialist roles"]',
            ["run.started", "error", "run.finished"],
        ),
    ],
)
def test_real_workflow_protocol_classifies_operation_failures(
    tmp_path: Path,
    failure_mode: str,
    status: TerminalStatus,
    reason: str,
    event_kinds: list[str],
) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(
        control_plane.execute(
            [
                sys.executable,
                "-m",
                "agent_lab.workflow_protocol_stub",
                "--scenario",
                failure_mode,
            ],
            request,
        )
    )

    assert result.terminal.status is status
    assert result.terminal.reason == reason
    assert result.graceful_shutdown is True
    events = [
        json.loads(line) for line in (result.paths.bundle / "events.jsonl").read_text().splitlines()
    ]
    assert [event["kind"] for event in events] == event_kinds
    assert events[-2]["payload"] == {"reason": reason}
    assert events[-1]["payload"]["status"] == status.value
    _assert_bundle_valid(result)


def test_real_workflow_protocol_preserves_interleaved_actor_parentage(
    tmp_path: Path,
) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(
        control_plane.execute(
            [
                sys.executable,
                "-m",
                "agent_lab.workflow_protocol_stub",
                "--scenario",
                "parallel_interleave",
            ],
            request,
        )
    )

    assert result.terminal.status is TerminalStatus.COMPLETED
    assert result.terminal.used_model_calls == 2
    assert result.terminal.used_output_tokens == 12
    events = [
        json.loads(line) for line in (result.paths.bundle / "events.jsonl").read_text().splitlines()
    ]
    requested_by_actor = {
        event["actor"]: event for event in events if event["kind"] == "model.requested"
    }
    responded = [event for event in events if event["kind"] == "model.responded"]
    assert [event["actor"] for event in responded] == [
        "telemetry_investigator",
        "change_investigator",
    ]
    for event in responded:
        request_event = requested_by_actor[event["actor"]]
        assert event["parent_event_id"] == request_event["event_id"]
        assert event["payload"]["namespace"] == f"maf_workflow/{event['actor']}"
    agent_started_by_actor = {
        event["actor"]: event for event in events if event["kind"] == "agent.started"
    }
    for event in (item for item in events if item["kind"] == "agent.finished"):
        assert event["parent_event_id"] == agent_started_by_actor[event["actor"]]["event_id"]


def test_mock_runner_crash_is_runner_failure(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    result = asyncio.run(control_plane.execute(_command("crash"), request))

    assert result.terminal.status is TerminalStatus.RUNNER_FAILURE
    assert result.terminal.reason == "runner_exited_without_terminal:17"
    _assert_bundle_valid(result)


def test_oversized_protocol_line_is_terminalized_and_reaped(tmp_path: Path) -> None:
    control_plane = ControlPlane(
        tmp_path,
        graceful_shutdown_seconds=0.2,
        protocol_line_limit_bytes=1024,
    )
    request = _request(control_plane, _manifest())

    result = asyncio.run(control_plane.execute(_command("oversized_line"), request))

    assert result.terminal.status is TerminalStatus.RUNNER_FAILURE
    assert result.terminal.reason == "runner_protocol_line_too_large"
    assert result.terminal.event_stream_complete is False
    assert result.graceful_shutdown is True
    assert (result.paths.bundle / "terminal.json").is_file()
    assert result.stderr_path.is_file()
    _assert_bundle_valid(result)


def test_prepare_send_failure_still_reaps_runner_and_writes_terminal(tmp_path: Path) -> None:
    class PrepareSendFailureControlPlane(ControlPlane):
        async def _send(self, process, command):
            if isinstance(command, PrepareCommand):
                raise BrokenPipeError("injected prepare failure")
            await super()._send(process, command)

    control_plane = PrepareSendFailureControlPlane(tmp_path, graceful_shutdown_seconds=0.1)
    request = _request(control_plane, _manifest())

    result = asyncio.run(
        control_plane.execute(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            request,
        )
    )

    assert result.terminal.status is TerminalStatus.RUNNER_FAILURE
    assert result.terminal.reason == "prepare_command_failed:BrokenPipeError"
    assert result.terminal.execution_started is False
    assert result.terminal.event_stream_complete is False
    assert result.graceful_shutdown is False
    assert result.stderr_path.is_file()
    _assert_bundle_valid(result)


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
    _assert_bundle_valid(result)


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
    _assert_bundle_valid(result)


def test_budget_terminal_does_not_require_success_finalization(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    request = _request(control_plane, _manifest())

    async def unexpected_completion(_event):
        raise AssertionError("budget exhaustion must not request finalization")

    result = asyncio.run(
        control_plane.execute(
            _command("budget", budget_field="model_calls"),
            request,
            completion_handler=unexpected_completion,
        )
    )

    assert result.terminal.status is TerminalStatus.BUDGET_EXHAUSTED
    assert result.terminal.reason == "model_call_budget_exhausted"


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
