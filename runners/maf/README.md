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
