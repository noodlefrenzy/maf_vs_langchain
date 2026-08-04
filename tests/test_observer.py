from __future__ import annotations

import asyncio
import json
import stat
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from rich.console import Console

from agent_lab.contracts import (
    Budget,
    EventKind,
    EventVisibility,
    FrameworkLane,
    ModelProfile,
    RunManifest,
)
from agent_lab.observer import (
    ObserverRepository,
    build_observer_app,
    fetch_pair_snapshot,
    load_or_create_observer_token,
    render_pair_snapshot,
)
from agent_lab.observer_contracts import (
    approval_decision_path,
    observer_pair_context,
    wait_for_observer_approval,
)
from agent_lab.run_bundle import RunBundle


def _manifest(lane: FrameworkLane) -> RunManifest:
    return RunManifest(
        experiment_id="observer_test_native_workflow",
        lane=lane,
        scenario="incident_response",
        scenario_generator_version="test",
        scenario_seed=42,
        repetition=0,
        model=ModelProfile(
            name="test-model",
            base_url="http://127.0.0.1:8000/v1",
            temperature=0.2,
            top_p=0.9,
            max_output_tokens=100,
            context_window_tokens=4096,
            request_timeout_seconds=30,
        ),
        budget=Budget(
            wall_clock_seconds=30,
            model_calls=10,
            tool_calls=20,
            output_tokens=1000,
            approval_requests=1,
        ),
        source_commit="1" * 40,
        source_tree_sha256="2" * 64,
        framework_versions={"test": "1"},
        dependency_lock_sha256="3" * 64,
        prompt_bundle_sha256="4" * 64,
        tool_catalog_sha256="5" * 64,
        effective_request_policy_sha256="6" * 64,
        execution_provenance_sha256="7" * 64,
        world_manifest_sha256="8" * 64,
    )


def _paired_bundles(root: Path) -> tuple[RunBundle, RunBundle, str]:
    left = RunBundle.create(root, _manifest(FrameworkLane.MAF_WORKFLOW))
    right = RunBundle.create(root, _manifest(FrameworkLane.LANGGRAPH))
    run_ids = (left.manifest.run_id, right.manifest.run_id)
    context = observer_pair_context(
        index_file="native-workflow-observer-test-order-1.json",
        pair_key="explicit_workflow",
        track="explicit_workflow",
        seed=42,
        repetition=0,
        run_ids=run_ids,
    )
    for bundle in (left, right):
        (bundle.path / "native" / "observer-pair.json").write_text(
            json.dumps(context),
            encoding="utf-8",
        )
        bundle.emit(EventKind.RUN_STARTED, payload={"execution_mode": "native_workflow"})
        bundle.emit(EventKind.AGENT_STARTED, actor="incident_commander")
    left.emit(
        EventKind.TEXT_DELTA,
        actor="incident_commander",
        payload={"text": "raw model text must not render", "reasoning": "hidden"},
    )
    left.emit(
        EventKind.STATE_UPDATED,
        visibility=EventVisibility.INTERNAL,
        payload={"phase": "private", "reasoning": "hidden internal state"},
    )
    left.emit(
        EventKind.APPROVAL_REQUESTED,
        actor="incident_commander",
        payload={
            "request_id": "mit-test-01",
            "case_id": "inc-test",
            "proposed_action_id": "rollback_checkout_retry_policy",
            "proposed_action": "rollback checkout-api and cap retries",
            "rationale": "two evidence sources support the reversible action",
            "evidence_ids": ["ev-1", "ev-2"],
            "requested_by": "incident_commander",
            "created_tick": 8,
            "status": "pending",
            "untrusted_extra": "must not render",
        },
    )
    artifact = left.write_artifact("incident-report.json", {"status": "fixture"})
    left.emit(
        EventKind.ARTIFACT_WRITTEN,
        payload={"artifact": "artifacts/incident-report.json", "sha256": "a" * 64},
    )
    assert artifact.is_file()
    return left, right, str(context["pair_id"])


def test_observer_discovers_live_pair_and_sanitizes_events(tmp_path: Path) -> None:
    left, _right, pair_id = _paired_bundles(tmp_path)
    repository = ObserverRepository(tmp_path)

    snapshot = repository.pair_snapshot(repository.get_pair(pair_id))
    left_view = next(
        lane for lane in snapshot["lanes"] if lane["run_id"] == str(left.manifest.run_id)
    )
    rendered = json.dumps(left_view)

    assert left_view["status"] == "running"
    assert left_view["active_agents"] == ["incident_commander"]
    assert left_view["pending_approval"]["request_id"] == "mit-test-01"
    assert left_view["artifacts"][0]["path"] == "artifacts/incident-report.json"
    assert "raw model text must not render" not in rendered
    assert "hidden internal state" not in rendered
    assert "untrusted_extra" not in rendered
    assert (
        next(event for event in left_view["events"] if event["kind"] == "model.text_delta")[
            "payload"
        ]
        == {}
    )

    output = StringIO()
    render_pair_snapshot(snapshot, console=Console(file=output, force_terminal=False, width=160))
    assert "raw model text" not in output.getvalue()
    assert "maf_workflow" in output.getvalue()


def test_observer_drops_untrusted_trace_links_and_non_loopback_requests(tmp_path: Path) -> None:
    left, _right, pair_id = _paired_bundles(tmp_path)
    (left.path / "native" / "trace-link.json").write_text(
        json.dumps({"phoenix_trace_url": "javascript:alert(1)"}),
        encoding="utf-8",
    )
    app = build_observer_app(tmp_path, approval_token=None)

    snapshot = ObserverRepository(tmp_path).pair_snapshot(
        ObserverRepository(tmp_path).get_pair(pair_id)
    )
    left_view = next(
        lane for lane in snapshot["lanes"] if lane["run_id"] == str(left.manifest.run_id)
    )
    denied = asyncio.run(_asgi_request(app, "GET", "/api/meta", host="attacker.example"))

    assert left_view["phoenix_trace_url"] is None
    assert denied[0] == 403
    with pytest.raises(ValueError, match="pair ID is malformed"):
        fetch_pair_snapshot("http://127.0.0.1:8787", "../meta")


def test_observer_approval_is_authenticated_owner_only_and_audited(tmp_path: Path) -> None:
    left, _right, _pair_id = _paired_bundles(tmp_path)
    app = build_observer_app(tmp_path, approval_token="t" * 32, operator_id="test-operator")
    body = {
        "request_id": "mit-test-01",
        "decision": "edit",
        "edited_action_id": "restore_feature_flag_snapshot",
    }

    denied = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/runs/{left.manifest.run_id}/approval",
            body=body,
            headers={"authorization": "Bearer wrong"},
        )
    )
    accepted = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/runs/{left.manifest.run_id}/approval",
            body=body,
            headers={"authorization": f"Bearer {'t' * 32}"},
        )
    )

    assert denied[0] == 401
    assert accepted[0] == 200
    decision_path = approval_decision_path(left.path, "mit-test-01")
    assert stat.S_IMODE(decision_path.stat().st_mode) == 0o600
    decision = asyncio.run(wait_for_observer_approval(left.path, "mit-test-01"))
    assert decision.decision.value == "edit"
    assert decision.decided_by == "test-operator"
    audit = (left.path / "control" / "approval-api-audit.jsonl").read_text(encoding="utf-8")
    assert '"accepted":true' in audit
    assert "t" * 32 not in audit


def test_observer_rejects_decision_after_control_plane_closes_inbox(tmp_path: Path) -> None:
    left, _right, _pair_id = _paired_bundles(tmp_path)
    control = left.path / "control"
    control.mkdir(mode=0o700)
    (control / "approval-closed.json").write_text("{}\n", encoding="utf-8")
    app = build_observer_app(tmp_path, approval_token="t" * 32)

    response = asyncio.run(
        _asgi_request(
            app,
            "POST",
            f"/api/runs/{left.manifest.run_id}/approval",
            body={"request_id": "mit-test-01", "decision": "approve"},
            headers={"authorization": f"Bearer {'t' * 32}"},
        )
    )

    assert response[0] == 409
    audit = (control / "approval-api-audit.jsonl").read_text(encoding="utf-8")
    assert '"reason":"approval_inbox_closed"' in audit


def test_observer_sse_replays_strictly_after_requested_sequence(tmp_path: Path) -> None:
    left, _right, _pair_id = _paired_bundles(tmp_path)
    app = build_observer_app(tmp_path, approval_token=None)

    status, _headers, body = asyncio.run(
        _asgi_request(
            app,
            "GET",
            f"/api/runs/{left.manifest.run_id}/events",
            query="after=1&follow=false",
        )
    )
    text = body.decode()

    assert status == 200
    assert "id: 2" in text
    assert "id: 1\n" not in text
    assert "event: lane-event" in text
    assert "raw model text must not render" not in text
    assert "event: complete" in text


def test_observer_artifacts_require_public_attestation(tmp_path: Path) -> None:
    left, _right, _pair_id = _paired_bundles(tmp_path)
    unattested = left.write_artifact("unattested.json", {})
    repository = ObserverRepository(tmp_path)

    assert repository.resolve_artifact(left.manifest.run_id, "incident-report.json").is_file()
    assert unattested.is_file()
    try:
        repository.resolve_artifact(left.manifest.run_id, "unattested.json")
    except KeyError:
        pass
    else:
        raise AssertionError("unattested artifact was exposed")


def test_observer_token_file_is_reused_and_owner_only(tmp_path: Path) -> None:
    token_path = tmp_path / "observer.token"

    first = load_or_create_observer_token(token_path)
    second = load_or_create_observer_token(token_path)

    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


async def _asgi_request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    query: str = "",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    host: str = "127.0.0.1",
) -> tuple[int, dict[str, str], bytes]:
    encoded = json.dumps(body).encode() if body is not None else b""
    request_headers = {"host": host, **(headers or {})}
    if body is not None:
        request_headers["content-type"] = "application/json"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8787),
        "root_path": "",
    }
    sent_request = False
    never_disconnect = asyncio.Event()
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        await never_disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    response_headers = {key.decode(): value.decode() for key, value in start.get("headers", [])}
    return start["status"], response_headers, response_body
