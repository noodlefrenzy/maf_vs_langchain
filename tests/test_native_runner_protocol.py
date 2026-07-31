import asyncio
import json
from pathlib import Path

import pytest

from agent_lab.contracts import (
    AgentRole,
    Budget,
    FrameworkLane,
    ModelProfile,
    RunManifest,
    TerminalStatus,
)
from agent_lab.control_plane import ControlPlane
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
    assert result.prepared.checkpoint.resumable is False
    events = [
        json.loads(line)
        for line in (result.paths.bundle / "events.jsonl").read_text().splitlines()
    ]
    assert [event["kind"] for event in events] == ["run.started", "run.finished"]
    assert events[-1]["payload"]["status"] == "completed"
    assert "native-probe-secret" not in (
        result.paths.bundle / "native" / "run-request.redacted.json"
    ).read_text()


@pytest.mark.parametrize(
    ("project", "module", "expected_lanes"),
    [
        (
            "runners/maf",
            "maf_runner.protocol_runner",
            {"maf_workflow", "maf_harness"},
        ),
        (
            "runners/langgraph",
            "langgraph_runner.protocol_runner",
            {"langgraph", "deep_agents"},
        ),
    ],
)
def test_native_runner_describes_supported_lanes(
    project: str,
    module: str,
    expected_lanes: set[str],
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
