"""Exercise MAF workflow approval checkpoint/resume across process restarts."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Never

from agent_framework import (
    Executor,
    FileCheckpointStorage,
    Workflow,
    WorkflowBuilder,
    WorkflowCheckpoint,
    WorkflowContext,
    handler,
    response_handler,
)

_WORKFLOW_NAME = "agent-lab-approval-restart-compat"


class _OwnerOnlyCheckpointStorage(FileCheckpointStorage):
    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        checkpoint_id = await super().save(checkpoint)
        (self.storage_path / f"{checkpoint_id}.json").chmod(0o600)
        return checkpoint_id


class _ApprovalExecutor(Executor):
    @handler
    async def request_approval(
        self,
        message: dict,
        ctx: WorkflowContext[Never, dict],
    ) -> None:
        await ctx.request_info(message, dict, request_id=str(message["request_id"]))

    @response_handler
    async def apply_decision(
        self,
        original_request: dict,
        response: dict,
        ctx: WorkflowContext[Never, dict],
    ) -> None:
        await ctx.yield_output(
            {
                "phase": "resumed",
                "request_id": original_request["request_id"],
                "decision": response["decision"],
            }
        )


def _storage(path: Path) -> _OwnerOnlyCheckpointStorage:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return _OwnerOnlyCheckpointStorage(path)


def _workflow(storage: _OwnerOnlyCheckpointStorage) -> Workflow:
    approval = _ApprovalExecutor(id="approval")
    return WorkflowBuilder(
        start_executor=approval,
        name=_WORKFLOW_NAME,
        checkpoint_storage=storage,
        output_from=[approval],
    ).build()


async def prepare(checkpoint_path: Path, request_id: str) -> dict[str, object]:
    storage = _storage(checkpoint_path)
    result = await _workflow(storage).run({"phase": "awaiting_approval", "request_id": request_id})
    requests = result.get_request_info_events()
    latest = await storage.get_latest(workflow_name=_WORKFLOW_NAME)
    if len(requests) != 1 or requests[0].request_id != request_id or latest is None:
        raise RuntimeError("MAF approval workflow did not persist one pending request")
    return {
        "framework": "maf_workflow",
        "phase": "awaiting_approval",
        "pid": os.getpid(),
        "request_id": request_id,
        "checkpoint_id": latest.checkpoint_id,
        "checkpoint_files": len(list(checkpoint_path.glob("*.json"))),
    }


async def resume(
    checkpoint_path: Path,
    checkpoint_id: str,
    request_id: str,
    decision: str,
) -> dict[str, object]:
    storage = _storage(checkpoint_path)
    result = await _workflow(storage).run(
        checkpoint_id=checkpoint_id,
        checkpoint_storage=storage,
        responses={request_id: {"decision": decision}},
    )
    outputs = [item for item in result.get_outputs() if isinstance(item, dict)]
    if not outputs or outputs[-1].get("request_id") != request_id:
        raise RuntimeError("MAF approval workflow did not resume the pending request")
    latest = await storage.get_latest(workflow_name=_WORKFLOW_NAME)
    if latest is None:
        raise RuntimeError("MAF approval workflow did not persist its resumed state")
    return {
        "framework": "maf_workflow",
        "phase": "resumed",
        "pid": os.getpid(),
        "request_id": request_id,
        "decision": outputs[-1]["decision"],
        "checkpoint_id": latest.checkpoint_id,
        "resumed_from_checkpoint_id": checkpoint_id,
        "checkpoint_files": len(list(checkpoint_path.glob("*.json"))),
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
        result = asyncio.run(prepare(args.checkpoint_path, args.request_id))
    else:
        result = asyncio.run(
            resume(
                args.checkpoint_path,
                args.checkpoint_id,
                args.request_id,
                args.decision,
            )
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
