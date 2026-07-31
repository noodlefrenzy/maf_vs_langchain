from concurrent.futures import ThreadPoolExecutor

import pytest
from incident_lab_mcp.http_auth import (
    CredentialError,
    RunCredentialSigner,
    RunWorldRegistry,
)

from agent_lab.contracts import FrameworkLane


def _registry(now: int = 1000):
    registry = RunWorldRegistry()
    receipt = registry.create_pair(
        pair_id="pair-test",
        seed=42,
        left_lane=FrameworkLane.MAF_WORKFLOW,
        right_lane=FrameworkLane.LANGGRAPH,
        ttl_seconds=300,
        fault_profile="none",
        now=now,
    )
    return registry, receipt


def test_pair_clones_have_equal_manifests_and_isolated_state() -> None:
    registry, receipt = _registry()
    left = registry.get(receipt.left.run_id, now=1000)
    right = registry.get(receipt.right.run_id, now=1000)

    assert receipt.manifests_equal is True
    assert receipt.left.world_manifest_sha256 == receipt.right.world_manifest_sha256
    assert left.world is not right.world
    left.world.get_incident_brief()
    assert left.world.tool_calls == 1
    assert right.world.tool_calls == 0


def test_role_credential_is_signed_scoped_expiring_and_revocable() -> None:
    registry, receipt = _registry()
    run = registry.get(receipt.left.run_id, now=1000)
    signer = RunCredentialSigner(secret=b"s" * 32)
    token = signer.issue(run, "telemetry", now=1000)

    claims = signer.verify(token, now=1001)
    assert claims.run_id == run.run_id
    assert claims.lane is FrameworkLane.MAF_WORKFLOW
    assert claims.role == "telemetry"

    prefix, payload, signature = token.split(".")
    tampered = f"{prefix}.{payload[:-1]}A.{signature}"
    with pytest.raises(CredentialError, match="signature"):
        signer.verify(tampered, now=1001)
    with pytest.raises(CredentialError, match="expired"):
        signer.verify(token, now=1300)

    registry.deactivate(run.run_id)
    with pytest.raises(CredentialError, match="revoked"):
        registry.get(run.run_id, now=1001)
    with pytest.raises(CredentialError, match="inactive"):
        signer.issue(run, "telemetry", now=1001)


def test_concurrent_role_calls_share_one_serialized_world() -> None:
    registry, receipt = _registry()
    run = registry.get(receipt.left.run_id, now=1000)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: run.world.get_incident_status(),
                range(40),
            )
        )

    assert run.world.tool_calls == 40
    assert run.world.world_tick == 40
    assert sorted(result["world_tick"] for result in results) == list(range(1, 41))
    assert [event.sequence for event in run.world.audit_log] == list(range(40))


def test_registry_rejects_duplicate_active_pair_and_expires_runs() -> None:
    registry, receipt = _registry()

    with pytest.raises(ValueError, match="already exists"):
        registry.create_pair(
            pair_id="pair-test",
            seed=42,
            left_lane=FrameworkLane.MAF_HARNESS,
            right_lane=FrameworkLane.DEEP_AGENTS,
            now=1000,
        )
    with pytest.raises(CredentialError, match="expired"):
        registry.get(receipt.left.run_id, now=1300)
    assert {run.run_id for run in registry.active_runs(now=1300)} == set()
