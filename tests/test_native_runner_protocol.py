import asyncio
import json
from pathlib import Path

import pytest

from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import (
    AgentRole,
    Budget,
    FrameworkLane,
    ModelProfile,
    NativeTracingProfile,
    RunManifest,
    TerminalStatus,
)
from agent_lab.control_plane import ControlPlane
from agent_lab.matched_contracts import normalized_topology_for_lane
from agent_lab.runner_protocol import RoleEndpoint, RunRequest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _manifest(lane: FrameworkLane) -> RunManifest:
    return RunManifest(
        experiment_id="native_protocol_test",
        lane=lane,
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
            wall_clock_seconds=10,
            model_calls=2,
            tool_calls=4,
            output_tokens=100,
            approval_requests=1,
        ),
        source_commit="0" * 40,
        source_tree_sha256="5" * 64,
        framework_versions={"native-runner": "protocol-probe"},
        dependency_lock_sha256="4" * 64,
        prompt_bundle_sha256="1" * 64,
        tool_catalog_sha256="2" * 64,
        world_manifest_sha256="3" * 64,
    )


@pytest.mark.parametrize(
    ("project", "module", "lane", "version_key"),
    [
        (
            "runners/maf",
            "maf_runner.protocol_runner",
            FrameworkLane.MAF_WORKFLOW,
            "agent-framework-core",
        ),
        (
            "runners/langgraph",
            "langgraph_runner.protocol_runner",
            FrameworkLane.LANGGRAPH,
            "langgraph",
        ),
    ],
)
def test_native_runner_protocol_probe(
    tmp_path: Path,
    project: str,
    module: str,
    lane: FrameworkLane,
    version_key: str,
) -> None:
    control_plane = ControlPlane(tmp_path)
    manifest = _manifest(lane)
    _bundle, paths = control_plane.allocate_run(manifest)
    request = RunRequest(
        manifest=manifest,
        paths=paths,
        role_endpoints={
            AgentRole.INCIDENT_COMMANDER: RoleEndpoint(
                role=AgentRole.INCIDENT_COMMANDER,
                url="http://127.0.0.1:8765/runs/test/commander/mcp",
                token="native-probe-secret",
                expires_at=2_000_000_000,
            )
        },
        public_ticket="Protocol probe only; do not invoke a model or tool.",
        order_seed=31,
        tracing=NativeTracingProfile(
            collector_endpoint="http://127.0.0.1:1/v1/traces",
        ),
    )
    command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / project),
        "--no-sync",
        "python",
        "-m",
        module,
        "--protocol-probe",
    ]

    result = asyncio.run(control_plane.execute(command, request))

    assert result.terminal.status is TerminalStatus.COMPLETED
    assert result.terminal.reason == "native_protocol_probe_completed"
    assert result.prepared is not None
    assert version_key in result.prepared.framework_versions
    assert result.prepared.checkpoint.resumable is True
    assert result.prepared.checkpoint.restart_safe is True
    events = [
        json.loads(line) for line in (result.paths.bundle / "events.jsonl").read_text().splitlines()
    ]
    assert [event["kind"] for event in events] == ["run.started", "run.finished"]
    assert events[-1]["payload"]["status"] == "completed"
    assert all(event["trace_id"] and event["span_id"] for event in events)
    trace_link = json.loads(
        (result.paths.bundle / "native" / "trace-link.json").read_text(encoding="utf-8")
    )
    assert not (result.paths.bundle / "native" / ".trace-link.json.tmp").exists()
    assert trace_link["collector_status"] == "unavailable"
    assert trace_link["span_count"] >= 1
    assert trace_link["completeness"]["ratio"] == 1.0
    assert (result.paths.traces / "spans.jsonl").is_file()
    assert (
        "native-probe-secret"
        not in (result.paths.bundle / "native" / "run-request.redacted.json").read_text()
    )


def test_native_runner_protocol_probe_can_disable_tracing(tmp_path: Path) -> None:
    control_plane = ControlPlane(tmp_path)
    manifest = _manifest(FrameworkLane.MAF_WORKFLOW)
    _bundle, paths = control_plane.allocate_run(manifest)
    request = RunRequest(
        manifest=manifest,
        paths=paths,
        role_endpoints={},
        public_ticket="Tracing-disabled protocol probe.",
        order_seed=9,
        tracing=NativeTracingProfile(enabled=False),
    )
    command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / "runners/maf"),
        "--no-sync",
        "python",
        "-m",
        "maf_runner.protocol_runner",
        "--protocol-probe",
    ]

    result = asyncio.run(control_plane.execute(command, request))

    assert result.terminal.status is TerminalStatus.COMPLETED
    trace_link = json.loads(
        (result.paths.bundle / "native" / "trace-link.json").read_text(encoding="utf-8")
    )
    assert trace_link["enabled"] is False
    assert trace_link["collector_status"] == "disabled"
    assert trace_link["span_count"] == 0
    assert not (result.paths.traces / "spans.jsonl").exists()
    events = [
        json.loads(line) for line in (result.paths.bundle / "events.jsonl").read_text().splitlines()
    ]
    assert all(event["trace_id"] is None and event["span_id"] is None for event in events)


@pytest.mark.parametrize(
    ("project", "module", "expected_lanes", "restart_lane"),
    [
        (
            "runners/maf",
            "maf_runner.protocol_runner",
            {"maf_workflow", "maf_harness"},
            "maf_workflow",
        ),
        (
            "runners/langgraph",
            "langgraph_runner.protocol_runner",
            {"langgraph", "deep_agents"},
            "langgraph",
        ),
    ],
)
def test_native_runner_describes_supported_lanes(
    project: str,
    module: str,
    expected_lanes: set[str],
    restart_lane: str,
) -> None:
    import subprocess

    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / project),
            "--no-sync",
            "python",
            "-m",
            module,
            "--describe",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)

    assert set(payload["supported_lanes"]) == expected_lanes
    assert set(payload["topology"]) == expected_lanes
    assert set(payload["normalized_topology"]) == expected_lanes
    assert payload["contract_hashes"] == contract_bundle_hashes()
    assert payload["normalized_topology"][restart_lane] == normalized_topology_for_lane(
        FrameworkLane(restart_lane)
    )
    assert payload["checkpoint"][restart_lane]["resumable"] is True
    assert payload["checkpoint"][restart_lane]["restart_safe"] is True
    unavailable = expected_lanes - {restart_lane}
    assert all(payload["checkpoint"][lane]["resumable"] is False for lane in unavailable)
