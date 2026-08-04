# ADR 0010: Freeze a symmetric two-phase harness adapter contract

Status: Accepted

Date: 2026-08-03

## Context

Native structured-output mode caused both opinionated harnesses to bypass or
short-circuit delegation. Stabilizing one lane at a time then introduced
adapter-policy asymmetries in output limits, provenance validation, correction
turns, under-delegation handling, and model-call limits. Those differences are
not framework opinions and would confound a head-to-head harness result.

## Decision

Use two phases in both lanes: native coordinator/specialist delegation first,
then tool-disabled structured synthesis. Freeze the parity-critical policy in
`agent_lab.harness_contracts` and include its hash in the v0.1 contract bundle:

- at least two specialists must prove activity through native MCP calls;
- under-delegation receives two bounded repair turns, then fails closed;
- claimed tools and evidence IDs must be subsets of observations for the same
  specialist;
- provenance failure receives exactly one matched correction turn;
- delegation and synthesis use the same six-call semantic cap and error exit;
- streaming cannot bypass dispatch-time accounting.

The versioned harness ablation payload also owns the paired controls that must
change both adapters together: canonical/paraphrase-A prompts, zero/one/two
provenance-correction turns, three-role versus no-review topology, and native
versus disabled context compaction. The no-review condition exposes only the
telemetry and change specialists and requires both to prove real MCP activity.
The no-compaction condition uses MAF's public `disable_compaction` option and
Deep Agents' public `HarnessProfile.excluded_middleware` alias for
`SummarizationMiddleware`. The pinned API compatibility probes assert those
hooks remain available.

Framework-native planning, workspace, scheduling, and context behavior remain
the opinionated behavior under test.

## Consequences

- Adapter guards can no longer silently favor one harness lane.
- A parity-policy change invalidates the shared contract hash and pair
  preflight.
- An ablation changes the effective normalized topology and prompt-policy hash,
  so a mislabeled or one-sided run fails provenance checks.
- The two phases add coordinator turns and must be reported as harness cost.
- Previous harness smoke cohorts remain engineering evidence, not comparable
  post-parity measurements.

ADR 0015 retains these hard limits and validators while making the limit
actionable in shared coordinator/specialist prompts and expanding the rendered
correction allowlist to cover both exact tools and evidence IDs.
