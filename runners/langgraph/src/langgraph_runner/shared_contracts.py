"""LangGraph-facing access to the framework-neutral frozen contract bundle."""

from __future__ import annotations

from agent_lab.contract_bundle import contract_bundle_hashes
from agent_lab.contracts import SCHEMA_VERSION, AgentRole
from agent_lab.prompt_bundle import render_role_prompt


def shared_contract_snapshot() -> dict[str, object]:
    """Return inputs that must remain byte-equivalent to the MAF lane."""

    return {
        "schema_version": SCHEMA_VERSION,
        "hashes": contract_bundle_hashes(),
        "role_prompts": {role.value: render_role_prompt(role) for role in AgentRole},
    }
