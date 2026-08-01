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
no model or MCP tool. The explicit LangGraph substrate advertises restart-safe
native checkpoints in an owner-only JSON envelope; Deep Agents checkpointing
remains unavailable. The later LangGraph workflow phases and Deep Agents
execution remain owned by their lane implementation issues.

The checkpoint compatibility entry point builds a native `interrupt` approval
graph, pauses it, and resumes its file-backed checkpoint in a fresh process:

```bash
uv run --project runners/langgraph --no-sync \
  python -m langgraph_runner.checkpoint_compat prepare \
  --checkpoint-path /tmp/langgraph-checkpoints/checkpoint.json \
  --request-id mitigation-request-1
```

The cross-process test pairs that checkpoint with a separate incident-world
checkpoint and fresh service process; run it with
`uv run pytest tests/test_framework_restart.py`.

The model compatibility entry point exercises the installed native LangChain
OpenAI client without starting a lane:

```bash
uv run --project runners/langgraph --no-sync \
  python -m langgraph_runner.model_compat \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3.5-9B
```

Use the top-level `agent_lab.model_compat_suite` command for paired execution,
wire evidence, and pass/fail comparison.

The first executable `langgraph` slice uses a native `StateGraph` fan-out to
telemetry, change, and reliability nodes, then a synchronized multi-predecessor
edge into a model-backed incident commander and deterministic evidence gate.
The gate rejects unknown citations, fewer than two cited specialist roles, or
a commander judgment that evidence is insufficient. Each agent receives only
its role-authenticated MCP endpoint and the shared model profile. Run the live
slice against the aligned lonestar model through the direct SSH tunnel:

```bash
uv run --project services/incident_mcp --no-sync python -m \
  incident_lab_mcp.langgraph_workflow_compat \
  --base-url http://127.0.0.1:8000/v1 \
  --model unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL
```

The compatibility controller records an explicit edited approval after the
native graph interrupts, then resumes the checkpoint, applies the exact
token-bound action, and verifies recovery. For this seed-42 smoke probe only,
the reviewer edits the model proposal to the known recovery fixture so model
quality cannot prevent the orchestration path from being exercised. Approval
tokens remain in process memory and are excluded from checkpoints and output.
This scripted policy inspects fixture truth, so its runs are benchmark-ineligible
test infrastructure, not a substitute for the control-plane approval command.
The runner itself uses `langgraph_runner.protocol_runner --run`; model/tool
usage, approval, checkpoint, state, and terminal transitions therefore cross
the same normalized lifecycle boundary used by the experiment control plane.
