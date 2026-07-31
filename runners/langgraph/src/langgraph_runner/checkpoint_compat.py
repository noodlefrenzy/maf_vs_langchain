"""Exercise LangGraph approval checkpoint/resume across process restarts."""

from __future__ import annotations

import argparse
import base64
import json
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

_SCHEMA_VERSION = "agent-lab-langgraph-checkpoint/v1"


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, list):
        return {"type": "list", "items": [_encode(item) for item in value]}
    if isinstance(value, Mapping):
        return {
            "type": "mapping",
            "items": [[_encode(key), _encode(item)] for key, item in value.items()],
        }
    raise TypeError(f"unsupported checkpoint envelope value: {type(value).__name__}")


def _decode(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ValueError("invalid checkpoint envelope value")
    if value["type"] == "bytes":
        return base64.b64decode(value["value"], validate=True)
    if value["type"] == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if value["type"] == "list":
        return [_decode(item) for item in value["items"]]
    if value["type"] == "mapping":
        return {_decode(key): _decode(item) for key, item in value["items"]}
    raise ValueError(f"unknown checkpoint envelope type {value['type']!r}")


class _FileBackedSaver(InMemorySaver):
    """Persist LangGraph native serialized checkpoint rows in a safe JSON envelope."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self._file_lock = threading.RLock()
        parent_created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent_created:
            self.path.parent.chmod(0o700)
        if self.path.exists():
            self._load()

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        result = super().put(config, checkpoint, metadata, new_versions)
        self._persist()
        return result

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        super().put_writes(config, writes, task_id, task_path)
        self._persist()

    def _persist(self) -> None:
        with self._file_lock:
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "storage": _encode(self.storage),
                "writes": _encode(self.writes),
                "blobs": _encode(self.blobs),
            }
            rendered = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(rendered)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self.path.chmod(0o600)
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _load(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported LangGraph checkpoint envelope version")
        storage = _decode(payload["storage"])
        writes = _decode(payload["writes"])
        blobs = _decode(payload["blobs"])
        if not all(isinstance(value, dict) for value in (storage, writes, blobs)):
            raise ValueError("LangGraph checkpoint envelope must contain mappings")
        for thread_id, namespaces in storage.items():
            self.storage[thread_id].update(namespaces)
        self.writes.update(writes)
        self.blobs.update(blobs)


class _ApprovalState(TypedDict):
    request_id: str
    phase: str
    decision: str


def _approval_node(state: _ApprovalState) -> dict[str, str]:
    response = interrupt({"phase": "awaiting_approval", "request_id": state["request_id"]})
    return {"phase": "resumed", "decision": str(response["decision"])}


def _graph(saver: _FileBackedSaver):
    builder = StateGraph(_ApprovalState)
    builder.add_node("approval", _approval_node)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=saver, name="agent-lab-approval-restart-compat")


def _config(request_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    configurable = {"thread_id": request_id}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def prepare(checkpoint_path: Path, request_id: str) -> dict[str, object]:
    saver = _FileBackedSaver(checkpoint_path)
    graph = _graph(saver)
    config = _config(request_id)
    result = graph.invoke(
        {"request_id": request_id, "phase": "awaiting_approval", "decision": ""},
        config,
    )
    snapshot = graph.get_state(config)
    checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
    if "__interrupt__" not in result or snapshot.next != ("approval",):
        raise RuntimeError("LangGraph did not persist one approval interrupt")
    return {
        "framework": "langgraph",
        "phase": "awaiting_approval",
        "pid": os.getpid(),
        "request_id": request_id,
        "checkpoint_id": checkpoint_id,
        "next": list(snapshot.next),
    }


def resume(
    checkpoint_path: Path,
    checkpoint_id: str,
    request_id: str,
    decision: str,
) -> dict[str, object]:
    saver = _FileBackedSaver(checkpoint_path)
    graph = _graph(saver)
    result = graph.invoke(
        Command(resume={"decision": decision}),
        _config(request_id, checkpoint_id),
    )
    latest = graph.get_state(_config(request_id))
    if result["request_id"] != request_id or result["phase"] != "resumed":
        raise RuntimeError("LangGraph did not resume the approval interrupt")
    return {
        "framework": "langgraph",
        "phase": result["phase"],
        "pid": os.getpid(),
        "request_id": result["request_id"],
        "decision": result["decision"],
        "checkpoint_id": latest.config["configurable"]["checkpoint_id"],
        "resumed_from_checkpoint_id": checkpoint_id,
        "next": list(latest.next),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--checkpoint-path", type=Path, required=True)
    prepare_parser.add_argument("--request-id", required=True)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--checkpoint-path", type=Path, required=True)
    resume_parser.add_argument("--checkpoint-id", required=True)
    resume_parser.add_argument("--request-id", required=True)
    resume_parser.add_argument("--decision", choices=["approve", "edit", "reject"], required=True)
    args = parser.parse_args()

    if args.phase == "prepare":
        result = prepare(args.checkpoint_path, args.request_id)
    else:
        result = resume(
            args.checkpoint_path,
            args.checkpoint_id,
            args.request_id,
            args.decision,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
