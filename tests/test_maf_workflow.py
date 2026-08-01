import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_lab.contracts import AgentRole

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ALIGNED_LONESTAR_MODEL = "unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL"


def test_native_maf_investigation_synthesizes_before_evidence_gate() -> None:
    result = subprocess.run(
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


@pytest.mark.skipif(
    os.getenv("AGENT_LAB_RUN_LIVE_MODEL_TESTS") != "1",
    reason="requires an explicit tunnel to the aligned lonestar model",
)
def test_live_maf_investigation_uses_aligned_lonestar_model_and_scoped_mcp() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT / "services/incident_mcp"),
            "--no-sync",
            "python",
            "-m",
            "incident_lab_mcp.maf_workflow_compat",
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
    assert payload["framework"] == "maf_workflow"
    assert payload["model"] == ALIGNED_LONESTAR_MODEL
    assert payload["advertised_models"] == [ALIGNED_LONESTAR_MODEL]
    assert payload["packet"]["passed"] is True
    assert payload["packet"]["synthesis"]["role"] == AgentRole.INCIDENT_COMMANDER.value
    assert payload["packet"]["synthesis"]["evidence_sufficient"] is True
    assert all(item.startswith("ev-") for item in payload["packet"]["evidence_ids"])
    assert payload["world_state_changed"] is False
    assert sorted(payload["world_operations"]) == sorted(
        [
            "query_service_health",
            "query_metric",
            "list_recent_changes",
            "get_config_diff",
            "search_runbooks",
            "get_runbook",
        ]
    )
    assert all(stats["model_calls"] == 1 for stats in payload["role_stats"].values())
    assert payload["role_stats"][AgentRole.INCIDENT_COMMANDER.value]["tool_calls"] == 0
    assert all(
        payload["role_stats"][role.value]["tool_calls"] == 2
        for role in (
            AgentRole.TELEMETRY_INVESTIGATOR,
            AgentRole.CHANGE_INVESTIGATOR,
            AgentRole.RELIABILITY_REVIEWER,
        )
    )
