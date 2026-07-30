# LangGraph runner

This process will host two adapters over the same shared contracts:

- `langgraph`: a custom graph with explicit role nodes, conditional routing,
  checkpointing, and deterministic termination guards.
- `deep_agents`: an opinionated Deep Agents harness with the same coordinator,
  specialist roles, MCP tools, budgets, and artifact contract.

The runner converts LangGraph/Deep Agents streaming events to
`NormalizedEvent` and also emits native LangChain/OpenInference telemetry.

The executable adapter is intentionally a backlog item. Implement it only
after the prompt bundle and MCP tool schemas are frozen for the first smoke
test.
