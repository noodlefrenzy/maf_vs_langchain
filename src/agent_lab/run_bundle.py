"""Append-only, framework-neutral run-bundle persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_lab.contracts import EventKind, EventVisibility, NormalizedEvent, RunManifest


class RunBundle:
    """A local evidence bundle for one run.

    The directory is created exclusively so a rerun cannot silently overwrite
    evidence. Event sequence numbers are assigned by this writer.
    """

    def __init__(self, path: Path, manifest: RunManifest) -> None:
        self.path = path
        self.manifest = manifest
        self._next_sequence = 0

    @classmethod
    def create(cls, root: Path, manifest: RunManifest) -> RunBundle:
        path = root / str(manifest.run_id)
        path.mkdir(parents=True, exist_ok=False)
        (path / "artifacts").mkdir()
        (path / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (path / "events.jsonl").touch()
        return cls(path=path, manifest=manifest)

    def emit(
        self,
        kind: EventKind,
        *,
        actor: str | None = None,
        visibility: EventVisibility = EventVisibility.PUBLIC,
        payload: dict[str, Any] | None = None,
        **event_fields: Any,
    ) -> NormalizedEvent:
        event = NormalizedEvent(
            run_id=self.manifest.run_id,
            sequence=self._next_sequence,
            lane=self.manifest.lane,
            kind=kind,
            actor=actor,
            visibility=visibility,
            payload=payload or {},
            **event_fields,
        )
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        self._next_sequence += 1
        return event

    def write_scores(self, scores: dict[str, Any]) -> None:
        destination = self.path / "scores.json"
        destination.write_text(
            json.dumps(scores, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
