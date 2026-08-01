"""Hermetic task stub for the real workflow protocol session."""

from __future__ import annotations

import argparse
import asyncio

from agent_lab.contracts import ApprovalRequest, FrameworkLane
from agent_lab.runner_host import (
    ApprovalProvider,
    NativeRunnerDescriptor,
    run_workflow_protocol,
)
from agent_lab.runner_protocol import CheckpointCapability, RunRequest
from agent_lab.workflow_telemetry import WorkflowTelemetry


def _descriptor() -> NativeRunnerDescriptor:
    return NativeRunnerDescriptor(
        name="workflow-protocol-stub",
        supported_lanes=(FrameworkLane.MAF_WORKFLOW,),
        framework_versions={"workflow-protocol-stub": "0.1.0"},
        topology={FrameworkLane.MAF_WORKFLOW: {"kind": "hermetic_task_stub"}},
        checkpoint={
            FrameworkLane.MAF_WORKFLOW: CheckpointCapability(
                resumable=True,
                restart_safe=True,
                format="stub-json",
            )
        },
    )


async def _execute(
    request: RunRequest,
    approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, object]:
    actor = "incident_commander"
    await telemetry.tool_started(actor=actor, tool_name="query_service_health")
    await telemetry.tool_finished(
        actor=actor,
        tool_name="query_service_health",
        succeeded=True,
    )
    await telemetry.model_started(actor=actor, model=request.manifest.model.name)
    await telemetry.model_finished(
        actor=actor,
        model=request.manifest.model.name,
        input_tokens=24,
        output_tokens=12,
    )
    approval = await approval_provider(
        ApprovalRequest(
            request_id="mit-protocol-stub-01",
            case_id="case-protocol-stub",
            proposed_action="rollback checkout-api",
            rationale="hermetic protocol exercise",
            evidence_ids=["ev-protocol-01", "ev-protocol-02"],
            requested_by=actor,
            created_tick=4,
        )
    )
    return {
        "framework": "workflow_protocol_stub",
        "model": request.manifest.model.name,
        "execution": {
            "status": "completed",
            "approval_decision": approval["decision"],
            "action_to_execute": approval["action_to_execute"],
        },
    }


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    raise SystemExit(asyncio.run(run_workflow_protocol(_descriptor(), _execute)))


if __name__ == "__main__":
    main()
