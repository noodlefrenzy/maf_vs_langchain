# Incident Lab MCP service

The service exposes a seeded hidden world through role-scoped MCP tool
catalogs. Start one process per role with the same seed:

```bash
INCIDENT_LAB_ROLE=telemetry INCIDENT_LAB_SEED=42 \
  uv run --project services/incident_mcp incident-lab-mcp
```

Supported roles are:

- `telemetry`: health, logs, metrics, and trace evidence
- `change`: deploy, configuration, feature-flag, and schema evidence
- `runbook`: policy and operational guidance
- `commander`: hypothesis, approval-gated mitigation, and recovery checks

The single-role development transport defaults to stdio. Tool calls advance
simulated time and append an audit record; transient failures follow the seed's
replayable schedule and are never retried inside the service. Mitigation
requests do not change state. Applying a mitigation requires the exact action
and token from an audited approval decision.

For the paired-run transport, start the loopback-only Streamable HTTP service:

```bash
uv run --project services/incident_mcp incident-lab-http \
  --seed 42 \
  --pair-id development-pair \
  --ttl-seconds 3600 \
  --credential-file /tmp/incident-lab-credentials.json
```

The service creates independent MAF and LangGraph world clones from the same
manifest, verifies their manifest digests match, and writes one run/lane/role
credential per endpoint. The credential file is created with mode `0600` and
must not already exist. Each HMAC-signed token expires with its run. A token is
accepted only by its exact run and role catalog; all role sessions for that run
share one serialized world and audit log.

The service binds only to `127.0.0.1`, enables DNS-rebinding protection, and
exposes `/healthz` and `/readyz`. Exercise the official SDK plus both native
framework adapters, including authentication, lifecycle, reconnect,
cancellation, structured errors, and revocation, with:

```bash
uv run --project services/incident_mcp \
  python -m incident_lab_mcp.http_compat
```

Validate the simulator, approval/recovery path, deterministic scorer, and run
bundle without a model:

```bash
uv run --project services/incident_mcp incident-lab-oracle \
  --experiment configs/experiments/smoke.toml \
  --seed 42
```

The service pins MCP Python SDK 1.x because Microsoft Agent Framework 1.12.1
still declares `mcp>=1.24,<2`. MCP 2.0 compatibility findings are recorded in
[the separate upgrade note](../../docs/research/mcp-2-upgrade.md). Ground truth
must never be mounted into a runner or agent workspace.
