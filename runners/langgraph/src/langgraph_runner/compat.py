"""Dependency/API compatibility smoke check; does not call a model."""

from __future__ import annotations

import json
from importlib.metadata import version


def main() -> None:
    from deepagents import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
        create_deep_agent,
        register_harness_profile,
    )
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_openai import ChatOpenAI
    from langgraph.graph import StateGraph

    required_symbols = (
        create_deep_agent,
        MultiServerMCPClient,
        ChatOpenAI,
        StateGraph,
        HarnessProfile,
    )
    assert all(required_symbols)
    no_compaction = HarnessProfile(
        excluded_tools=frozenset({"execute"}),
        excluded_middleware=frozenset({"SummarizationMiddleware"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile("openai", no_compaction)
    model = ChatOpenAI(
        model="compat-no-network",
        api_key="compat-placeholder",
        base_url="http://127.0.0.1:9/v1",
        use_responses_api=False,
    )
    assert create_deep_agent(model=model)
    print(
        json.dumps(
            {
                "status": "ok",
                "deepagents": version("deepagents"),
                "langchain": version("langchain"),
                "langchain-mcp-adapters": version("langchain-mcp-adapters"),
                "langchain-openai": version("langchain-openai"),
                "langgraph": version("langgraph"),
                "mcp": version("mcp"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
