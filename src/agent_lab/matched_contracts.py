"""Canonical normalized topology for the two matched framework tracks."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from agent_lab.contract_bundle import sha256_payload
from agent_lab.contracts import Budget, FrameworkLane, ModelProfile
from agent_lab.model_request_policy import matched_effective_request_policy

_SPECIALIST_ROLES = [
    "telemetry_investigator",
    "change_investigator",
    "reliability_reviewer",
]

_EXPLICIT_WORKFLOW_TOPOLOGY: dict[str, Any] = {
    "kind": "explicit_workflow",
    "stages": [
        {"kind": "parallel_fanout", "roles": _SPECIALIST_ROLES},
        {"kind": "fan_in", "node": "incident_commander"},
        {"kind": "guard", "node": "evidence_gate"},
        {"kind": "interrupt", "node": "approval"},
        {"kind": "state_change", "node": "mitigation"},
        {"kind": "verification", "node": "recovery_verification"},
    ],
}

_OPINIONATED_HARNESS_TOPOLOGY: dict[str, Any] = {
    "kind": "opinionated_harness",
    "coordinator": "incident_commander",
    "specialists": _SPECIALIST_ROLES,
    "delegation_required": True,
    "outer_stages": ["evidence_gate", "approval", "mitigation", "recovery_verification"],
}


def normalized_topology_for_lane(lane: FrameworkLane) -> dict[str, Any]:
    """Return a fresh framework-neutral topology snapshot for one lane."""

    if lane in {FrameworkLane.MAF_WORKFLOW, FrameworkLane.LANGGRAPH}:
        return deepcopy(_EXPLICIT_WORKFLOW_TOPOLOGY)
    if lane in {FrameworkLane.MAF_HARNESS, FrameworkLane.DEEP_AGENTS}:
        return deepcopy(_OPINIONATED_HARNESS_TOPOLOGY)
    raise ValueError(f"unsupported matched lane {lane.value}")


class RunnerContractMetadata(Protocol):
    """Descriptor fields required by the framework-neutral preflight."""

    contract_hashes: dict[str, str]
    normalized_topology: dict[FrameworkLane, dict[str, object]]


def preflight_pair_contract(
    *,
    track: str,
    left_lane: FrameworkLane,
    right_lane: FrameworkLane,
    descriptors: dict[FrameworkLane, RunnerContractMetadata],
    expected_contract_hashes: dict[str, str],
    experiment_id: str,
    scenario: str,
    scenario_seed: int,
    repetition: int,
    model: ModelProfile,
    budget: Budget,
    fault_profile: str,
    source_commit: str,
    source_tree_sha256: str,
    execution_provenance_by_lane: dict[FrameworkLane, dict[str, Any]],
    world_manifest_sha256: str,
) -> dict[str, Any]:
    """Fail closed on matched-contract drift before either paired lane starts."""

    expected_topology = normalized_topology_for_lane(left_lane)
    if normalized_topology_for_lane(right_lane) != expected_topology:
        raise ValueError(f"canonical normalized topology differs within {track}")
    for lane in (left_lane, right_lane):
        descriptor = descriptors[lane]
        if descriptor.contract_hashes != expected_contract_hashes:
            raise ValueError(
                f"{lane.value} runner contract hashes differ from the repository contract lock"
            )
        if descriptor.normalized_topology[lane] != expected_topology:
            raise ValueError(f"{lane.value} runner normalized topology differs within {track}")

    request_policy = matched_effective_request_policy(model, left_lane, right_lane)
    left_execution = execution_provenance_by_lane[left_lane]
    right_execution = execution_provenance_by_lane[right_lane]
    for lane, execution in (
        (left_lane, left_execution),
        (right_lane, right_execution),
    ):
        frozen_hash = execution.get("provenance_sha256")
        snapshot_payload = {
            key: value for key, value in execution.items() if key != "provenance_sha256"
        }
        if frozen_hash != sha256_payload(snapshot_payload):
            raise ValueError(f"{lane.value} execution provenance hash is invalid")
    if left_execution != right_execution:
        raise ValueError(f"effective inference execution provenance differs within {track}")

    snapshot = {
        "schema_version": "0.1.0",
        "track": track,
        "lanes": [left_lane.value, right_lane.value],
        "normalized_topology": expected_topology,
        "held_constants": {
            "experiment_id": experiment_id,
            "scenario": scenario,
            "scenario_seed": scenario_seed,
            "repetition": repetition,
            "model": model.model_dump(mode="json"),
            "budget": budget.model_dump(mode="json"),
            "fault_profile": fault_profile,
            "source_commit": source_commit,
            "source_tree_sha256": source_tree_sha256,
            "execution_provenance": left_execution,
            "world_manifest_sha256": world_manifest_sha256,
            "contract_hashes": expected_contract_hashes,
        },
        "role_prompts_sha256": expected_contract_hashes["prompt_bundle_sha256"],
        "tool_schemas_sha256": expected_contract_hashes["tool_catalog_sha256"],
        "completion_schema_sha256": expected_contract_hashes["artifact_schemas_sha256"],
        "workflow_contract_sha256": expected_contract_hashes["workflow_contract_sha256"],
        "effective_request_policy": request_policy,
        "effective_request_policy_sha256": request_policy["policy_sha256"],
        "execution_provenance_sha256": left_execution["provenance_sha256"],
    }
    return {**snapshot, "snapshot_sha256": sha256_payload(snapshot)}
