# Comparison protocol

## Unit of comparison

The primary unit is a paired difference for the same:

```text
(track, scenario generator version, seed, repetition, model profile, budget)
```

The two members differ only by framework lane. Workflow and harness tracks are
analyzed separately.

## Before a run

The control plane must:

1. resolve the source commit and require a clean worktree for a measured run;
2. load and validate the experiment/model profiles;
3. record package lock hashes and framework versions;
4. resolve the exact model revision and server flags;
5. render and hash effective domain prompts and tool catalogs;
6. generate one hidden world manifest, clone it twice, and verify equal hashes;
7. allocate isolated workspaces, checkpoints, traces, and state stores;
8. choose lane order from a recorded randomization seed;
9. preflight the model, MCP catalogs, Phoenix, and GPU telemetry;
10. write each immutable run manifest before starting the framework.

If a held-constant hash differs within a pair, the pair is invalid rather than
quietly included.

Before either lane starts, each isolated runner reports the hashes of the
contract bundle it actually loaded and its framework-neutral normalized
topology. The control plane compares both runners with the repository contract
lock and the canonical matched topology, then snapshots the effective model,
budget, fault profile, source tree, world manifest, role-prompt hash, tool-schema
hash, and completion-schema hash. Any drift aborts the pair before execution.
For the opinionated track this snapshot also includes the shared harness
contract hash covering delegation repair, provenance correction, and phase
limits.
The byte-stable snapshot and its digest are stored as
`native/pair-contract-preflight.json` in both run bundles.

## During a run

- Enforce budgets in a framework-neutral outer watchdog.
- Persist each normalized event before broadcasting it.
- Record native trace correlation IDs when available.
- Count a model call at request dispatch, including failed attempts.
- Count a tool call at MCP request receipt, including invalid calls.
- Do not add hidden retries in the control plane or MCP service.
- Both explicit-workflow adapters make at most one application retry after an
  explicitly retryable structured tool failure; every attempt is metered and
  traced. MAF's native connection-loss reconnect is separately preregistered as
  framework behavior under test; service audit is authoritative for any replay. See
  [explicit-workflow parity](explicit-workflow-parity.md).
- Apply the same scripted approval policy in unattended trials.
- For interactive demos, record the human decision and exclude those runs from
  automated benchmark aggregates unless explicitly labeled.
- Sample GPU/RAM metrics on the same cadence for every lane.
- Never inspect evaluator ground truth until execution has terminated.

## Completion states

Every run ends in exactly one state:

- `completed`;
- `budget_exhausted`;
- `approval_rejected`;
- `approval_timeout`;
- `model_failure`;
- `tool_service_failure`;
- `runner_failure`;
- `cancelled`;
- `invalid_provenance`.

Partial artifacts are scored when meaningful. Operational failure rates remain
visible and are not dropped from denominators.

## Task scoring

The scorer reads:

- the hidden world manifest and final world state;
- the command MCP audit log;
- the final structured incident artifact;
- normalized events needed to verify evidence chronology and post-action
  checks.

The scorer does not read raw prompts or private reasoning. It emits component
scores, hard safety flags, task-success status, and machine-readable reasons.

## System measures

Derived measures use explicit definitions:

- **wall time**: first `run.started` to terminal event;
- **time to first visible progress**: `run.started` to first public agent/tool
  event;
- **time to mitigation**: `run.started` to approved mitigation execution;
- **useful tool call**: result contributes a ground-truth evidence class or
  required post-action check;
- **redundant tool call**: same normalized query and materially equivalent
  result after the fact was already available to the same agent/context;
- **coordination overhead**: model tokens/calls not attributable to specialist
  evidence collection or final synthesis, reported with transparent rules;
- **recovery**: run completes the relevant subgoal after an injected failure
  without operator intervention;
- **trace completeness**: expected semantic operations with correlated native
  spans divided by expected operations.

The compute-matched ceiling uses model calls, tool calls, wall time, and output
tokens. Input, cached-input, and reasoning tokens remain reported outcomes:
they are reconciled when present but are not silently treated as zero or used
as v0.1 dispatch gates. Model-call totals must reconcile across normalized
events, metric events, the metric summary, and `terminal.json`.

Ambiguous derived measures must include an audit sample before being used in a
headline claim.

## Statistical plan

The executable preregistration is
[measured-study-preregistration.md](measured-study-preregistration.md), backed
by `configs/studies/measured-v1.toml`. It freezes binary `task_success` as the
primary outcome, a 10 percentage-point practical difference, seed-cluster
analysis over five repetitions, two co-primary track contrasts with Holm
familywise control, failure/outlier/stopping rules, and a fail-closed pilot
sizing command. The protocol remains explicitly blocked until accepted-model
pilots supply usable variance/runtime and component-discrimination evidence for
both tracks and a reviewer approves the frozen record. The current harness
pilot fails closed on zero paired seed variance.

The pilot estimates variance and failure modes. Before the measured study:

- choose the primary outcome and smallest practically meaningful difference;
- estimate sample size from pilot paired differences;
- run the preregistered canonical/paraphrase-A harness profiles with identical
  paired seeds, repetition counts, budgets, and fault schedules;
- run the preregistered zero/one/two-turn provenance-correction profiles, with
  the effective shared policy hash frozen in each pair;
- run the matched no-review and no-compaction profiles, with active roles and
  native compaction mode frozen into the effective topology;
- freeze exclusions and outlier handling;
- freeze model/server/config/tool/prompt versions;
- preregister which comparisons are confirmatory versus exploratory.

For the measured study:

- report paired raw points and distributions;
- report effect sizes, not only p-values;
- average repeated observations within seed and bootstrap seed clusters for
  continuous/ordinal differences;
- use paired seed-level sign-flip tests for primary task success and label
  run-level McNemar counts descriptive;
- apply Holm correction across the two confirmatory track p-values;
- correct or clearly label multiple exploratory comparisons;
- include all operational failures in reliability reporting;
- publish seeds, manifests, scorer version, and report code in the private repo.

## Model nondeterminism

The configured seed is a requested control, not a guarantee of identical GPU
generation. Repetitions are therefore mandatory. Exact reproducibility applies
to world generation and scorer inputs; statistical reproducibility applies to
model behavior.

## Performance controls on one RTX 4090

- measured runs are sequential;
- warm and cold cohorts are separated;
- lane order is balanced within blocks;
- no other GPU workload is allowed;
- model server is restarted on a recorded schedule, not selectively;
- prefix-cache settings are fixed and reported;
- concurrent subagent requests are allowed only when native to the lane and
  queue time is recorded;
- GPU power/thermal state and peak VRAM are recorded;
- CPU offload settings are held fixed within a model profile.

## Interpretation rules

- A framework failure is still a result.
- A model tool-call parsing failure is classified separately from a framework
  routing failure when wire evidence allows.
- Built-in prompt/harness behavior is part of a harness result.
- More agents or calls are not evidence of better coordination.
- The single-agent baseline is required before claiming multi-agent value.
- Its compute-matched profile uses the same 900-second, 32-model-call,
  64-tool-call, and 32,000-output-token ceilings as each primary pilot lane.
  The unrestricted profile changes only the call/token ceilings to 64, 128,
  and 64,000 respectively; prompts, model, tools, source provenance, fault
  profile, approval policy, and scenario blocks remain fixed.
- A richer native trace UI does not imply better task performance; it is an
  operability result.
- A demo is illustrative, never inferential.
- Retrospective developer-effort comparisons are descriptive unless
  implementation order was balanced or randomized.
