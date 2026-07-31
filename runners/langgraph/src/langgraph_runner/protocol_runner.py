"""LangGraph runner process entry point for the shared lifecycle protocol."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import version

from agent_lab.contracts import FrameworkLane
from agent_lab.runner_host import (
    NativeRunnerDescriptor,
    descriptor_payload,
    run_protocol_probe,
)
from agent_lab.runner_protocol import CheckpointCapability


def descriptor() -> NativeRunnerDescriptor:
    return NativeRunnerDescriptor(
        name="langgraph-runner",
        supported_lanes=(
            FrameworkLane.LANGGRAPH,
            FrameworkLane.DEEP_AGENTS,
        ),
        framework_versions={
            "deepagents": version("deepagents"),
            "langchain": version("langchain"),
            "langchain-mcp-adapters": version("langchain-mcp-adapters"),
            "langchain-openai": version("langchain-openai"),
            "langgraph": version("langgraph"),
        },
        topology={
            FrameworkLane.LANGGRAPH: {
                "kind": "explicit_workflow",
                "runtime": "langgraph",
                "nodes": [
                    "telemetry_investigator",
                    "change_investigator",
                    "reliability_reviewer",
                    "evidence_join",
                    "incident_commander",
                    "approval",
                    "mitigation",
                    "recovery_verification",
                ],
                "parallel_fanout": True,
            },
            FrameworkLane.DEEP_AGENTS: {
                "kind": "opinionated_harness",
                "runtime": "deep_agents",
                "coordinator": "incident_commander",
                "subagents": [
                    "telemetry_investigator",
                    "change_investigator",
                    "reliability_reviewer",
                ],
            },
        },
        checkpoint={
            FrameworkLane.LANGGRAPH: CheckpointCapability(
                resumable=False,
                restart_safe=False,
                format="pending-langgraph-implementation",
            ),
            FrameworkLane.DEEP_AGENTS: CheckpointCapability(
                resumable=False,
                restart_safe=False,
                format="pending-deep-agents-implementation",
            ),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--describe", action="store_true")
    mode.add_argument("--protocol-probe", action="store_true")
    args = parser.parse_args()

    runner = descriptor()
    if args.describe:
        print(json.dumps(descriptor_payload(runner), sort_keys=True))
        return
    raise SystemExit(run_protocol_probe(runner))


if __name__ == "__main__":
    main()
