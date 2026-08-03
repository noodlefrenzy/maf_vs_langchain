#!/usr/bin/env python3
"""Validate both production checkpoint providers across fresh processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_lab.checkpoint_backends import CosmosCheckpointConfig

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], environment: dict[str, str]) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def _validate_lane(
    *,
    project: str,
    module: str,
    framework: str,
    container_suffix: str,
    base_config: CosmosCheckpointConfig,
) -> dict[str, Any]:
    request_id = f"checkpoint-validation-{uuid4().hex}"
    container = f"{base_config.container}-{container_suffix}"
    if len(container) > 255:
        raise ValueError("suffixed Cosmos validation container exceeds 255 characters")
    environment = dict(os.environ)
    environment["AGENT_LAB_COSMOS_CONTAINER"] = container
    command = [
        "uv",
        "run",
        "--project",
        str(REPOSITORY_ROOT / project),
        "--group",
        "checkpoint-production",
        "--no-sync",
        "python",
        "-m",
        module,
    ]
    prepared = _run([*command, "prepare", "--request-id", request_id], environment)
    resumed = _run(
        [
            *command,
            "resume",
            "--checkpoint-id",
            str(prepared["checkpoint_id"]),
            "--request-id",
            request_id,
            "--decision",
            "approve",
        ],
        environment,
    )
    if prepared.get("framework") != framework or resumed.get("framework") != framework:
        raise RuntimeError(f"{framework} production checkpoint identity drifted")
    if prepared.get("phase") != "awaiting_approval" or resumed.get("phase") != "resumed":
        raise RuntimeError(f"{framework} production checkpoint did not resume")
    if resumed.get("decision") != "approve":
        raise RuntimeError(f"{framework} production checkpoint lost the approval decision")
    if resumed.get("resumed_from_checkpoint_id") != prepared.get("checkpoint_id"):
        raise RuntimeError(f"{framework} resumed a different checkpoint")
    if resumed.get("checkpoint_id") == prepared.get("checkpoint_id"):
        raise RuntimeError(f"{framework} did not persist post-resume state")
    if resumed.get("pid") == prepared.get("pid"):
        raise RuntimeError(f"{framework} validation did not cross a process boundary")
    return {
        "framework": framework,
        "backend": resumed["backend"],
        "checkpoint_format": resumed["checkpoint_format"],
        "container": environment["AGENT_LAB_COSMOS_CONTAINER"],
        "prepared_pid": prepared["pid"],
        "resumed_pid": resumed["pid"],
        "resumed": True,
    }


def main() -> None:
    try:
        config = CosmosCheckpointConfig.from_environment()
        lanes = [
            _validate_lane(
                project="runners/maf",
                module="maf_runner.checkpoint_production",
                framework="maf_workflow",
                container_suffix="maf",
                base_config=config,
            ),
            _validate_lane(
                project="runners/langgraph",
                module="langgraph_runner.checkpoint_production",
                framework="langgraph",
                container_suffix="langgraph",
                base_config=config,
            ),
        ]
    except (
        KeyError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"production checkpoint validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        json.dumps(
            {
                "schema_version": "agent-lab-production-checkpoint-validation/v1",
                "backend": "azure_cosmos_db",
                "database": config.database,
                "lanes": lanes,
                "valid": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
