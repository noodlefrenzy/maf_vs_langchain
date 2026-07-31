"""Deterministic hashes for the frozen v0.1 shared contract bundle."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_lab.contracts import SCHEMA_VERSION, ToolErrorCode
from agent_lab.prompt_bundle import prompt_bundle_payload
from agent_lab.tool_catalog import artifact_schema_payload, tool_catalog_payload


def canonical_json(value: Any) -> bytes:
    """Serialize contract data with stable ordering and no insignificant whitespace."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def error_taxonomy_payload() -> dict[str, object]:
    retryable = {ToolErrorCode.TRANSIENT_TIMEOUT.value}
    return {
        "schema_version": SCHEMA_VERSION,
        "errors": [
            {"code": code.value, "retryable": code.value in retryable} for code in ToolErrorCode
        ],
    }


def contract_bundle_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "prompts": prompt_bundle_payload(),
        "tools": tool_catalog_payload(),
        "artifacts": artifact_schema_payload(),
        "errors": error_taxonomy_payload(),
    }


def contract_bundle_hashes() -> dict[str, str]:
    prompts = prompt_bundle_payload()
    tools = tool_catalog_payload()
    artifacts = artifact_schema_payload()
    errors = error_taxonomy_payload()
    return {
        "prompt_bundle_sha256": sha256_payload(prompts),
        "tool_catalog_sha256": sha256_payload(tools),
        "artifact_schemas_sha256": sha256_payload(artifacts),
        "error_taxonomy_sha256": sha256_payload(errors),
        "contract_bundle_sha256": sha256_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "prompts": prompts,
                "tools": tools,
                "artifacts": artifacts,
                "errors": errors,
            }
        ),
    }
