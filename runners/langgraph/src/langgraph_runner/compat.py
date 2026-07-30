"""Dependency/API compatibility smoke check; does not call a model."""

from __future__ import annotations

import json
from importlib.metadata import version


def main() -> None:
    from deepagents import create_deep_agent
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_openai import ChatOpenAI
    from langgraph.graph import StateGraph

    required_symbols = (
        create_deep_agent,
        MultiServerMCPClient,
        ChatOpenAI,
        StateGraph,
    )
    assert all(required_symbols)
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
