# ADR 0017: Limit harness specialists to role-specific evidence tools

Status: Accepted

Date: 2026-08-03

## Context

ADR 0016's clean Qwen3.5-27B gate at source `b70d031` verified the mechanical
specialist-completion boundary and bounded MAF background-result retrieval.
Deep Agents completed the full workflow, while MAF's native tasks settled and
were retrieved within their reservations.

MAF nevertheless failed strict provenance. Its change specialist used only
`get_incident_brief` and `get_incident_status`, exhausted its bounded evidence
phase without calling a change-specific tool, and consequently observed no
evidence ID. The coordinator already receives the same public ticket, and the
shared specialist prompt says those calls cannot satisfy the role-evidence
requirement. Keeping them in a four-response specialist catalog creates a
known dead end that is unrelated to either framework's scheduling behavior.

## Decision

For opinionated-harness specialist phases only, expose the role-specific
evidence tools and exclude `get_incident_brief` and `get_incident_status` in
both adapters. Keep the full role catalog unchanged for the explicit workflow,
single-agent baseline, compatibility probes, MCP authorization boundary, and
incident commander.

Freeze the excluded public tools and each resulting specialist catalog in
`agent_lab.harness_contracts`. Define native specialist activity as at least one
role-specific evidence call. Deep Agents still validates the complete MCP
endpoint catalog before filtering what it passes to each `SubAgent`; MAF passes
the same shared subset through its native MCP client's `allowed_tools` option.
Neither adapter chooses a tool, manufactures evidence, or relaxes the
same-role provenance validator.

Require a new clean one-pair Qwen3.5-27B gate before another full harness
pilot.

## Consequences

- Scarce specialist responses cannot be spent re-reading public context that
  is already present in the delegated task and cannot produce an evidence ID.
- Native tool choice among the role's diagnostic, change, or runbook tools
  remains model/framework behavior under test.
- General role permissions and all non-harness tracks are unchanged.
- The specialist prompts and mechanical exposure now express the same
  evidence requirement for both canonical and paraphrase variants.
- The harness-contract SHA-256 becomes
  `ba584a68e790474673b275de407436725f945ff3381d8b41669395c364ecc6b0` and
  the aggregate contract-bundle SHA-256 becomes
  `db8a188847436f28df1fb128c031aa5c956523e7d144bdb71c4aa5d9b15ef524`.
  Earlier gates remain retained diagnostic evidence and cannot be pooled with
  the next cohort.
