"""Append-only, framework-neutral run-bundle persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_lab.contracts import (
    EventKind,
    EventVisibility,
    NormalizedEvent,
    RunManifest,
    RunTerminal,
)


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
        (path / "checkpoints").mkdir()
        (path / "native").mkdir()
        (path / "metrics").mkdir()
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
        if destination.exists():
            raise FileExistsError(f"scores already exist at {destination}")
        destination.write_text(
            json.dumps(scores, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def write_artifact(
        self,
        name: str,
        artifact: BaseModel | dict[str, Any] | list[Any],
    ) -> Path:
        """Write one immutable JSON artifact below the bundle artifact directory."""

        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("artifact name must be a plain .json filename")
        destination = self.path / "artifacts" / name
        if destination.exists():
            raise FileExistsError(f"artifact already exists at {destination}")
        body = artifact.model_dump(mode="json") if isinstance(artifact, BaseModel) else artifact
        destination.write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return destination

    def write_terminal(self, terminal: RunTerminal) -> Path:
        """Write the terminal record exactly once and only for this bundle."""

        if terminal.run_id != self.manifest.run_id:
            raise ValueError("terminal run_id does not match bundle manifest")
        destination = self.path / "terminal.json"
        if destination.exists():
            raise FileExistsError(f"terminal record already exists at {destination}")
        destination.write_text(terminal.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return destination
