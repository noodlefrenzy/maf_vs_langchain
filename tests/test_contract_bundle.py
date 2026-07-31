import importlib.util
import json
from pathlib import Path
from types import ModuleType

from agent_lab.contract_bundle import contract_bundle_hashes, error_taxonomy_payload
from agent_lab.contracts import SCHEMA_VERSION, AgentRole
from agent_lab.tool_catalog import get_tool_definition, tool_definitions_for_role


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_bundle_matches_versioned_lock() -> None:
    lock_path = Path("contracts/v0.1.0/contract-lock.json")
    expected = json.loads(lock_path.read_text(encoding="utf-8"))

    assert expected.pop("schema_version") == SCHEMA_VERSION
    assert contract_bundle_hashes() == expected


def test_runner_adapters_load_byte_equivalent_shared_inputs() -> None:
    maf = _load_module(
        Path("runners/maf/src/maf_runner/shared_contracts.py"),
        "maf_shared_contracts",
    )
    langgraph = _load_module(
        Path("runners/langgraph/src/langgraph_runner/shared_contracts.py"),
        "langgraph_shared_contracts",
    )

    maf_snapshot = maf.shared_contract_snapshot()
    langgraph_snapshot = langgraph.shared_contract_snapshot()
    assert json.dumps(maf_snapshot, sort_keys=True) == json.dumps(
        langgraph_snapshot,
        sort_keys=True,
    )


def test_role_catalogs_enforce_capability_boundaries() -> None:
    commander_tools = {
        tool.name for tool in tool_definitions_for_role(AgentRole.INCIDENT_COMMANDER)
    }
    telemetry_tools = {
        tool.name for tool in tool_definitions_for_role(AgentRole.TELEMETRY_INVESTIGATOR)
    }

    assert "apply_mitigation" in commander_tools
    assert "search_logs" not in commander_tools
    assert "search_logs" in telemetry_tools
    assert "apply_mitigation" not in telemetry_tools
    assert {"get_incident_brief", "get_incident_status"} <= commander_tools & telemetry_tools


def test_every_mutating_tool_requires_approval() -> None:
    mutating_tools = [
        tool for role in AgentRole for tool in tool_definitions_for_role(role) if tool.mutates_world
    ]

    assert {tool.name for tool in mutating_tools} == {"apply_mitigation"}
    assert all(tool.approval_required for tool in mutating_tools)
    assert get_tool_definition("request_mitigation").mutates_world is False


def test_only_transient_timeout_is_retryable() -> None:
    retryable = {item["code"] for item in error_taxonomy_payload()["errors"] if item["retryable"]}

    assert retryable == {"transient_timeout"}
