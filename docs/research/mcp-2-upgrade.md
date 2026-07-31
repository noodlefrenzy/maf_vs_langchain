# MCP 2.0 upgrade boundary

This note records why MCP 2.0 is outside the v0.1 compatibility baseline and
what must be proven in a separate upgrade experiment.

## Current baseline

- The incident service, MAF runner, and LangGraph runner resolve MCP Python SDK
  `1.29.0`.
- All three project manifests constrain MCP to `>=1.24,<2`.
- Installed `agent-framework-core` `1.12.1` metadata declares
  `mcp>=1.24.0,<2` for its `all` extra.
- Installed `langchain-mcp-adapters` `0.3.1` metadata declares `mcp>=1.24.0`
  without an upper bound.
- Authenticated Streamable HTTP compatibility is exercised against both native
  clients without crossing their isolated project environments.

The binding constraint is therefore the MAF environment, not the incident
service protocol design. Upgrading only the service would create a mixed-major
matrix that is not part of the frozen v0.1 experiment.

## Separate experiment

An MCP 2.0 spike must use isolated lockfiles and must not alter measured v0.1
runs. It should:

1. select versions only after the repository's 72-hour release quarantine;
2. verify MAF's package constraint has become compatible or document the exact
   incompatibility;
3. rerun role-catalog schema checks and authenticated HTTP checks for both
   framework clients;
4. cover initialization, lifecycle shutdown, reconnect, cancellation,
   structured tool errors, token expiry, and revocation;
5. compare protocol negotiation and serialized tool schemas with the frozen
   MCP 1.x baseline.

No MCP 2.0 dependency is introduced by the v0.1 implementation.
