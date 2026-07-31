import json
import stat
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=25,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("framework", "project", "module", "checkpoint_name"),
    [
        ("maf_workflow", "runners/maf", "maf_runner.checkpoint_compat", "native"),
        (
            "langgraph",
            "runners/langgraph",
            "langgraph_runner.checkpoint_compat",
            "native/checkpoint.json",
        ),
    ],
)
def test_native_framework_and_world_resume_across_process_restart(
    tmp_path: Path,
    framework: str,
    project: str,
    module: str,
    checkpoint_name: str,
) -> None:
    case_dir = tmp_path / framework
    world_checkpoint = case_dir / "world.json"
    native_checkpoint = case_dir / checkpoint_name
    service_command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / "services/incident_mcp"),
        "--no-sync",
        "python",
        "-m",
        "incident_lab_mcp.restart_compat",
    ]
    runner_command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / project),
        "--no-sync",
        "python",
        "-m",
        module,
    ]

    world_prepared = _run(
        [
            *service_command,
            "prepare",
            "--checkpoint",
            str(world_checkpoint),
            "--seed",
            "104",
            "--pending",
        ]
    )
    request_id = str(world_prepared["request_id"])
    runner_prepared = _run(
        [
            *runner_command,
            "prepare",
            "--checkpoint-path",
            str(native_checkpoint),
            "--request-id",
            request_id,
        ]
    )
    runner_resumed = _run(
        [
            *runner_command,
            "resume",
            "--checkpoint-path",
            str(native_checkpoint),
            "--checkpoint-id",
            str(runner_prepared["checkpoint_id"]),
            "--request-id",
            request_id,
            "--decision",
            "approve",
        ]
    )
    world_resumed = _run([*service_command, "resume", "--checkpoint", str(world_checkpoint)])

    assert world_prepared["approval_state"] == "pending"
    assert world_prepared["applied"] is False
    assert runner_prepared["framework"] == framework
    assert runner_prepared["phase"] == "awaiting_approval"
    assert runner_resumed["phase"] == "resumed"
    assert runner_resumed["decision"] == "approve"
    assert runner_resumed["resumed_from_checkpoint_id"] == runner_prepared["checkpoint_id"]
    assert runner_resumed["checkpoint_id"] != runner_prepared["checkpoint_id"]
    assert world_resumed["approval_state"] == "approve"
    assert world_resumed["applied"] is True
    assert world_resumed["recovered"] is True
    assert {
        world_prepared["request_id"],
        runner_prepared["request_id"],
        runner_resumed["request_id"],
        world_resumed["request_id"],
    } == {request_id}
    assert runner_prepared["pid"] != runner_resumed["pid"]
    assert world_prepared["pid"] != world_resumed["pid"]
    assert stat.S_IMODE(world_checkpoint.stat().st_mode) == 0o600

    if native_checkpoint.is_dir():
        assert stat.S_IMODE(native_checkpoint.stat().st_mode) == 0o700
        checkpoint_files = list(native_checkpoint.glob("*.json"))
        assert len(checkpoint_files) >= 2
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in checkpoint_files)
        native_payload = "".join(path.read_text() for path in checkpoint_files)
    else:
        assert stat.S_IMODE(native_checkpoint.stat().st_mode) == 0o600
        assert stat.S_IMODE(native_checkpoint.parent.stat().st_mode) == 0o700
        native_payload = native_checkpoint.read_text()
    assert "approval_token" not in native_payload
