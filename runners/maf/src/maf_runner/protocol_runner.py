"""MAF runner process entry point for the shared lifecycle protocol."""

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
    if request.manifest.lane is not FrameworkLane.MAF_WORKFLOW:
        raise ValueError("real execution is implemented only for maf_workflow")
    from maf_runner.workflow_lane import run_live_approved

    return await run_live_approved(request, approval_provider, telemetry)


def descriptor() -> NativeRunnerDescriptor:
    return NativeRunnerDescriptor(
        name="maf-runner",
        supported_lanes=(
            FrameworkLane.MAF_WORKFLOW,
            FrameworkLane.MAF_HARNESS,
        ),
        framework_versions={
            "agent-framework-core": version("agent-framework-core"),
            "agent-framework-openai": version("agent-framework-openai"),
            "agent-framework-orchestrations": version("agent-framework-orchestrations"),
            "agent-framework-tools": version("agent-framework-tools"),
        },
        topology={
            FrameworkLane.MAF_WORKFLOW: {
                "kind": "explicit_workflow",
                "runtime": "agent_framework_workflows",
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
            FrameworkLane.MAF_HARNESS: {
                "kind": "opinionated_harness",
                "runtime": "agent_framework_harness",
                "coordinator": "incident_commander",
                "background_agents": [
                    "telemetry_investigator",
                    "change_investigator",
                    "reliability_reviewer",
                ],
            },
        },
        checkpoint={
            FrameworkLane.MAF_WORKFLOW: CheckpointCapability(
                resumable=True,
                restart_safe=True,
                format="agent-framework-file-checkpoint/v1",
            ),
            FrameworkLane.MAF_HARNESS: CheckpointCapability(
                resumable=False,
                restart_safe=False,
                format="pending-maf-harness-implementation",
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
