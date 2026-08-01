import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_lab.contracts import AgentRole

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ALIGNED_LONESTAR_MODEL = "unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL"


def test_native_langgraph_investigation_matches_maf_commander_synthesis() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "runners/langgraph"),
            "--no-sync",
            "python",
            "-m",
            "langgraph_runner.workflow_lane",
            "--deterministic-probe",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)

    assert payload["passed"] is True
    assert payload["gate_reasons"] == []
    assert payload["evidence_ids"] == ["ev-change", "ev-runbook", "ev-telemetry"]
    assert set(payload["independent_roles"]) == {
        AgentRole.TELEMETRY_INVESTIGATOR.value,
        AgentRole.CHANGE_INVESTIGATOR.value,
        AgentRole.RELIABILITY_REVIEWER.value,
    }
    assert [item["role"] for item in payload["findings"]] == sorted(
        role.value
        for role in (
            AgentRole.TELEMETRY_INVESTIGATOR,
            AgentRole.CHANGE_INVESTIGATOR,
            AgentRole.RELIABILITY_REVIEWER,
        )
    )
    assert payload["synthesis"] == {
        "role": AgentRole.INCIDENT_COMMANDER.value,
        "root_cause": "correlated root cause",
        "chronology": ["change preceded symptoms", "telemetry confirmed impact"],
        "evidence_ids": ["ev-change", "ev-runbook", "ev-telemetry"],
        "proposed_mitigation": "apply the reversible runbook action",
        "recovery_checks": ["verify affected service health"],
        "evidence_sufficient": True,
        "uncertainties": [],
    }
    maf_result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "runners/maf"),
            "--no-sync",
            "python",
            "-m",
            "maf_runner.workflow_lane",
            "--deterministic-probe",
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert payload == json.loads(maf_result.stdout)


def test_native_langgraph_approval_matches_maf_through_recovery() -> None:
    commands = (
        (
            "runners/langgraph",
            "langgraph_runner.workflow_lane",
        ),
        (
            "runners/maf",
            "maf_runner.workflow_lane",
        ),
    )
    payloads = []
    for project, module in commands:
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
                "--deterministic-approved-probe",
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payloads.append(json.loads(result.stdout))

    assert payloads[0] == payloads[1]
    assert payloads[0]["status"] == "completed"
    assert payloads[0]["approval_decision"] == "approve"
    assert payloads[0]["mitigation"]["effective"] is True
    assert payloads[0]["recovery"]["recovered"] is True


def test_native_langgraph_rejection_matches_maf_without_mitigation() -> None:
    commands = (
        ("runners/langgraph", "langgraph_runner.workflow_lane"),
        ("runners/maf", "maf_runner.workflow_lane"),
    )
    payloads = []
    for project, module in commands:
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
                "--deterministic-rejected-probe",
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payloads.append(json.loads(result.stdout))

    assert payloads[0] == payloads[1]
    assert payloads[0]["status"] == "approval_rejected"
    assert payloads[0]["approval_decision"] == "reject"
    assert payloads[0]["action_to_execute"] is None
    assert payloads[0]["mitigation"] is None
    assert payloads[0]["recovery"] is None


@pytest.mark.skipif(
    os.getenv("AGENT_LAB_RUN_LIVE_MODEL_TESTS") != "1",
    reason="requires an explicit tunnel to the aligned lonestar model",
)
def test_live_langgraph_workflow_uses_native_approval_and_recovers() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.langgraph_workflow_compat",
            "--base-url",
            os.getenv("AGENT_LAB_LIVE_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"),
            "--model",
            ALIGNED_LONESTAR_MODEL,
        ],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=360,
    )
    payload = json.loads(result.stdout)

    assert payload["status"] == "ok"
    assert payload["framework"] == "langgraph"
    assert payload["model"] == ALIGNED_LONESTAR_MODEL
    assert payload["advertised_models"] == [ALIGNED_LONESTAR_MODEL]
    execution = payload["execution"]
    assert execution["status"] == "completed"
    assert execution["evidence"]["passed"] is True
    assert execution["approval_decision"] == "edit"
    assert execution["action_to_execute"] == "rollback checkout-api and cap retries"
    assert execution["mitigation"]["effective"] is True
    assert execution["recovery"]["recovered"] is True
    assert payload["world_state_changed"] is True
    assert payload["checkpoint_contains_approval_token"] is False
    assert payload["compatibility_approval_policy"] == ("seed_42_edit_to_known_recovery_action")
    assert payload["protocol_terminal_status"] == "completed"
    assert payload["protocol_usage"]["model_calls"] == 4
    assert payload["protocol_usage"]["tool_calls"] == 10
    assert payload["protocol_usage"]["output_tokens"] > 0
    assert payload["normalized_event_kinds"][0] == "run.started"
    assert payload["normalized_event_kinds"][-1] == "run.finished"
    assert payload["normalized_event_kinds"].count("model.requested") == 4
    assert payload["normalized_event_kinds"].count("model.responded") == 4
    assert payload["normalized_event_kinds"].count("tool.requested") == 10
    assert payload["normalized_event_kinds"].count("approval.requested") == 1
    assert payload["normalized_event_kinds"].count("approval.decided") == 1
    assert payload["normalized_event_kinds"].count("checkpoint.saved") == 1
    assert payload["normalized_event_kinds"].count("artifact.written") == 5
    assert payload["bundle_valid"] is True
    assert isinstance(payload["task_success"], bool)
    assert 0 <= payload["total_score"] <= 1
    assert sorted(payload["world_operations"]) == sorted(
        [
            "query_service_health",
            "query_metric",
            "list_recent_changes",
            "get_config_diff",
            "search_runbooks",
            "get_runbook",
            "submit_hypothesis",
            "request_mitigation",
            "approval_decision",
            "apply_mitigation",
            "verify_recovery",
        ]
    )
    assert all(stats["model_calls"] == 1 for stats in payload["role_stats"].values())
    assert payload["role_stats"][AgentRole.INCIDENT_COMMANDER.value]["tool_calls"] == 4
    assert all(
        payload["role_stats"][role.value]["tool_calls"] == 2
        for role in (
            AgentRole.TELEMETRY_INVESTIGATOR,
            AgentRole.CHANGE_INVESTIGATOR,
            AgentRole.RELIABILITY_REVIEWER,
        )
    )
