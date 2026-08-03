# ADR 0015: Make harness bounds and provenance actionable in prompts

Status: Accepted

Date: 2026-08-03

## Context

The clean replacement matched-harness pilot at source `1020b6f` closed the
native task-lifecycle defects from ADR 0014: all 30 bundles and 15 pairs were
valid and inference-eligible. It nevertheless produced zero task successes in
both lanes.

Every Deep Agents run reached the six-call specialist ceiling. The specialist
prompt required eventual completion but did not disclose the hard phase bound;
the model continued issuing repetitive evidence calls on its sixth response
and dispatch seven failed before a final finding could be returned.

Eleven MAF proposals failed strict tool or evidence provenance after the one
allowed correction. The validator checked both fields, but the synthesis and
correction prompts rendered only evidence IDs, not the exact role-specific
tool allowlist. Several findings consequently cited background-agent control
tools, placeholders, blanks, or otherwise unobserved names. Other findings used
only public incident calls and had no role-specific evidence ID to cite.

These are shared adapter-contract gaps, not framework-native differences. The
preregistered sizing command correctly rejected the resulting zero-variance
pilot.

## Decision

Keep the hard six-dispatch phase ceiling and strict fail-closed validator.
Expose the bound symmetrically in both canonical and paraphrase coordinator,
delegation-repair, and specialist instructions. Coordinators must delegate
before optional planning/workspace work. Specialists must call at least one
role-specific evidence tool, use no more than four tool-calling responses, and
reserve a final response for their natural-JSON finding even when uncertainty
remains.

Replace the synthesis-only evidence list with one deterministic role-specific
provenance allowlist containing both exact `tools_used` names and exact
`evidence_ids`. Synthesis and the matched correction turn explicitly reject
blank, placeholder, planning, workspace, task-management, and background-agent
names. Validation remains authoritative; prompt text does not normalize or
silently repair a proposal.

Add a one-seed Qwen3.5-27B harness-specific gate using the pilot fault profile,
budgets, ablation controls, tracing, and resource policy. Require that gate at
clean source before another five-seed replacement pilot.

## Consequences

- Both adapters still use their native scheduling, planning, workspace, and
  subagent mechanisms; only the shared completion/provenance boundary changes.
- The hard phase and outer run budgets are unchanged. The four-response tool
  instruction leaves one response of compliance margin before the sixth-call
  hard stop.
- The harness-contract and aggregate contract-bundle hashes change. The valid
  zero-success pilot remains retained evidence but cannot be pooled with a
  replacement cohort.
- Canonical and paraphrase variants carry the same operational constraints, so
  the planned prompt-sensitivity ablation remains matched.
- A clean one-pair gate can still fail on genuine model/framework behavior. A
  failed gate blocks another full pilot rather than prompting post hoc outcome
  repair.

## Validation outcome

The clean gate ran at source `448b355`. Characterization was measurement-
eligible with no skipped probes and a 22,133 MiB peak; every compatibility
fixture and actor probe passed. Both task bundles and the matched pair were
valid and inference-eligible, but neither lane reached synthesis:

- Deep Agents delegated to all three specialists. The reliability and
  telemetry specialists finished after four and five model responses, but the
  change specialist ignored the prompt-only four-response tool boundary, used
  its sixth response for more schema calls, and failed before dispatch seven.
- MAF delegated to telemetry and change specialists, both of which obtained
  role-specific evidence. Its background-task start/wait/retrieval loop used
  all six commander responses and attempted dispatch seven, which the aggregate
  phase reservation rejected.

The prompt and provenance revision is therefore useful diagnostic evidence but
not a qualified harness operating point. It demonstrates that prompt-only
completion guidance cannot enforce the frozen boundary and that MAF's native
background polling consumes the same reservation independently of specialist
completion. The failed gate blocks another full pilot.

ADR 0016 supersedes the prompt-only completion portion of this decision. It
retains the prompts, hard limit, and exact provenance allowlist while
mechanically disabling specialist tools on response five and separating MAF
task drain from bounded native result retrieval.

| Gate artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `40a20abb7c3606949e396cdad1369e033d1431c1206d31e67ca7ee30e810a717` |
| `model-compatibility.json` | `5b2d0b256600fe240cc42329e8ce9e2b6941512306472f9cebf81ef50e5c6e43` |
| resolved execution plan | `1a7a809aa3d7a07f6a64b5c5a50113c2fc30cf9644bffd47851b266436a2b2eb` |
| cohort index | `bfe95a021b40b46e4fc5ad6d919c48aaaf0948a81ddb4fee9afc3f11d53bac5b` |
