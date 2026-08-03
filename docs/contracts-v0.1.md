# Frozen contract bundle v0.1

The first smoke implementation consumes one framework-neutral contract bundle.
Its canonical sources are:

- `agent_lab.prompt_bundle` for domain instructions, role definitions, and
  rendered role prompts;
- `agent_lab.tool_catalog` for MCP names, descriptions, role permissions,
  input/output schemas, and approval metadata;
- `agent_lab.contracts` for evidence, approval, incident-report, audit, event,
  and terminal models;
- `agent_lab.workflow_contracts` for explicit-workflow schemas, observation
  and bounded retry policy, dynamic instructions, evidence gating, result
  parsing, and accounting;
- `agent_lab.harness_contracts` for opinionated-harness delegation, provenance,
  correction, phase-limit, prompt, active-role, native-compaction, and
  completion-schema policy;
- `agent_lab.baseline_contracts` for the one-agent union catalog, source-scoped
  provenance, discovery repair, two-phase synthesis, correction, evidence gate,
  prompt, and completion-schema policy;
- `agent_lab.contract_bundle` for canonical serialization and SHA-256 hashes.

The committed hashes are in `contracts/v0.1.0/contract-lock.json`; one
byte-stable hidden-world fixture per incident family is in
`contracts/v0.1.0/scenario-fixtures.json`. Tests fail when a source changes
without updating the appropriate lock. Any semantic change after measured
bundles exist also requires a new schema directory and version; old bundles
and their locks remain readable.

Both framework runner packages expose a small `shared_contract_snapshot`
adapter. The parity test compares their rendered prompts and all bundle hashes
byte-for-byte. Opinionated-harness instructions and parity-critical control
policy are part of the shared bundle. The single-agent adapters likewise share
one frozen baseline contract: MAF `Agent` and LangChain `create_agent` each run
one model-driven incident commander with the same discovery-tool union and no
delegation. An unstructured first phase must make real MCP calls through at
least two capability sources; a tool-disabled structured phase receives the
observed source-specific evidence allowlist. Both adapters enforce the same
eight-dispatch per-phase model-call ceiling beneath the framework-neutral outer
run budget. Framework-native implementation details remain captured in each
run's execution provenance.

## Role boundaries

All roles can read the public incident brief and status. Only the telemetry
investigator receives health, log, metric, and trace tools. Only the change
investigator receives deploy/config/flag/schema tools. Only the reliability
reviewer receives runbook tools. Only the incident commander can submit a
hypothesis, request approval, apply an authorized mitigation, and verify
recovery.

For the single-agent baseline, the public incident tools are assigned once to
the telemetry endpoint and every specialist-only discovery tool remains on its
native capability endpoint. The one agent sees their duplicate-free union.
Finding `role` fields label the endpoint that supplied the evidence; they do
not imply a specialist agent existed. Command tools remain behind the shared
outer approval, mitigation, and recovery stages.

`request_mitigation` carries both a canonical action ID and a human-readable
description and never mutates the world. The approval token binds the request,
action ID, and description. `apply_mitigation` is the only state-changing v0.1
tool and requires a request identifier plus that valid approval token. Effects
and safety checks use the canonical ID. The catalog supplies the description
shown for approval and execution; the agent's submitted wording remains in the
audit log. The tool service performs no hidden retry; only
`transient_timeout` is retryable under the frozen error taxonomy. Both explicit
workflow adapters make at most one visible, metered application retry for that
normalized error.
