"""Validate LangGraph approval restart with the production-intended Cosmos backend."""

from __future__ import annotations

import argparse
import json
import os
from typing import TypedDict

from langchain_azure_cosmosdb import CosmosDBSaverSync
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent_lab.checkpoint_backends import CosmosCheckpointConfig

_BACKEND_FORMAT = "langchain-azure-cosmosdb-checkpoint/v1"
_GRAPH_NAME = "agent-lab-approval-restart-cosmos"


class _ApprovalState(TypedDict):
    request_id: str
    phase: str
    decision: str


def _approval_node(state: _ApprovalState) -> dict[str, str]:
    response = interrupt({"phase": "awaiting_approval", "request_id": state["request_id"]})
    return {"phase": "resumed", "decision": str(response["decision"])}


def _saver() -> CosmosDBSaverSync:
    config = CosmosCheckpointConfig.from_environment()
    return CosmosDBSaverSync(
        config.database,
        config.container,
        endpoint=config.endpoint,
        key=config.credential_value(),
        cosmos_client_kwargs=config.client_options(),
    )


def _graph(saver: CosmosDBSaverSync):
    builder = StateGraph(_ApprovalState)
    builder.add_node("approval", _approval_node)
    builder.add_edge(START, "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=saver, name=_GRAPH_NAME)


def _config(request_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    configurable = {"thread_id": request_id}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def prepare(request_id: str) -> dict[str, object]:
    with _saver() as saver:
        graph = _graph(saver)
        config = _config(request_id)
        result = graph.invoke(
            {"request_id": request_id, "phase": "awaiting_approval", "decision": ""},
            config,
        )
        snapshot = graph.get_state(config)
        checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
        if "__interrupt__" not in result or snapshot.next != ("approval",):
            raise RuntimeError("LangGraph Cosmos workflow did not persist one approval interrupt")
        return {
            "backend": "azure_cosmos_db",
            "checkpoint_format": _BACKEND_FORMAT,
            "framework": "langgraph",
            "phase": "awaiting_approval",
            "pid": os.getpid(),
            "request_id": request_id,
            "checkpoint_id": checkpoint_id,
            "next": list(snapshot.next),
        }


def resume(checkpoint_id: str, request_id: str, decision: str) -> dict[str, object]:
    with _saver() as saver:
        graph = _graph(saver)
        result = graph.invoke(
            Command(resume={"decision": decision}),
            _config(request_id, checkpoint_id),
        )
        latest = graph.get_state(_config(request_id))
        if result["request_id"] != request_id or result["phase"] != "resumed":
            raise RuntimeError("LangGraph Cosmos workflow did not resume the approval interrupt")
        return {
            "backend": "azure_cosmos_db",
            "checkpoint_format": _BACKEND_FORMAT,
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
    prepare_parser.add_argument("--request-id", required=True)
    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--checkpoint-id", required=True)
    resume_parser.add_argument("--request-id", required=True)
    resume_parser.add_argument("--decision", choices=["approve", "edit", "reject"], required=True)
    args = parser.parse_args()

    if args.phase == "prepare":
        result = prepare(args.request_id)
    else:
        result = resume(args.checkpoint_id, args.request_id, args.decision)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
