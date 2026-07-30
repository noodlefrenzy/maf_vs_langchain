"""Dependency/API compatibility smoke check; does not call a model."""

from __future__ import annotations

import json
from importlib.metadata import version


def main() -> None:
    from agent_framework import Agent, MCPStdioTool, create_harness_agent
    from agent_framework.openai import OpenAIChatCompletionClient
    from agent_framework.orchestrations import HandoffBuilder

    required_symbols = (
        Agent,
        MCPStdioTool,
        create_harness_agent,
        OpenAIChatCompletionClient,
        HandoffBuilder,
    )
    assert all(required_symbols)
    print(
        json.dumps(
            {
                "status": "ok",
                "agent-framework-core": version("agent-framework-core"),
                "agent-framework-openai": version("agent-framework-openai"),
                "agent-framework-orchestrations": version("agent-framework-orchestrations"),
                "agent-framework-tools": version("agent-framework-tools"),
                "mcp": version("mcp"),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
