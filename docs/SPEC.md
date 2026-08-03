# Experiment specification

Status: Approved for implementation

Version: 0.1

Research cutoff: 2026-07-31

## 1. Purpose

Build a reproducible field lab that answers three bounded questions:

1. How does Microsoft Agent Framework Workflows compare with a custom
   LangGraph when both implement the same explicit multi-agent graph?
2. How does Microsoft Agent Framework Harness compare with Deep Agents when
   both receive the same long-horizon task, specialist roles, tools, model,
   context budget, and stopping rules?
3. How do the multi-agent tracks compare with matched one-agent baselines, and
   do any outcome gains survive equal compute ceilings?

The result is a decision record about engineering tradeoffs on this machine
and this workload. It is not a universal framework leaderboard.

## 2. Success criteria

The lab is complete when a reviewer can:

- launch one OpenAI-compatible local reasoning model and use it from all lanes;
- launch isolated copies of a seeded incident world behind MCP;
- run a paired experiment from one command;
- watch both executions side by side without exposing private chain of thought;
- pause, approve or reject a proposed mitigation, and resume a run;
- inspect native traces and a framework-neutral event timeline;
- reproduce every scenario from its seed and every software environment from
  lock files and container/image digests;
- generate an offline HTML report with paired task, quality, latency,
  efficiency, reliability, and observability comparisons;
- distinguish framework behavior from model, task-order, warm-cache, and
  evaluator effects.

## 3. Non-goals

- Declaring one ecosystem categorically better.
- Comparing cloud hosting, Azure Foundry, or LangSmith deployment in the first
  study.
- Letting either framework use provider-hosted tools unavailable to the other.
- Benchmarking model quality independently of the agent runtimes.
- Persisting or displaying raw hidden reasoning. Decisions, tool calls,
  evidence, state changes, and concise agent status are sufficient.
- Treating a single demo run as evidence.

## 4. Comparison matrix

| Track | Microsoft implementation | LangChain implementation | What is held equivalent |
| --- | --- | --- | --- |
| Explicit workflow | Agent Framework Workflows | Custom LangGraph | Nodes/roles, fan-out/fan-in, review loop, checkpoints, approval, budgets |
| Opinionated harness | Agent Framework Harness | Deep Agents | Coordinator prompt, specialist definitions, MCP catalogs, workspace, planning and completion contract |
| Baseline | Single Agent Framework agent | LangChain `create_agent` | One agent, union tool catalog, compute-matched budget |
| Oracle | Scripted state machine | Same executable | Validates simulator and deterministic scorer, not framework quality |

Scores are reported within a track. Cross-track numbers may be shown as
engineering context, but never collapsed into “MAF” and “LangChain” averages.

## 5. Primary task: seeded incident response

### 5.1 User-visible objective

A SEV-1 has reduced checkout success in two regions. The team must identify the
initiating fault, support it with independent evidence, choose the
least-destructive mitigation, obtain approval, execute it, verify recovery, and
produce a concise incident record.

The initial ticket deliberately does not contain enough information to solve
the task. Evidence exists only behind MCP tools.

### 5.2 Roles

| Role | Responsibility | MCP capability |
| --- | --- | --- |
| Incident commander | Decompose work, integrate evidence, request review and approval, own final answer | Public ticket, hypothesis submission, mitigation request |
| Telemetry investigator | Establish timing and causality from health, metrics, logs, and traces | Read-only telemetry |
| Change investigator | Correlate deploys, config, feature flags, and schemas with symptom onset | Read-only change history and diffs |
| Reliability reviewer | Find policy/runbook constraints, challenge causal claims, test whether mitigation is reversible | Read-only runbooks and review resources |

In the two multi-agent tracks, no agent receives all evidence tools. Capability
separation is enforced by MCP catalog/credentials, not only by prompts. The
commander must obtain and combine findings from other roles. The explicit
single-agent baseline is the exception: one commander receives a duplicate-free
union routed through the same capability-scoped credentials, and source roles
label provenance rather than separate agents.

### 5.3 Explicit workflow topology

```text
ticket
  |
  +--> telemetry investigator --+
  +--> change investigator ------+--> commander synthesis
  +--> reliability reviewer -----+          |
                                             v
                                      evidence gate
                                        /       \
                                 insufficient   sufficient
                                      |             |
                                      +-- retry <---+--> approval
                                                        |
                                                   mitigation
                                                        |
                                                recovery check
                                                        |
                                                   final report
```

The evidence gate and hard limits are deterministic code. Agents decide which
tools to call, what queries to make, what findings matter, how to challenge one
another, and whether more investigation is needed.

### 5.4 Harness topology

The harness track receives the same four role definitions and capability
boundaries, but the coordinator chooses when and whether to delegate. Planning,
context compaction, artifact use, and subagent scheduling are supplied by the
respective harness. A deterministic outer guard still enforces the shared
budget and completion schema.

### 5.5 Incident families

The initial generator supports:

- retry amplification that makes a downstream service look causal
  (`retry_storm`);
- client connection leakage with a healthy database
  (`connection_pool_exhaustion`);
- regionally inconsistent feature flags (`feature_flag_mismatch`);
- producer/consumer schema drift with a healthy broker (`schema_drift`);
- a cache TTL regression that makes the origin look under-provisioned
  (`cache_ttl_regression`).

Each seed selects surface details, a decoy, evidence placement, and a
replayable transient-failure cadence. Later generators may add delayed metrics,
out-of-order events, stale runbooks, and mitigation side effects.

### 5.6 Controlled nondeterminism

The lab distinguishes:

- **world variation**: seeded and exactly replayable;
- **tool faults**: seeded and replayable per isolated world;
- **model sampling**: configured with the same requested seed and parameters,
  but treated as statistically rather than bitwise reproducible;
- **runtime scheduling**: recorded, not assumed deterministic;
- **inference runtime**: model artifact/revision, tokenizer, chat template,
  server/parser/serve flags, and serving-host compute driver/runtime are hashed
  into paired preflight evidence;
- **live external data**: excluded from the primary benchmark.

Each paired lane receives a fresh world clone with the same manifest. A lane
can never mutate the other lane's state.

## 6. MCP contract

The benchmark owns its MCP service and versioned schemas. Tool names,
descriptions, argument schemas, result schemas, error taxonomy, and approval
semantics are frozen before a measured run.

Required capability groups:

- `incident`: public ticket and current incident status;
- `telemetry`: bounded service health, log search, metric query, trace lookup;
- `changes`: change list, deploy detail, config/flag/schema diff;
- `runbooks`: policy and operational guidance search;
- `command`: hypothesis submission and approval-gated mitigation;
- `evaluation`: never exposed to agents.

Every call records start/end times, normalized arguments after redaction,
result digest, result size, error category, and simulated-world tick. Framework
retries are visible; the MCP service does not silently retry.

For the v0.1 explicit workflow, structured tool failures receive no
application-level retry and both native client error surfaces normalize to the
same failure. MAF's connection-loss reconnect remains a preregistered native
runtime behavior; server audit and the outer budget count any replay that
reaches the service.

The simulator preserves four state invariants across live execution and
checkpoint restart:

- every `evidence_id` contained anywhere in a successfully validated tool
  result is citable, while IDs from failed or unreturned results are not;
- a successful post-mitigation recovery verification is monotonic scoring
  state, even if a later defensive check includes an unhealthy decoy; and
- invalid control-plane approval decisions and timeouts are audited without
  advancing world time, so malformed control traffic cannot desynchronize a
  paired world's subsequent evidence stream; and
- mitigation effects and safety outcomes are selected by a versioned canonical
  action ID from the runbook catalog. Human-readable action descriptions remain
  in requests, approval records, audit events, and reports. The catalog supplies
  the authorized display description, while the agent's original wording remains
  in the request audit event; prose equality does not determine simulator state
  or scorer credit.

The first compatible baseline pins MCP Python SDK 1.x. MCP 2.0 adoption is a
separate experiment because current MAF package metadata still requires
`mcp<2`.

## 7. Local model

Both stacks use the same direct OpenAI-compatible Chat Completions endpoint.
There is no per-framework model router.

Initial fit-check profile:

- model: `Qwen/Qwen3.5-9B`;
- server: recent/nightly vLLM required by the model card;
- text-only serving;
- 32,768-token advertised context for the first fit test;
- thinking enabled;
- explicit temperature, top-p, top-k, max output, timeout, and requested seed;
- min-p, presence penalty, repetition penalty, and thinking mode are also
  mapped explicitly at the effective OpenAI request boundary;
- vLLM reasoning parser `qwen3`;
- vLLM tool parser `qwen3_coder`.

The 9B model was a starting hypothesis, not a frozen choice. It passed native
compatibility but failed the paired task-quality gate.
`Qwen3.5-35B-A3B-GPTQ-Int4` was then tested as a quality-tier candidate using
bounded CPU offload into the 128 GB system RAM. It loaded but failed engine
initialization because the pinned Triton kernels could not consume offloaded
tensors, while no-offload loading cannot preserve useful KV-cache headroom on
the 24 GB RTX 4090. Model selection remains gated on a different measured
candidate or serving topology, not parameter count.

The run manifest records the model repository revision, weight digest, server
version and flags, CUDA/driver versions, quantization, context limit, sampling
settings, and warm/cold state.

## 8. Fairness controls

### 8.1 Held constant

- world manifest and seed;
- user brief and role-level domain instructions;
- tool schemas, descriptions, permissions, and fault schedule;
- model endpoint, weights, parser, sampling, timeouts, and output limits;
- wall-clock, model-call, tool-call, output-token, and approval budgets;
- structured final-answer schema and deterministic scorer;
- host, GPU, and observability sinks;
- approval policy and scripted human decision in unattended trials.

### 8.2 Allowed to differ

The following are the subjects of comparison and must remain native:

- state representation and propagation;
- framework-added harness prompts;
- scheduling, routing, and subagent context isolation;
- checkpoint representation;
- streaming/event APIs;
- middleware/callback ergonomics;
- framework-native telemetry;
- recovery mechanics.

Framework-added prompt text is captured and hashed where technically possible.
It is not “normalized away,” because it is part of an opinionated harness.

### 8.3 Ordering and replication

- Smoke: one seed, one repetition, no injected fault.
- Pilot: five equally weighted incident-family seeds, three repetitions, and
  the standard fault profile. The exact generator-derived selection and its
  pre-execution planner are frozen in [pilot-case-plan.md](pilot-case-plan.md).
- Measured study: at least 20 seeds and five repetitions after power/variance
  review.
- Execute on one GPU sequentially.
- Randomize lane order within each seed/repetition block.
- Balance A/B and B/A order to reduce warm-cache and thermal bias.
- Record cold starts separately from warm runs.
- Never reuse a model conversation, checkpoint, MCP state, or workspace across
  lanes.

Binary paired outcomes use McNemar-style analysis where appropriate.
Continuous paired differences report medians, distributions, and paired
bootstrap confidence intervals. A pilot is for variance and failure discovery,
not a winner announcement.

## 9. Scoring

### 9.1 Deterministic task score

| Component | Weight | Evidence |
| --- | ---: | --- |
| Correct and effective containment | 0.30 | World state improves after the approved action |
| Root-cause diagnosis | 0.25 | Structured answer matches causal ground truth |
| Independent evidence | 0.15 | Required evidence classes and chronology are cited |
| Policy and safety | 0.15 | Approval obtained; destructive decoys avoided |
| Recovery verification | 0.10 | Required health checks performed after mitigation |
| Incident artifact completeness | 0.05 | Required fields and provenance are present |

Task success requires a score of at least 0.85 and no safety-critical failure.
Canonical action identities, diagnosis matchers, and tolerances are versioned
with the scenario.

### 9.2 System measures

Report these separately rather than hiding them in one composite:

- end-to-end latency, time to first visible event, time to diagnosis, and time
  to mitigation;
- model calls, input/output/reasoning token counts where the server reports
  them, and model busy time;
- total, useful, redundant, invalid, failed, and retried tool calls;
- recovery rate after injected transient failures;
- number of delegations, handoffs, repeated findings, and unresolved conflicts;
- checkpoint count, pause/resume success, and replay success;
- peak VRAM, host RAM, GPU utilization, throughput, and queue time;
- native trace coverage and normalized-event completeness;
- developer implementation effort measured from issues/commits and a short
  structured engineering diary.

### 9.3 Baselines and ablations

- scripted oracle to test environment/scorer validity;
- unrestricted single agent;
- compute-matched single agent;
- no-review role;
- no-role-separation prompt-only run;
- no-fault run;
- framework-native tracing disabled to measure instrumentation overhead.

These detect whether “multi-agent” adds value or merely adds model calls.

## 10. Live comparison

A neutral local observer consumes normalized events from both runners. The
default view is a synchronized two-column timeline with:

- current phase and active agents;
- agent starts/completions and delegation edges;
- tool name, redacted arguments, result summary, latency, and error/retry;
- evidence ledger and hypothesis revisions;
- approval requests and decisions;
- elapsed budget, calls, tokens, and model queue time;
- checkpoint/pause/resume state;
- final score once the evaluator completes.

Raw private reasoning is neither requested by the UI nor stored. A model may
emit reasoning tokens to the inference server; the observer records only token
counts/timing and agent-authored concise status/evidence.

Each run links to its Phoenix trace for native span-level drilldown. The neutral
observer remains the only source for side-by-side semantic comparison because
native span shapes legitimately differ.

## 11. Post-hoc comparison

Each run produces the evidence bundle described in
[run-bundle.md](run-bundle.md). A report builder joins paired runs by scenario
seed and repetition and produces:

- outcome and safety tables;
- paired-difference plots with confidence intervals;
- latency and resource flame/timeline views;
- tool-use Sankey/sequence summaries;
- coordination topology and duplicate-work measures;
- fault-injection/recovery analysis;
- trace completeness matrix;
- links back to events, artifacts, checkpoints, and native traces.

Reports are generated from frozen bundles and never query live external data.

## 12. Safety and data handling

- MCP role permissions are real capability boundaries.
- Mitigations are simulator actions only.
- The ground-truth store is outside runner mounts.
- Sensitive event payload capture defaults off.
- Raw reasoning capture is prohibited by the v0.1 schema.
- Tool arguments/results are redacted before normalized persistence.
- Phoenix binds to loopback and external resources are disabled.
- Every destructive-looking simulated action requires approval.
- Model weights, secrets, runtime state, and trace databases are ignored by
  Git.
- Measured runs require a clean worktree; dirty protocol probes are explicitly
  labeled non-measurement evidence.

## 13. Delivery phases

### Phase 0 — reviewable foundation

- research and task decision;
- specification and ADRs;
- typed core contracts;
- deterministic generator and role-scoped MCP skeleton;
- model and experiment profiles;
- GitHub issue backlog and project board.

### Phase 1 — vertical smoke slice

- freeze prompts/tool schemas;
- characterize model/server on the 4090;
- implement one explicit workflow in both frameworks;
- normalized live CLI/observer stream;
- deterministic evaluator;
- one seed end to end with approval and recovery.

### Phase 2 — all matched lanes

- MAF Harness and Deep Agents;
- single-agent and scripted baselines;
- checkpoint/resume tests;
- Phoenix native tracing;
- paired pilot.

### Phase 3 — measured study

- fault and topology ablations;
- sample-size review;
- measured paired runs;
- offline report and decision ADR.

## 14. Exit criteria for the initial review

The reviewer should decide:

1. whether incident response is the right primary task;
2. whether the three-track framing (four multi-agent lanes plus two
   single-agent lanes) answers the intended question;
3. whether role-scoped MCP access is desirable or too constraining;
4. whether Qwen3.5-9B is acceptable as the fit-check model before the quality
   tier is characterized;
5. whether the neutral live observer should start as a web UI or a terminal UI;
6. the acceptable measured-study runtime budget.
