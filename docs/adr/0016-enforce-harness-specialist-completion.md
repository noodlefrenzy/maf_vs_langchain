# ADR 0016: Enforce harness specialist completion and bounded result retrieval

Status: Accepted

Date: 2026-08-03

## Context

ADR 0015 disclosed the six-response ceiling and asked specialists to stop
calling tools after four responses. Its clean Qwen3.5-27B gate at source
`448b355` retained valid, inference-eligible bundles but failed both lanes.

Deep Agents delegated to all three roles, but its change specialist ignored the
prompt-only boundary, used response six for more schema calls, and attempted
dispatch seven. MAF's telemetry and change specialists obtained role-specific
evidence, but the commander's native background start/wait/retrieval loop polled
while those tasks ran, consumed all six reserved responses, and attempted
dispatch seven.

The hard limiter and lifecycle barrier worked as designed. The gate showed that
natural-language guidance alone could not make the completion boundary reliable
and that waiting for MAF background work through model calls conflated task
latency with commander reasoning cost.

## Decision

Retain the hard six-dispatch phase ceiling. Make the specialist evidence phase
mechanical in both native adapters: the first four specialist model responses
may expose role-scoped tools, while response five is sent with no tools and
`tool_choice = none`. Response six remains unused compliance margin and the
existing hard tripwire still rejects dispatch seven.

Deep Agents applies a per-role middleware instance that removes all tools from
the fifth model request. MAF configures its native function-invocation loop for
four tool-enabled iterations; the framework's built-in final non-tool iteration
produces response five. Neither adapter fabricates or parses a finding on the
model's behalf.

For MAF delegation, stop model-driven polling after the native commander starts
background tasks. The adapter waits on the retained native task handles, then
runs one separately bounded commander turn instructing it to retrieve completed
results through the framework's native background-result tools. Delegation
repair uses the same drain-then-retrieve sequence. Exceptional exits still
cancel and drain every started task. Deep Agents' synchronous `task` tool
already returns settled results to its commander and needs no extra retrieval
turn.

Freeze the mechanical boundary, the MAF retrieval instruction, and the
settlement/retrieval policy in the shared harness contract. Require a new clean
one-pair gate before another full harness pilot.

## Consequences

- Native specialist scheduling, tool execution, context management, and result
  generation remain framework-owned; only tool availability at the matched
  completion boundary is adapter-enforced.
- MAF task latency no longer spends commander model responses on repeated wait
  polling. Native task results still must be explicitly retrieved by the model,
  and a failed retrieval remains a visible runner failure.
- The hard phase and outer run budgets are unchanged. A model that cannot
  finish without tools on response five still fails closed by response seven.
- Hermetic probes exercise Deep's four-tool-plus-one-final sequence, MAF's
  four-iteration native configuration, the independent six-call tripwire, and
  exceptional task drain.
- The harness-contract SHA-256 becomes
  `7f07791f5a4880988d975fd0126ef427f4822a9519581e890e393427ba7986e8` and
  the aggregate contract-bundle SHA-256 becomes
  `0c24718384899acec49e6227d70306c4638c170b6020e0481b05e0ddda6d1624`.
  Earlier gates and pilots remain retained evidence but cannot be pooled with a
  new cohort.

## Validation outcome

The clean gate ran at source `b70d031`. Characterization was measurement-
eligible with no skipped probes and a 22,140 MiB peak; every compatibility
fixture and actor probe passed. Both bundles and the pair were valid and
inference-eligible, and the lifecycle controls behaved as intended:

- Deep Agents' three specialists all returned within the enforced completion
  boundary, and its workflow reached approval, mitigation, recovery, and a
  completed terminal state. It scored `0.5375` with `task_success = false`:
  the chosen `restore_feature_flag_snapshot` action was applied but was not the
  seeded incident's effective mitigation, so containment and recovery scored
  zero. This is a task-quality result rather than a lifecycle failure.
- MAF's initial delegation returned after two commander responses, its native
  tasks settled, and the separate result-retrieval turn stayed within its fresh
  reservation. The telemetry specialist obtained role-specific evidence, but
  the change specialist used only `get_incident_brief` and
  `get_incident_status`. Those public-context calls return no evidence IDs, so
  the strict same-role provenance validator rejected the proposal after its one
  correction turn.

ADR 0016 therefore closes the bounded-completion and background-settlement
defects, but the clean pair does not qualify the harness operating point. The
MAF failure reveals a narrower shared contract gap: specialist prompts require
role-specific evidence while the specialist catalogs still expose redundant
public-context tools that can consume the entire bounded evidence phase. A
subsequent symmetric contract revision must remove those tools from specialist
turns and pass another clean gate before a full pilot.

| Gate artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `2312e08fc5044e3a6b859b6d50c7facf88ac68f5c58d3a63190ca4de2388e634` |
| `model-compatibility.json` | `c92ebe438a299583c782cf5810af023da6a48312dd37c8a646cf5909ae2c8c9b` |
| resolved execution plan | `28b587f6a856ac2f98bbea66bc0431eec2d269dc358ec3ebd3a1552085806f25` |
| cohort index | `d886774136720a7bd84d4568b649daaa4682b90c39cfa89df929026f7a249474` |
