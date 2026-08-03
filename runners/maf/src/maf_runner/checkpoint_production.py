"""Validate MAF approval restart with the production-intended Cosmos backend."""

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Never

from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    response_handler,
)
from agent_framework_azure_cosmos import CosmosCheckpointStorage
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential

from agent_lab.checkpoint_backends import CosmosCheckpointConfig

_BACKEND_FORMAT = "agent-framework-azure-cosmos-checkpoint/v1"
_WORKFLOW_NAME = "agent-lab-approval-restart-cosmos"


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


def _workflow(storage: CosmosCheckpointStorage) -> Workflow:
    approval = _ApprovalExecutor(id="approval")
    return WorkflowBuilder(
        start_executor=approval,
        name=_WORKFLOW_NAME,
        checkpoint_storage=storage,
        output_from=[approval],
    ).build()


@asynccontextmanager
async def _storage() -> AsyncIterator[CosmosCheckpointStorage]:
    config = CosmosCheckpointConfig.from_environment()
    key = config.credential_value()
    credential = key if key is not None else DefaultAzureCredential()
    client = CosmosClient(
        config.endpoint,
        credential=credential,
        **config.client_options(),
    )
    try:
        yield CosmosCheckpointStorage(
            cosmos_client=client,
            database_name=config.database,
            container_name=config.container,
        )
    finally:
        await client.close()
        if key is None:
            await credential.close()


async def prepare(request_id: str) -> dict[str, object]:
    async with _storage() as storage:
        result = await _workflow(storage).run(
            {"phase": "awaiting_approval", "request_id": request_id}
        )
        requests = result.get_request_info_events()
        latest = await storage.get_latest(workflow_name=_WORKFLOW_NAME)
        if len(requests) != 1 or requests[0].request_id != request_id or latest is None:
            raise RuntimeError("MAF Cosmos workflow did not persist one pending request")
        return {
            "backend": "azure_cosmos_db",
            "checkpoint_format": _BACKEND_FORMAT,
            "framework": "maf_workflow",
            "phase": "awaiting_approval",
            "pid": os.getpid(),
            "request_id": request_id,
            "checkpoint_id": latest.checkpoint_id,
        }


async def resume(checkpoint_id: str, request_id: str, decision: str) -> dict[str, object]:
    async with _storage() as storage:
        result = await _workflow(storage).run(
            checkpoint_id=checkpoint_id,
            checkpoint_storage=storage,
            responses={request_id: {"decision": decision}},
        )
        outputs = [item for item in result.get_outputs() if isinstance(item, dict)]
        if not outputs or outputs[-1].get("request_id") != request_id:
            raise RuntimeError("MAF Cosmos workflow did not resume the pending request")
        latest = await storage.get_latest(workflow_name=_WORKFLOW_NAME)
        if latest is None:
            raise RuntimeError("MAF Cosmos workflow did not persist its resumed state")
        return {
            "backend": "azure_cosmos_db",
            "checkpoint_format": _BACKEND_FORMAT,
            "framework": "maf_workflow",
            "phase": "resumed",
            "pid": os.getpid(),
            "request_id": request_id,
            "decision": outputs[-1]["decision"],
            "checkpoint_id": latest.checkpoint_id,
            "resumed_from_checkpoint_id": checkpoint_id,
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
        result = asyncio.run(prepare(args.request_id))
    else:
        result = asyncio.run(resume(args.checkpoint_id, args.request_id, args.decision))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
