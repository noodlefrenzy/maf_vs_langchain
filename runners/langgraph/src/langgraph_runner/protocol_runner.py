"""LangGraph runner process entry point for the shared lifecycle protocol."""

from __future__ import annotations

import argparse
import asyncio
import json
from importlib.metadata import version

from agent_lab.contracts import FrameworkLane
from agent_lab.runner_host import (
    ApprovalProvider,
    NativeRunnerDescriptor,
    descriptor_payload,
    run_protocol_probe,
    run_workflow_protocol,
)
from agent_lab.runner_protocol import CheckpointCapability, RunRequest
from agent_lab.workflow_telemetry import WorkflowTelemetry


async def _execute_workflow(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, object]:
    if request.manifest.lane is FrameworkLane.LANGGRAPH:
        from langgraph_runner.workflow_lane import run_live_approved
    elif request.manifest.lane is FrameworkLane.DEEP_AGENTS:
        from langgraph_runner.harness_lane import run_live_approved
    else:
        raise ValueError(f"unsupported LangGraph-family lane {request.manifest.lane.value}")

    return await run_live_approved(request, approval_provider, telemetry)


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
                resumable=True,
                restart_safe=True,
                format="langgraph-checkpoint-v4+agent-lab-json-envelope/v1",
            ),
            FrameworkLane.DEEP_AGENTS: CheckpointCapability(
                resumable=False,
                restart_safe=False,
                format="deep-agents-process-local/v1",
            ),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--describe", action="store_true")
    mode.add_argument("--protocol-probe", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args()

    runner = descriptor()
    if args.describe:
        print(json.dumps(descriptor_payload(runner), sort_keys=True))
        return
    if args.run:
        raise SystemExit(asyncio.run(run_workflow_protocol(runner, _execute_workflow)))
    raise SystemExit(run_protocol_probe(runner))


if __name__ == "__main__":
    main()
