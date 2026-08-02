# Frozen contract bundle v0.1

The first smoke implementation consumes one framework-neutral contract bundle.
Its canonical sources are:

- `agent_lab.prompt_bundle` for domain instructions, role definitions, and
  rendered role prompts;
- `agent_lab.tool_catalog` for MCP names, descriptions, role permissions,
  input/output schemas, and approval metadata;
- `agent_lab.contracts` for evidence, approval, incident-report, audit, event,
  and terminal models;
- `agent_lab.contract_bundle` for canonical serialization and SHA-256 hashes.

The committed hashes are in `contracts/v0.1.0/contract-lock.json`; one
byte-stable hidden-world fixture per incident family is in
`contracts/v0.1.0/scenario-fixtures.json`. Tests fail when a source changes
without updating the appropriate lock. Any semantic change after measured
bundles exist also requires a new schema directory and version; old bundles
and their locks remain readable.

Both framework runner packages expose a small `shared_contract_snapshot`
adapter. The parity test compares their rendered prompts and all bundle hashes
byte-for-byte. Framework-native harness instructions are intentionally not part
of this shared bundle and must be captured separately in each run manifest.

## Role boundaries

All roles can read the public incident brief and status. Only the telemetry
investigator receives health, log, metric, and trace tools. Only the change
investigator receives deploy/config/flag/schema tools. Only the reliability
reviewer receives runbook tools. Only the incident commander can submit a
hypothesis, request approval, apply an authorized mitigation, and verify
recovery.

`request_mitigation` carries both a canonical action ID and a human-readable
description and never mutates the world. The approval token binds the request,
action ID, and description. `apply_mitigation` is the only state-changing v0.1
tool and requires a request identifier plus that valid approval token. Effects
and safety checks use the canonical ID. The catalog supplies the description
shown for approval and execution; the agent's submitted wording remains in the
audit log. The tool service performs no hidden retry; only
`transient_timeout` is retryable under the frozen error taxonomy.
