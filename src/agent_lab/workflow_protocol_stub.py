"""Hermetic task stub for the real workflow protocol session."""

from __future__ import annotations

import argparse
import asyncio

from agent_lab.contract_bundle import contract_bundle_hashes
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
        contract_hashes=contract_bundle_hashes(),
        topology={FrameworkLane.MAF_WORKFLOW: {"kind": "hermetic_task_stub"}},
        normalized_topology={FrameworkLane.MAF_WORKFLOW: {"kind": "hermetic_task_stub"}},
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
    specialist = "telemetry_investigator"
    await telemetry.handoff(
        source_actor=actor,
        target_actor=specialist,
        phase="specialist_delegated",
    )
    await telemetry.tool_started(actor=specialist, tool_name="query_service_health")
    await telemetry.tool_finished(
        actor=specialist,
        tool_name="query_service_health",
        succeeded=True,
    )
    await telemetry.model_started(actor=specialist, model=request.manifest.model.name)
    await telemetry.model_finished(
        actor=specialist,
        model=request.manifest.model.name,
        input_tokens=24,
        output_tokens=12,
    )
    await telemetry.handoff(
        source_actor=specialist,
        target_actor=actor,
        phase="specialist_finding_returned",
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


async def _execute_model_timeout(
    request: RunRequest,
    _approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, object]:
    await telemetry.model_started(
        actor="telemetry_investigator",
        model=request.manifest.model.name,
    )
    raise TimeoutError("hermetic model timeout")


async def _execute_tool_failure(
    _request: RunRequest,
    _approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, object]:
    await telemetry.tool_started(
        actor="telemetry_investigator",
        tool_name="query_service_health",
    )
    await telemetry.tool_finished(
        actor="telemetry_investigator",
        tool_name="query_service_health",
        succeeded=False,
    )
    raise ConnectionError("hermetic tool service failure")


async def _execute_parallel_interleave(
    request: RunRequest,
    _approval_provider: ApprovalProvider,
    telemetry: WorkflowTelemetry,
) -> dict[str, object]:
    change_actor = "change_investigator"
    telemetry_actor = "telemetry_investigator"
    await telemetry.model_started(actor=change_actor, model=request.manifest.model.name)
    await telemetry.model_started(actor=telemetry_actor, model=request.manifest.model.name)
    await telemetry.model_finished(
        actor=telemetry_actor,
        model=request.manifest.model.name,
        input_tokens=20,
        output_tokens=7,
    )
    await telemetry.model_finished(
        actor=change_actor,
        model=request.manifest.model.name,
        input_tokens=18,
        output_tokens=5,
    )
    return {
        "framework": "workflow_protocol_stub",
        "model": request.manifest.model.name,
        "execution": {"status": "completed"},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=["success", "model_timeout", "tool_service", "parallel_interleave"],
        default="success",
    )
    args = parser.parse_args()
    executor = {
        "success": _execute,
        "model_timeout": _execute_model_timeout,
        "tool_service": _execute_tool_failure,
        "parallel_interleave": _execute_parallel_interleave,
    }[args.scenario]
    raise SystemExit(asyncio.run(run_workflow_protocol(_descriptor(), executor)))


if __name__ == "__main__":
    main()
