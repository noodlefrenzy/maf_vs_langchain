# Microsoft runner

This process will host two adapters over the same shared contracts:

- `maf_workflow`: an explicit Agent Framework workflow with role-specific
  participants and deterministic termination guards.
- `maf_harness`: an Agent Framework Harness with background agents, planning,
  compaction, and the same role-scoped MCP tools.

The runner owns translation from native workflow/agent events into
`NormalizedEvent`. Native OpenTelemetry spans are exported in parallel; they
are not used as the canonical scorer input.

The executable adapter is intentionally a backlog item. Implement it only
after the prompt bundle and MCP tool schemas are frozen for the first smoke
test.
