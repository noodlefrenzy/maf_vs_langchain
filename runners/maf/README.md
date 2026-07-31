# Microsoft runner

This process will host two adapters over the same shared contracts:

- `maf_workflow`: an explicit Agent Framework workflow with role-specific
  participants and deterministic termination guards.
- `maf_harness`: an Agent Framework Harness with background agents, planning,
  compaction, and the same role-scoped MCP tools.

The runner owns translation from native workflow/agent events into
`NormalizedEvent`. Native OpenTelemetry spans are exported in parallel; they
are not used as the canonical scorer input.

The native process now implements the shared lifecycle boundary:

```bash
uv run --project runners/maf --no-sync \
  python -m maf_runner.protocol_runner --describe
```

`--protocol-probe` accepts the versioned JSON-lines protocol on stdin, reports
the installed MAF versions and planned lane topology, emits canonical
start/finish events, and shuts down cleanly. It deliberately invokes no model
or MCP tool. The explicit Workflow substrate advertises restart-safe
`FileCheckpointStorage`; Harness checkpointing remains unavailable. The actual
Workflow and Harness executions remain owned by their lane implementation issues.

The checkpoint compatibility entry point builds a native approval-request
workflow, pauses it, and resumes its owner-only file checkpoint in a fresh
process:

```bash
uv run --project runners/maf --no-sync \
  python -m maf_runner.checkpoint_compat prepare \
  --checkpoint-path /tmp/maf-checkpoints \
  --request-id mitigation-request-1
```

The cross-process test pairs that checkpoint with a separate incident-world
checkpoint and fresh service process; run it with
`uv run pytest tests/test_framework_restart.py`.

The model compatibility entry point exercises the installed native MAF OpenAI
client without starting a lane:

```bash
uv run --project runners/maf --no-sync python -m maf_runner.model_compat \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3.5-9B
```

Use the top-level `agent_lab.model_compat_suite` command for paired execution,
wire evidence, and pass/fail comparison.

The first executable `maf_workflow` slice uses a native `WorkflowBuilder`
fan-out from one dispatcher to telemetry, change, and reliability agents, then
a synchronized fan-in to a deterministic evidence gate. Each agent receives
only its role-authenticated MCP endpoint and the shared model profile. Run the
live slice against the aligned lonestar model through the direct SSH tunnel:

```bash
uv run --project services/incident_mcp --no-sync python -m \
  incident_lab_mcp.maf_workflow_compat \
  --base-url http://127.0.0.1:8000/v1 \
  --model unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL
```

This phase is read-only: it must pass the evidence gate without creating an
approval request or changing the incident world. Commander synthesis,
approval, mitigation, and recovery are the next workflow nodes.
