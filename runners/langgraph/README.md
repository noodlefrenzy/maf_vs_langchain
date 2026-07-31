# LangGraph runner

This process will host two adapters over the same shared contracts:

- `langgraph`: a custom graph with explicit role nodes, conditional routing,
  checkpointing, and deterministic termination guards.
- `deep_agents`: an opinionated Deep Agents harness with the same coordinator,
  specialist roles, MCP tools, budgets, and artifact contract.

The runner converts LangGraph/Deep Agents streaming events to
`NormalizedEvent` and also emits native LangChain/OpenInference telemetry.

The native process now implements the shared lifecycle boundary:

```bash
uv run --project runners/langgraph --no-sync \
  python -m langgraph_runner.protocol_runner --describe
```

`--protocol-probe` accepts the versioned JSON-lines protocol on stdin, reports
the installed LangGraph/Deep Agents versions and planned lane topology, emits
canonical start/finish events, and shuts down cleanly. It deliberately invokes
no model or MCP tool and advertises checkpoint support as unavailable. The
actual LangGraph and Deep Agents executions remain owned by their lane
implementation issues.
