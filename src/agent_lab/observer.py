"""Framework-neutral live and replay observer over immutable run bundles."""

# The embedded, dependency-free browser asset intentionally contains minified CSS/JavaScript.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import stat
import threading
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from agent_lab.contracts import (
    EventKind,
    EventVisibility,
    NormalizedEvent,
    RunManifest,
    RunTerminal,
)
from agent_lab.observer_contracts import (
    OBSERVER_APPROVAL_SCHEMA,
    OBSERVER_PAIR_CONTEXT_SCHEMA,
    ObserverApprovalDecision,
    ObserverApprovalSubmission,
    approval_decision_path,
    observer_pair_id,
)
from agent_lab.scenario import MitigationActionId

_AUDIT_SCHEMA = "observer-approval-audit/v1"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_VISIBLE_PAYLOAD_FIELDS: dict[EventKind, frozenset[str]] = {
    EventKind.RUN_STARTED: frozenset({"execution_mode"}),
    EventKind.RUN_FINISHED: frozenset(
        {"status", "reason", "execution_mode", "task_success", "total_score"}
    ),
    EventKind.AGENT_STARTED: frozenset({"phase", "namespace"}),
    EventKind.AGENT_FINISHED: frozenset({"phase", "namespace", "status"}),
    EventKind.MODEL_REQUESTED: frozenset({"model", "namespace"}),
    EventKind.MODEL_RESPONDED: frozenset(
        {
            "model",
            "namespace",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "parser_outcome",
            "provider_request_id",
            "queue_seconds",
            "time_to_first_token_seconds",
            "generation_seconds",
        }
    ),
    EventKind.TEXT_DELTA: frozenset(),
    EventKind.TOOL_REQUESTED: frozenset({"tool_name", "namespace"}),
    EventKind.TOOL_STARTED: frozenset({"tool_name", "namespace"}),
    EventKind.TOOL_FINISHED: frozenset({"tool_name", "namespace"}),
    EventKind.TOOL_FAILED: frozenset(
        {"tool_name", "namespace", "error_code", "retryable", "reason"}
    ),
    EventKind.HANDOFF: frozenset(
        {
            "source_actor",
            "target_actor",
            "source_namespace",
            "target_namespace",
            "phase",
        }
    ),
    EventKind.STATE_UPDATED: frozenset({"phase", "status"}),
    EventKind.CHECKPOINT_SAVED: frozenset({"reason", "request_id", "checkpoint_id"}),
    EventKind.APPROVAL_REQUESTED: frozenset(
        {
            "request_id",
            "case_id",
            "proposed_action_id",
            "proposed_action",
            "rationale",
            "evidence_ids",
            "requested_by",
            "created_tick",
            "status",
        }
    ),
    EventKind.APPROVAL_DECIDED: frozenset(
        {
            "request_id",
            "decision",
            "decided_by",
            "decided_tick",
            "action_id_to_execute",
            "action_to_execute",
        }
    ),
    EventKind.ARTIFACT_WRITTEN: frozenset({"artifact", "sha256"}),
    EventKind.METRIC_RECORDED: frozenset(
        {
            "name",
            "model",
            "namespace",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "parser_outcome",
            "provider_request_id",
            "queue_seconds",
            "time_to_first_token_seconds",
            "generation_seconds",
            "value",
            "unit",
        }
    ),
    EventKind.ERROR: frozenset({"code", "kind", "phase", "reason", "retryable"}),
}


@dataclass(frozen=True)
class PairRecord:
    pair_id: str
    track: str
    seed: int
    repetition: int
    index_file: str | None
    run_ids: tuple[UUID, UUID]


class ObserverRepository:
    """Read-only projection of replay and in-progress normalized bundle evidence."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root.resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._approval_lock = threading.Lock()

    def list_pairs(self) -> list[dict[str, Any]]:
        return [self.pair_snapshot(record, include_events=False) for record in self._pairs()]

    def get_pair(self, pair_id: str) -> PairRecord:
        for record in self._pairs():
            if hmac.compare_digest(record.pair_id, pair_id):
                return record
        raise KeyError(f"observer pair {pair_id!r} was not found")

    def pair_snapshot(
        self,
        record: PairRecord,
        *,
        include_events: bool = True,
    ) -> dict[str, Any]:
        lanes = [
            self.run_snapshot(run_id, include_events=include_events) for run_id in record.run_ids
        ]
        return {
            "schema_version": "observer-pair-snapshot/v1",
            "pair_id": record.pair_id,
            "track": record.track,
            "seed": record.seed,
            "repetition": record.repetition,
            "index_file": record.index_file,
            "complete": all(lane["terminal"] is not None for lane in lanes),
            "lanes": lanes,
        }

    def run_snapshot(self, run_id: UUID, *, include_events: bool = True) -> dict[str, Any]:
        bundle = self.bundle_for_run(run_id)
        manifest = _read_manifest(bundle)
        events = self.events_after(run_id, -1)
        terminal = _read_optional_model(bundle / "terminal.json", RunTerminal)
        trace_link = _read_optional_object(bundle / "native" / "trace-link.json")
        artifact_links = []
        for event in events:
            if event["kind"] != EventKind.ARTIFACT_WRITTEN.value:
                continue
            artifact = event["payload"].get("artifact")
            if isinstance(artifact, str) and artifact.startswith("artifacts/"):
                artifact_links.append(
                    {
                        "path": artifact,
                        "url": (
                            f"/api/runs/{run_id}/artifacts/{artifact.removeprefix('artifacts/')}"
                        ),
                    }
                )
        pending = _pending_approval(events)
        return {
            "schema_version": "observer-run-snapshot/v1",
            "run_id": str(run_id),
            "lane": manifest.lane.value,
            "experiment_id": manifest.experiment_id,
            "started_at": manifest.started_at.isoformat(),
            "status": terminal.status.value if terminal is not None else "running",
            "terminal": terminal.model_dump(mode="json") if terminal is not None else None,
            "budget": _budget_view(manifest, terminal, events),
            "active_agents": _active_agents(events),
            "pending_approval": pending,
            "phoenix_trace_url": _safe_phoenix_url(trace_link),
            "artifacts": artifact_links,
            "last_sequence": events[-1]["sequence"] if events else -1,
            "events": events if include_events else [],
        }

    def events_after(self, run_id: UUID, sequence: int) -> list[dict[str, Any]]:
        bundle = self.bundle_for_run(run_id)
        return [
            _event_view(event)
            for event in _read_events(bundle / "events.jsonl")
            if event.sequence > sequence and event.visibility is EventVisibility.PUBLIC
        ]

    def bundle_for_run(self, run_id: UUID) -> Path:
        bundle = (self.runs_root / str(run_id)).resolve()
        if bundle.parent != self.runs_root or not (bundle / "manifest.json").is_file():
            raise KeyError(f"run {run_id} was not found below {self.runs_root}")
        manifest = _read_manifest(bundle)
        if manifest.run_id != run_id:
            raise ValueError("bundle directory and manifest run IDs differ")
        return bundle

    def resolve_artifact(self, run_id: UUID, artifact_name: str) -> Path:
        if not artifact_name or Path(artifact_name).is_absolute():
            raise ValueError("artifact path must be relative")
        bundle = self.bundle_for_run(run_id)
        artifact_root = (bundle / "artifacts").resolve()
        candidate = (artifact_root / artifact_name).resolve()
        if not candidate.is_relative_to(artifact_root) or not candidate.is_file():
            raise KeyError(f"artifact {artifact_name!r} was not found")
        observed = {
            str(event.payload.get("artifact"))
            for event in _read_events(bundle / "events.jsonl")
            if event.visibility is EventVisibility.PUBLIC
            and event.kind is EventKind.ARTIFACT_WRITTEN
        }
        relative = candidate.relative_to(artifact_root).as_posix()
        if f"artifacts/{relative}" not in observed:
            raise KeyError("artifact is not attested by a public normalized event")
        return candidate

    def submit_approval(
        self,
        run_id: UUID,
        submission: ObserverApprovalSubmission,
        *,
        decided_by: str,
    ) -> ObserverApprovalDecision:
        bundle = self.bundle_for_run(run_id)
        terminal = bundle / "terminal.json"
        events = self.events_after(run_id, -1)
        pending = _pending_approval(events)
        accepted = False
        reason = "unknown"
        try:
            if terminal.exists():
                reason = "run_already_terminal"
                raise ValueError("a terminal run cannot accept an approval decision")
            if (bundle / "control" / "approval-closed.json").exists():
                reason = "approval_inbox_closed"
                raise ValueError("the run's approval inbox is closed")
            if pending is None:
                reason = "no_pending_approval"
                raise ValueError("the run has no pending approval request")
            if pending.get("request_id") != submission.request_id:
                reason = "request_id_mismatch"
                raise ValueError("approval request ID differs from the pending request")
            decision = ObserverApprovalDecision(
                **submission.model_dump(mode="python"),
                run_id=run_id,
                decided_by=decided_by,
            )
            control = bundle / "control"
            control.mkdir(mode=0o700, exist_ok=True)
            control.chmod(0o700)
            destination = approval_decision_path(bundle, submission.request_id)
            body = (decision.model_dump_json(indent=2) + "\n").encode("utf-8")
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
            accepted = True
            reason = "accepted"
            return decision
        finally:
            self._append_approval_audit(
                bundle,
                {
                    "schema_version": _AUDIT_SCHEMA,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "run_id": str(run_id),
                    "request_id": submission.request_id,
                    "decision": submission.decision.value,
                    "edited_action_id": (
                        submission.edited_action_id.value
                        if submission.edited_action_id is not None
                        else None
                    ),
                    "decided_by": decided_by,
                    "accepted": accepted,
                    "reason": reason,
                },
            )

    def _append_approval_audit(self, bundle: Path, payload: dict[str, Any]) -> None:
        control = bundle / "control"
        control.mkdir(mode=0o700, exist_ok=True)
        control.chmod(0o700)
        audit = control / "approval-api-audit.jsonl"
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._approval_lock:
            descriptor = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "ab") as handle:
                handle.write(encoded)

    def _pairs(self) -> list[PairRecord]:
        records: dict[frozenset[UUID], PairRecord] = {}
        for index_path in sorted(self.runs_root.glob("native-workflow-*.json")):
            payload = _read_optional_object(index_path)
            if payload is None:
                continue
            for block in payload.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                grouped: dict[str, list[UUID]] = {}
                for run in block.get("runs", []):
                    if not isinstance(run, dict):
                        continue
                    try:
                        run_id = UUID(str(run["run_id"]))
                        self.bundle_for_run(run_id)
                    except (KeyError, ValueError):
                        continue
                    grouped.setdefault(str(run.get("track", "unknown")), []).append(run_id)
                for track, run_ids in grouped.items():
                    if len(run_ids) != 2 or len(set(run_ids)) != 2:
                        continue
                    pair_key = frozenset(run_ids)
                    records[pair_key] = PairRecord(
                        pair_id=observer_pair_id(
                            index_path.name,
                            int(block.get("seed", block.get("case", {}).get("seed", 0))),
                            int(block.get("repetition", 0)),
                            track,
                        ),
                        track=track,
                        seed=int(block.get("seed", block.get("case", {}).get("seed", 0))),
                        repetition=int(block.get("repetition", 0)),
                        index_file=index_path.name,
                        run_ids=(run_ids[0], run_ids[1]),
                    )
        for context_path in sorted(self.runs_root.glob("*/native/observer-pair.json")):
            context = _read_optional_object(context_path)
            if context is None or context.get("schema_version") != OBSERVER_PAIR_CONTEXT_SCHEMA:
                continue
            try:
                run_ids = tuple(UUID(str(value)) for value in context["run_ids"])
                if len(run_ids) != 2 or len(set(run_ids)) != 2:
                    continue
                context_run_id = UUID(context_path.parents[1].name)
                if context_run_id not in run_ids:
                    continue
                for run_id in run_ids:
                    self.bundle_for_run(run_id)
                index_file = str(context["index_file"])
                track = str(context["track"])
                seed = int(context["seed"])
                repetition = int(context["repetition"])
                if context.get("pair_id") != observer_pair_id(
                    index_file,
                    seed,
                    repetition,
                    track,
                ):
                    continue
            except (KeyError, ValueError):
                continue
            pair_key = frozenset(run_ids)
            records.setdefault(
                pair_key,
                PairRecord(
                    pair_id=str(context["pair_id"]),
                    track=track,
                    seed=seed,
                    repetition=repetition,
                    index_file=index_file,
                    run_ids=(run_ids[0], run_ids[1]),
                ),
            )
        return sorted(
            records.values(), key=lambda record: (record.index_file or "", record.pair_id)
        )


def build_observer_app(
    runs_root: Path,
    *,
    approval_token: str | None,
    operator_id: str = "local-observer-operator",
    poll_interval_seconds: float = 0.25,
) -> FastAPI:
    """Build a loopback-only FastAPI observer over normalized bundle evidence."""

    if approval_token is not None and len(approval_token) < 32:
        raise ValueError("observer approval token must contain at least 32 characters")
    if not operator_id.strip():
        raise ValueError("observer operator ID must not be blank")
    if poll_interval_seconds <= 0:
        raise ValueError("observer poll interval must be positive")
    repository = ObserverRepository(runs_root)
    app = FastAPI(title="Agent Lab neutral observer", docs_url=None, redoc_url=None)
    app.state.repository = repository

    @app.middleware("http")
    async def require_loopback(request: Request, call_next):
        client_host = request.client.host if request.client is not None else ""
        if request.url.hostname not in _LOOPBACK_HOSTS or client_host not in _LOOPBACK_HOSTS:
            return JSONResponse({"detail": "observer is loopback-only"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _OBSERVER_HTML

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        return {
            "schema_version": "observer-meta/v1",
            "approval_controls_enabled": approval_token is not None,
            "mitigation_action_ids": [action.value for action in MitigationActionId],
            "raw_reasoning_rendered": False,
        }

    @app.get("/api/pairs")
    async def pairs() -> dict[str, Any]:
        return {"schema_version": "observer-pair-list/v1", "pairs": repository.list_pairs()}

    @app.get("/api/pairs/{pair_id}")
    async def pair(pair_id: str) -> dict[str, Any]:
        try:
            return repository.pair_snapshot(repository.get_pair(pair_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    async def run(run_id: UUID) -> dict[str, Any]:
        try:
            return repository.run_snapshot(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    async def events(
        request: Request,
        run_id: UUID,
        after: Annotated[int, Query(ge=-1)] = -1,
        follow: bool = True,
    ) -> StreamingResponse:
        try:
            repository.bundle_for_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return StreamingResponse(
            _event_stream(
                request,
                repository,
                run_id,
                after=after,
                follow=follow,
                poll_interval_seconds=poll_interval_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}/artifacts/{artifact_name:path}")
    async def artifact(run_id: UUID, artifact_name: str) -> FileResponse:
        try:
            path = repository.resolve_artifact(run_id, artifact_name)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path, media_type="application/json", filename=path.name)

    @app.post("/api/runs/{run_id}/approval")
    async def approve(
        request: Request,
        run_id: UUID,
        submission: ObserverApprovalSubmission,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if approval_token is None:
            raise HTTPException(status_code=503, detail="approval controls are disabled")
        client_host = request.client.host if request.client is not None else ""
        if client_host not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail="approval controls are loopback-only")
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential, approval_token):
            raise HTTPException(status_code=401, detail="invalid observer approval credential")
        try:
            decision = repository.submit_approval(
                run_id,
                submission,
                decided_by=operator_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (FileExistsError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "schema_version": OBSERVER_APPROVAL_SCHEMA,
            "status": "accepted",
            "run_id": str(decision.run_id),
            "request_id": decision.request_id,
            "decision": decision.decision.value,
            "decided_by": decision.decided_by,
            "decided_at": decision.decided_at.isoformat(),
        }

    return app


async def _event_stream(
    request: Request,
    repository: ObserverRepository,
    run_id: UUID,
    *,
    after: int,
    follow: bool,
    poll_interval_seconds: float,
) -> AsyncIterator[str]:
    cursor = after
    while True:
        events = repository.events_after(run_id, cursor)
        for event in events:
            cursor = int(event["sequence"])
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
            yield f"id: {cursor}\nevent: lane-event\ndata: {encoded}\n\n"
        terminal = (repository.bundle_for_run(run_id) / "terminal.json").is_file()
        if not follow or terminal:
            yield f'event: complete\ndata: {{"last_sequence":{cursor}}}\n\n'
            return
        if await request.is_disconnected():
            return
        yield ": keep-alive\n\n"
        await asyncio.sleep(poll_interval_seconds)


def load_or_create_observer_token(path: Path) -> str:
    """Load or exclusively create an owner-only local approval credential."""

    path = path.resolve()
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"observer credential file must be owner-only, found {mode:o}")
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("observer credential file contains an invalid token")
        return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def fetch_pair_snapshot(base_url: str, pair_id: str) -> dict[str, Any]:
    """Fetch one observer pair through the same read API used by the browser."""

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("observer base URL must use HTTP loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("observer base URL cannot contain credentials, query, or fragment")
    if (
        len(pair_id) != 25
        or not pair_id.startswith("pair-")
        or any(character not in "0123456789abcdef" for character in pair_id.removeprefix("pair-"))
    ):
        raise ValueError("observer pair ID is malformed")
    url = f"{base_url.rstrip('/')}/api/pairs/{pair_id}"
    with urlopen(url, timeout=5) as response:  # noqa: S310 - loopback URL is validated above
        return json.loads(response.read())


def render_pair_snapshot(snapshot: dict[str, Any], *, console: Console | None = None) -> None:
    """Render the browser API's sanitized pair projection with Rich."""

    output = console or Console()
    lanes = snapshot.get("lanes", [])
    if len(lanes) != 2:
        raise ValueError("observer pair snapshot must contain exactly two lanes")
    table = Table(title=f"{snapshot['track']} · seed {snapshot['seed']} · {snapshot['pair_id']}")
    for lane in lanes:
        table.add_column(f"{lane['lane']} · {lane['status']}", overflow="fold")
    rows = max(len(lane.get("events", [])) for lane in lanes)
    for index in range(rows):
        cells = []
        for lane in lanes:
            events = lane.get("events", [])
            if index >= len(events):
                cells.append("")
                continue
            event = events[index]
            actor = f" [{event['actor']}]" if event.get("actor") else ""
            cells.append(
                f"{event['sequence']:>3} {event['kind']}{actor}\n{_payload_summary(event)}"
            )
        table.add_row(*cells)
    output.print(table)


def _read_manifest(bundle: Path) -> RunManifest:
    return RunManifest.model_validate_json((bundle / "manifest.json").read_text(encoding="utf-8"))


def _read_events(path: Path) -> list[NormalizedEvent]:
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    events = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(NormalizedEvent.model_validate_json(line))
        except Exception:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                break
            raise
    return events


def _read_optional_model(path: Path, model: type[BaseModel]) -> BaseModel | None:
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _read_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_phoenix_url(trace_link: dict[str, Any] | None) -> str | None:
    if trace_link is None or not isinstance(trace_link.get("phoenix_trace_url"), str):
        return None
    value = trace_link["phoenix_trace_url"]
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    return value


def _event_view(event: NormalizedEvent) -> dict[str, Any]:
    allowed = _VISIBLE_PAYLOAD_FIELDS[event.kind]
    payload = {key: value for key, value in event.payload.items() if key in allowed}
    return {
        "schema_version": "observer-event/v1",
        "run_id": str(event.run_id),
        "sequence": event.sequence,
        "observed_at": event.observed_at.isoformat(),
        "lane": event.lane.value,
        "kind": event.kind.value,
        "actor": event.actor,
        "parent_event_id": str(event.parent_event_id) if event.parent_event_id else None,
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "payload": payload,
    }


def _pending_approval(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    requests: dict[str, dict[str, Any]] = {}
    decided: set[str] = set()
    for event in events:
        request_id = event["payload"].get("request_id")
        if not isinstance(request_id, str):
            continue
        if event["kind"] == EventKind.APPROVAL_REQUESTED.value:
            requests[request_id] = event["payload"]
        elif event["kind"] == EventKind.APPROVAL_DECIDED.value:
            decided.add(request_id)
    pending = [payload for request_id, payload in requests.items() if request_id not in decided]
    return pending[-1] if pending else None


def _active_agents(events: list[dict[str, Any]]) -> list[str]:
    active: Counter[str] = Counter()
    for event in events:
        actor = event.get("actor")
        if not isinstance(actor, str):
            continue
        if event["kind"] == EventKind.AGENT_STARTED.value:
            active[actor] += 1
        elif event["kind"] == EventKind.AGENT_FINISHED.value and active[actor] > 0:
            active[actor] -= 1
    return sorted(actor for actor, count in active.items() if count > 0)


def _budget_view(
    manifest: RunManifest,
    terminal: RunTerminal | None,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    output_tokens = sum(
        int(event["payload"].get("output_tokens", 0) or 0)
        for event in events
        if event["kind"] == EventKind.MODEL_RESPONDED.value
    )
    used = {
        "model_calls": sum(event["kind"] == EventKind.MODEL_REQUESTED.value for event in events),
        "tool_calls": sum(event["kind"] == EventKind.TOOL_REQUESTED.value for event in events),
        "output_tokens": output_tokens,
        "approval_requests": sum(
            event["kind"] == EventKind.APPROVAL_REQUESTED.value for event in events
        ),
        "wall_clock_seconds": terminal.elapsed_seconds if terminal is not None else None,
    }
    return {"limits": manifest.budget.model_dump(mode="json"), "used": used}


def _payload_summary(event: dict[str, Any]) -> str:
    payload = event.get("payload", {})
    preferred = (
        "phase",
        "tool_name",
        "target_actor",
        "request_id",
        "evidence_ids",
        "decision",
        "status",
        "artifact",
        "reason",
    )
    values = [f"{key}={payload[key]}" for key in preferred if key in payload]
    return " · ".join(values)


_OBSERVER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Agent Lab neutral observer</title><style>
:root{color-scheme:dark;font:14px system-ui;background:#0b1020;color:#e5e7eb}body{margin:0}
header{position:sticky;top:0;z-index:2;background:#111827;padding:12px 18px;border-bottom:1px solid #334155}
select,input,button{background:#1e293b;color:#e5e7eb;border:1px solid #475569;border-radius:5px;padding:6px}
button{cursor:pointer}.meta{color:#94a3b8;margin-left:10px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px}
.lane{min-width:0;background:#111827;border:1px solid #334155;border-radius:8px;overflow:hidden}.lane-head{padding:12px;border-bottom:1px solid #334155}
.events{height:70vh;overflow:auto;padding:8px}.event{border-left:3px solid #64748b;margin:6px 0;padding:7px 9px;background:#0f172a}
.event.agent\2e handoff{border-color:#a78bfa}.event.tool\2e requested,.event.tool\2e finished{border-color:#38bdf8}
.event.approval\2e requested,.event.approval\2e decided{border-color:#f59e0b}.event.error,.event.tool\2e failed{border-color:#ef4444}
.seq,.time{color:#94a3b8;font-size:12px}.payload{color:#cbd5e1;white-space:pre-wrap;overflow-wrap:anywhere}.links a{color:#7dd3fc;margin-right:8px}
.approval{margin-top:8px;padding:8px;background:#3b2f12}.approval button{margin:4px}.budget{font-family:ui-monospace,monospace;font-size:12px}
@media(max-width:800px){.grid{grid-template-columns:1fr}.events{height:45vh}}
</style></head><body><header><select id="pairs"></select> <button id="reload">Reload</button>
<input id="token" type="password" autocomplete="off" placeholder="approval token"><span id="status" class="meta"></span></header>
<main id="grid" class="grid"></main><script>
const pairSelect=document.querySelector('#pairs'),grid=document.querySelector('#grid'),statusEl=document.querySelector('#status');let streams=[];
document.querySelector('#token').value=sessionStorage.getItem('observerToken')||'';
document.querySelector('#token').onchange=e=>sessionStorage.setItem('observerToken',e.target.value);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function getJSON(url,options){const r=await fetch(url,options);if(!r.ok)throw new Error(await r.text());return r.json()}
function summary(e){const p=e.payload||{},keys=['phase','tool_name','target_actor','request_id','evidence_ids','decision','status','artifact','reason'];return keys.filter(k=>k in p).map(k=>`${k}=${p[k]}`).join(' · ')}
function eventNode(e){const d=document.createElement('div');d.className=`event ${e.kind}`;d.innerHTML=`<span class="seq">#${e.sequence}</span> <b>${esc(e.kind)}</b> ${esc(e.actor||'')}<br><span class="time">${esc(e.observed_at)}</span><div class="payload">${esc(summary(e))}</div>`;return d}
function budget(b){return Object.keys(b.limits).map(k=>`${k}: ${b.used[k]??'…'} / ${b.limits[k]}`).join('<br>')}
function approval(lane){const p=lane.pending_approval;if(!p)return'';return `<div class="approval" data-request="${esc(p.request_id)}"><b>Approval pending</b><br>${esc(p.proposed_action_id)}<br><button data-d="approve">Approve</button><button data-d="reject">Reject</button><button data-d="edit">Edit</button></div>`}
function laneNode(lane){const section=document.createElement('section');section.className='lane';section.dataset.run=lane.run_id;const links=[...(lane.phoenix_trace_url?[`<a href="${esc(lane.phoenix_trace_url)}" target="_blank">Phoenix</a>`]:[]),...lane.artifacts.map(a=>`<a href="${esc(a.url)}" target="_blank">${esc(a.path.split('/').pop())}</a>`)];section.innerHTML=`<div class="lane-head"><h2>${esc(lane.lane)} · ${esc(lane.status)}</h2><div class="budget">${budget(lane.budget)}</div><div>Active: ${esc(lane.active_agents.join(', ')||'none')}</div><div class="links">${links.join('')}</div>${approval(lane)}</div><div class="events"></div>`;const events=section.querySelector('.events');lane.events.forEach(e=>events.append(eventNode(e)));section.querySelectorAll('.approval button').forEach(b=>b.onclick=()=>decide(lane,b.dataset.d));return section}
async function decide(lane,decision){const pending=lane.pending_approval,body={request_id:pending.request_id,decision};if(decision==='edit'){const actions=(await getJSON('/api/meta')).mitigation_action_ids;const selected=prompt(`Edited action ID:\n${actions.join('\n')}`,pending.proposed_action_id);if(!selected)return;body.edited_action_id=selected}try{await getJSON(`/api/runs/${lane.run_id}/approval`,{method:'POST',headers:{'Content-Type':'application/json','Authorization':`Bearer ${document.querySelector('#token').value}`},body:JSON.stringify(body)});await loadPair()}catch(e){alert(e)}}
function connect(lane){const source=new EventSource(`/api/runs/${lane.run_id}/events?after=${lane.last_sequence}&follow=true`);streams.push(source);source.addEventListener('lane-event',ev=>{const e=JSON.parse(ev.data),box=document.querySelector(`[data-run="${lane.run_id}"] .events`);box.append(eventNode(e));box.scrollTop=box.scrollHeight;if(e.kind.startsWith('approval.')||e.kind==='run.finished')setTimeout(loadPair,100)});source.addEventListener('complete',()=>source.close())}
async function loadPair(){streams.forEach(s=>s.close());streams=[];if(!pairSelect.value)return;const pair=await getJSON(`/api/pairs/${pairSelect.value}`);grid.innerHTML='';pair.lanes.forEach(l=>grid.append(laneNode(l)));pair.lanes.forEach(connect);statusEl.textContent=`${pair.track} · seed ${pair.seed} · ${pair.complete?'replay complete':'live'}`}
async function loadPairs(){const data=await getJSON('/api/pairs'),old=pairSelect.value;pairSelect.innerHTML='';data.pairs.forEach(p=>{const o=document.createElement('option');o.value=p.pair_id;o.textContent=`${p.track} · seed ${p.seed} · ${p.lanes.map(l=>l.lane).join(' vs ')}`;pairSelect.append(o)});if([...pairSelect.options].some(o=>o.value===old))pairSelect.value=old;await loadPair()}
pairSelect.onchange=loadPair;document.querySelector('#reload').onclick=loadPairs;loadPairs().catch(e=>statusEl.textContent=e);
</script></body></html>"""
