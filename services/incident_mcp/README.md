# Incident Lab MCP service

The service exposes a seeded hidden world through role-scoped MCP tool
catalogs. Start one process per role with the same seed:

```bash
INCIDENT_LAB_ROLE=telemetry INCIDENT_LAB_SEED=42 \
  uv run --project services/incident_mcp incident-lab-mcp
```

Supported roles in the skeleton are:

- `telemetry`: service health and log evidence
- `change`: recent deployment/configuration evidence
- `runbook`: operational guidance
- `commander`: final hypothesis and mitigation requests

The current transport defaults to stdio. The implementation milestone will
move the stateful benchmark to one Streamable HTTP service with separate
role-scoped credentials, cloned state stores per run, and approval/resume
support. Ground truth must never be mounted into a runner or agent workspace.

The service pins MCP Python SDK 1.x because Microsoft Agent Framework 1.12.1
still declares `mcp>=1.24,<2`. MCP 2.0 compatibility is a separate upgrade
spike.
