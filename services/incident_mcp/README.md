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

The current transport defaults to stdio. Tool calls advance simulated time and
append an audit record; transient failures follow the seed's replayable
schedule and are never retried inside the service. Mitigation requests do not
change state. Applying a mitigation requires the exact action and token from an
audited approval decision.

Validate the simulator, approval/recovery path, deterministic scorer, and run
bundle without a model:

```bash
uv run --project services/incident_mcp incident-lab-oracle \
  --experiment configs/experiments/smoke.toml \
  --seed 42
```

Run-scoped Streamable HTTP, authenticated role credentials, and persistent
world-clone lifecycle remain the next transport milestone. Ground truth must
never be mounted into a runner or agent workspace.

The service pins MCP Python SDK 1.x because Microsoft Agent Framework 1.12.1
still declares `mcp>=1.24,<2`. MCP 2.0 compatibility is a separate upgrade
spike.
