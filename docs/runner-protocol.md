# Runner protocol v0.1

The control plane and every framework runner communicate over a versioned
JSON-lines protocol on stdin/stdout. Framework-native objects, credentials in
environment variables, and hidden world state never cross this boundary.

## Lifecycle

The control plane sends:

1. `prepare`, containing the immutable run manifest, public ticket,
   role-scoped MCP endpoints, and runner-visible paths;
2. `start`, after validating the runner's prepared provenance;
3. zero or more `approval` decisions;
4. `cancel` when an outer limit or operator cancellation fires;
5. `shutdown` for graceful process teardown.

The runner emits:

- `prepared`, with topology, effective-prompt, tool-catalog, framework-version,
  and checkpoint-capability metadata;
- cumulative `usage` before the corresponding model, tool, output-token, or
  approval dispatch;
- ordered normalized `event` envelopes;
- optional `heartbeat` phase updates;
- exactly one `terminal` outcome, or an `error`.

Every object rejects unknown fields and carries protocol version `0.1.0`.
Messages with the wrong run ID, lane, provenance hashes, or event sequence fail
closed.

## Isolation and secrets

Each run is created exclusively under its run UUID:

```text
<runs>/<run-id>/
  manifest.json
  events.jsonl
  terminal.json
  world/                 service/control-plane only
  workspace/             runner-visible files
  checkpoints/           framework-native checkpoints
  state/                 runner-owned durable state
  native/
    prepared-run.json
    run-request.redacted.json
    runner.stderr.log
    traces/
  metrics/
```

The typed request contains only the runner's role endpoints. Bearer tokens are
sent through stdin and are redacted from the persisted request view. The world
directory is neither included in the runner protocol nor exported in its
environment.

## Paired protocol preflight

Run the checked-in smoke profile through all native process boundaries with:

```bash
uv run agent-lab experiment probe \
  --experiment configs/experiments/smoke.toml \
  --runs-dir runs \
  --order-seed 1729
```

For each seed/repetition block, the command creates equal isolated world
clones for both comparison tracks, starts the authenticated loopback service,
resolves native runner descriptors and lock hashes, records deterministic lane
order, and launches lanes sequentially. It saves an immutable summary index
plus one terminal bundle and owner-only world checkpoint per lane.

The execution mode is always labeled `protocol_probe`. It invokes no model or
MCP tool, produces no score, and is infrastructure evidence only. Actual task
runs remain gated on the lane implementations and their checkpoint metadata.

## Outer limits and terminal states

The control plane checks cumulative counters monotonically and cancels on the
first model-call, tool-call, output-token, or approval-request overage. The
wall-clock deadline is always external to the framework. A deadline while the
runner reports `awaiting_approval` becomes `approval_timeout`; other wall-clock
overruns become `budget_exhausted`.

Launch failure, malformed protocol output, identity drift, invalid event order,
and a process exit without a terminal message become a persisted
`runner_failure` or `invalid_provenance` terminal. Cancellation and shutdown
first use protocol commands, then process termination, and finally a kill only
after the configured grace period.

The mock-runner matrix covers success, startup/launch failure, crash, wall
timeout, approval timeout, explicit cancellation, graceful shutdown, and all
four usage budgets. Native MAF and LangGraph processes also implement the
boundary in an explicit `protocol_probe` mode. They report installed framework
versions and planned topology, emit canonical run lifecycle events, and prove
process compatibility without calling a model or MCP tool. Actual lane
execution plugs into the same handlers in the lane implementation issues.
