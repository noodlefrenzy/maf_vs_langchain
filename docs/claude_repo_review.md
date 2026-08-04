# Repository Review — Agent Framework Field Lab

- **Reviewed:** 2026-08-02
- **Branch / commit:** `agent/opinionated-harness-experiment` @ `90a9d91`
- **Scope:** full source tree — `src/agent_lab/` (~6,900 LOC), `runners/maf/`, `runners/langgraph/`, `services/incident_mcp/`, `tests/` (104 tests), all docs/ADRs/configs/CI. Installed framework packages were inspected where adapter behavior depends on them.
- **Method:** five parallel deep-read reviews (core package, runner parity, MCP world service, tests/hygiene, docs-vs-code), synthesized here. Findings already documented in `docs/findings.md`, `docs/gotchas.md`, or `docs/REVIEW.md` are not re-reported as new (see §8).

> **Status: this is a historical snapshot, not an open defect list.**
> Every finding below has since been resolved or explicitly reclassified.
> The disposition of each one is tracked in
> [`claude-review-resolution.md`](claude-review-resolution.md), which maps
> findings to the commits that addressed them. This document is retained
> unedited because the review's original judgements — including the ones that
> invalidated retained runs — are part of the project's evidence trail. Read the
> resolution ledger alongside it; do not read this file alone as a description of
> the current codebase.

---

## 1. Executive summary

This is an unusually rigorous repository. The experiment's core validity claims — frozen hashed contracts, paired digest-verified worlds, budgets enforced outside the frameworks, fail-closed provenance validation, honest "smoke ≠ benchmark" framing — are not just documented, they are implemented, unit-tested, and in most cases adversarially tested. All 12 verifiable README/SPEC claims we checked matched the code exactly. The test suite tampers with its own artifacts and asserts specific failure codes; CI enforces four lockfiles; no secrets, caches, or run artifacts are committed.

The review nevertheless found real problems, in three tiers:

1. **Comparison-fairness risks (most important).** The explicit-workflow track (MAF Workflows vs LangGraph) is genuinely matched. The opinionated-harness track (MAF Harness vs Deep Agents) is **not yet apples-to-apples**: several adapter asymmetries currently compound in MAF's favor — a 2× specialist output-token cap gap, a provenance-correction retry only MAF receives, a stricter provenance validator only Deep Agents faces, and structurally different local call limits. Harness-lane results should not be compared head-to-head until these are reconciled or explicitly reclassified as framework-native behavior under test.
2. **Scoring-integrity bugs.** Two world-simulator defects can silently penalize a *well-behaved* agent (`verify_recovery` last-write-wins; nested evidence IDs never registered as observed), and three control-plane/validator seams guarantee that legitimate non-completed runs (budget-exhausted, cancelled, approval-timeout) produce bundles that can never validate — conflating legitimate outcomes with evidence corruption.
3. **Robustness/hygiene issues.** A 64 KiB protocol-line limit that crashes the control plane without a terminal record, a ~1 s post-terminal window that can truncate `trace-link.json`, duplicated gate/model code between the two runners with no drift guard (and one function already drifted), stale ADR statuses, and a few CI/doc gaps.

None of this undermines the project's direction — the smoke runs already carry the correct epistemic labels. But the harness-track asymmetries and the scoring-integrity bugs should be fixed **before** the planned baseline and repeated-run milestones, because they would contaminate exactly the data those milestones are meant to produce.

---

## 2. Architecture assessment

The system composes cleanly across four isolated `uv` projects (root `agent_lab`, two runners, the MCP service — deliberately not a workspace, per ADR 0007):

- `agent_lab.contracts` / `runner_protocol` — frozen Pydantic vocabulary (19 event kinds, 9 terminal states) and a versioned JSON-lines protocol (`0.1.0`) between control plane and runner subprocesses. Bearer tokens travel only over the subprocess pipe; the only persisted request view is redacted.
- `agent_lab.control_plane` — the outer authority: allowlisted subprocess environment, wall-clock/model/tool/token/approval budgets enforced independently of the frameworks, exclusive per-run directories, exactly one `terminal.json` per run (with the exceptions in §4).
- `agent_lab.runner_host` — runner-side session that meters usage *pre-dispatch* and raises fail-closed `WorkflowBudgetExceeded`; budgets are therefore enforced on **both** sides of the trust boundary, so a buggy adapter cannot under-report past the outer meter.
- `incident_lab_mcp.world` — a pure, tick-based simulator with **no RNG post-construction**: evidence IDs are hash-derived, fault injection is a deterministic function of the world's own call counter, and hidden ground truth is only exposed to the scorer. Paired lanes are deep copies of a *single* generated incident, digest-compared, each with its own HMAC approval secret.
- `bundle_validation` + `evaluator` — fail-closed forensic validation (manifest digests, contiguous sequences, event-derived vs terminal usage, artifact digests, span-ledger recheck) gating a deterministic six-component scorer.

Notable design strengths worth calling out explicitly:

- **Scripted observations in the workflow lanes** (exactly two hard-coded MCP calls per role, one structured model call, `tool_choice="none"`) remove model-driven tool-use variance from the matched comparison — a smart experimental control that isolates framework overhead.
- **Provenance is un-fakeable end-to-end**: evidence IDs must have been observed in real MCP results, cited tools must have really been called, audit records chain `result_sha256`, and `preflight_pair_contract` fails closed on contract-hash or topology drift before any paired run starts.
- **Approval-token design**: HMAC bound to `case_id:request_id:action` with a per-world random secret — tokens can't cross lanes, can't survive an edited action, do survive checkpoint restart, and are compared constant-time. Tokens are never serialized, never checkpointed, popped from memory on use.
- **Security posture**: timing-safe credential verification (signature checked before payload parse), loopback-only enforcement for HTTP service and trace collectors, DNS-rebinding protection, `O_EXCL`+0600 for every secret-bearing file, environment allowlisting for runner subprocesses, and redaction verified *by tests inside both runner venvs*.

---

## 3. Comparison fairness (the core question)

### 3.1 Explicit-workflow track (MAF Workflows vs LangGraph) — well matched

Prompts are byte-identical (shared `prompt_bundle` plus literal-equal user prompts), sampling parameters come from one `ModelProfile`, output caps match (`min(max_output_tokens, 2048)` both sides), SDK retries are disabled on both (`max_retries=0`), observation scripts and evidence gates are equivalent, and a test asserts literal output equality across the two runners' deterministic probes. Remaining caveats, in priority order:

| # | Asymmetry | Evidence | Impact |
|---|---|---|---|
| F-1 | **MAF's native MCP client silently reconnects and retries once** on connection loss; the LangChain adapter does not. Both lanes' prompts claim observations "were collected exactly once each with no retry". | installed `agent_framework/_mcp.py:2076-2131`; prompts at `runners/maf/src/maf_runner/workflow_lane.py:526-528`, `runners/langgraph/src/langgraph_runner/workflow_lane.py:437-439` | Under connection-dropping faults, MAF gets a free second attempt while LangGraph fails — a reliability bias, and the matched prompt text is factually wrong for MAF. Either disable the retry, or document it as framework-native behavior under test and fix the prompt claim. |
| F-2 | **Tool-error semantics diverge**: MAF raises `ToolExecutionException` on an `isError` MCP result (hard failure); LangChain returns the error text as the tool result (run proceeds). The repo's own compat probes encode both behaviors. | `runners/maf/src/maf_runner/http_compat.py:40-45` vs `runners/langgraph/src/langgraph_runner/http_compat.py:36-38` | Same injected fault → different terminal-status classification per lane. Matters as soon as fault profiles are enabled (planned next milestone). |
| F-3 | `tool_calls`/`model_calls` reporting semantics differ (MAF: deduplicated names, pre-call append counting failures; LangGraph: per-invocation, post-success). Equal today by construction; diverge on any retry/failure path. | `maf/workflow_lane.py:559-578,160-162` vs `langgraph/workflow_lane.py:429-431,482-484` | `role_stats` become incomparable under faults. Central telemetry counting is symmetric, so budgets are unaffected. |
| F-4 | `_render_tool_result` strictness differs: MAF raises `TypeError` on list results with no `.text`; LangGraph falls back to `json.dumps`. **This shared-by-copy function has already drifted.** | `maf/workflow_lane.py:800-807` vs `langgraph/workflow_lane.py:715-729` | Same MCP payload shape could crash one lane and feed the other. Symptom of the broader duplication problem (Q-1). |

### 3.2 Opinionated-harness track (MAF Harness vs Deep Agents) — materially asymmetric

These asymmetries were introduced reactively during per-lane stabilization (the history in `docs/gotchas.md` shows why each exists), but their **net effect compounds in MAF's favor**, and no document currently states plainly that the harness-track comparison is confounded until they are reconciled. `docs/findings.md` recommends testing correction policy "symmetrically" as future work, which partially acknowledges this.

| # | Asymmetry | Evidence | Direction |
|---|---|---|---|
| F-5 | **Specialist output-token caps differ 2×**: MAF harness specialists get `min(profile, 2048)`; the single Deep Agents client class bakes `min(profile, 4096)` for all actors including specialists. Commanders match at 4096. | `runners/maf/src/maf_runner/harness_lane.py:211` vs `runners/langgraph/src/langgraph_runner/harness_lane.py:81` | Biases token/latency/completion metrics in both directions depending on measure; unmatched either way. |
| F-6 | **MAF gets a provenance-correction retry; Deep Agents does not.** On validation failure MAF re-prompts the commander once with the validator error and allowlist; Deep Agents validates once and raises. | `maf/harness_lane.py:351-371` vs `langgraph/harness_lane.py:372-373` | Favors MAF on borderline runs. |
| F-7 | **The provenance validator is stricter on the Deep Agents side**: it checks `tools_used` against observed MCP calls; MAF's validator never checks `tools_used` at all. | `langgraph/harness_lane.py:44-51,230-235` vs `maf/harness_lane.py:236-250` | Favors MAF; compounds with F-6 (stricter gate, fewer chances). |
| F-8 | **Local call limits are structurally different**: MAF uses per-run function-invocation config (`max_iterations: 12, max_function_calls: 6`); Deep Agents uses `ModelCallLimitMiddleware(run_limit=6, exit_behavior="end")` plus `recursion_limit=64`, and its synthesis graph has no call limiter at all. Which cap binds first — and whether it ends gracefully or raises — differs per framework. | `maf/harness_lane.py:105-109` vs `langgraph/harness_lane.py:297,311,370` | Identical shared budgets can yield different terminal statuses. |
| F-9 | **Post-delegation enforcement differs**: Deep Agents raises explicitly if <2 specialists made MCP calls; MAF falls through to the proposal turn and fails later, with a different error class and extra model spend. | `langgraph/harness_lane.py:335-337` vs `maf/harness_lane.py:312-325` | Different terminal taxonomy and cost for the same underlying failure. |
| F-10 | **Metering robustness is asymmetric**: MAF's `ChatMiddleware` hard-rejects streaming and has a hermetic verification probe (`harness_dispatch_probe.py`); the Deep Agents `_agenerate` override would be **silently bypassed by any streaming path** — which would also bypass the shared budget hook — counts failed dispatches, and has no probe. | `maf/harness_lane.py:62-63` + `maf/harness_dispatch_probe.py` vs `langgraph/harness_lane.py:96-136` | The lane whose metering was recently found buggy (MAF, fixed at `90a9d91`) is now the *better*-verified one; the Deep Agents side has the weaker guarantee and no test. |
| F-11 | **Trace-capture asymmetry**: MAF instrumentation honors `capture_sensitive`; the LangChain instrumentor hardcodes output-hiding regardless of the flag. | `maf/protocol_runner.py:24-38` vs `langgraph/protocol_runner.py:37-55` | Any trace-content comparison sees MAF outputs but never LangChain outputs. |

**Recommendation:** before any harness-track baseline runs, either (a) equalize F-5–F-9 (single shared cap constant, symmetric one-retry correction policy, one shared validator that checks the same fields for both lanes, matched post-delegation checks), or (b) preregister them as "framework-native harness opinions under test" — but that framing is hard to defend for F-5/F-6/F-7, which are adapter policy, not framework opinion. Add a Deep Agents dispatch-metering probe (F-10) either way; it protects the shared budget boundary, not just a metric.

---

## 4. Scoring- and evidence-integrity findings

These can silently alter benchmark scores or invalidate legitimate runs — the two things this lab must never do.

### World simulator (`services/incident_mcp/src/incident_lab_mcp/world.py`)

- **S-1 (high). `verify_recovery` can un-verify recovery** — `world.py:797`. `self.recovery_verified = recovered` is last-write-wins. An agent that recovers correctly, verifies successfully, then makes a defensive final check that includes the decoy service (fixed 0.08 error rate > 0.02 threshold at `world.py:825-826`) overwrites `True` with `False` and loses the 0.10 recovery component despite a genuinely recovered world. Make it monotonic (`or`) or score on "ever verified" — or, if the trap is intentional, document it, because it currently punishes diligence.
- **S-2 (high). Nested evidence IDs are never registered as observed** — `world.py:791,799` vs `928-930`. Only the top-level `result["evidence_id"]` is registered, but `verify_recovery` returns per-service `checks` each carrying its own `evidence_id`. An agent citing an ID it literally received gets `INVALID_ARGUMENTS: not observed in this world`. Register nested IDs or strip them from nested objects.
- **S-3 (medium). Invalid control-plane approval calls still mutate the world** — `world.py:608`. `world_tick` increments before validation, so a retried mis-addressed approval permanently shifts every subsequent tick-derived evidence ID, silently desynchronizing paired lanes.
- **S-4 (low). Non-ASCII `approval_token` yields `INTERNAL_ERROR` instead of `STALE_APPROVAL`** — `world.py:742-745,914-926`. `hmac.compare_digest` raises `TypeError` on non-ASCII strings; a model hallucinating an en-dash in a token gets the failure blamed on the simulator. Compare `.encode()` bytes.

### Control plane / validator seam (`src/agent_lab/`)

- **S-5 (high). Non-completed runs produce bundles that can never validate** — `control_plane.py:298-304` vs `bundle_validation.py:339-366,401-431`. On budget violation (also timeout/cancellation) the control plane `break`s and stops draining stdout, so the runner's trailing events (`MODEL_RESPONDED`, `ERROR`, `RUN_FINISHED`) never reach `events.jsonl`. Validation then unconditionally errors (`run_finish_count`, `terminal_usage_mismatch`), marking every `BUDGET_EXHAUSTED`/`CANCELLED` bundle `invalid_provenance`. But the comparison protocol requires operational failures to stay in the denominator — this seam conflates a legitimate outcome with evidence corruption. Drain briefly after the break, or teach the validator the truncation contract for non-completed statuses.
- **S-6 (high). `APPROVAL_TIMEOUT` terminals are always flagged as budget violations** — `bundle_validation.py:433-448` with `control_plane.py:229,239-246,434`. The approval deadline *is* the wall-clock deadline, so approval timeouts necessarily exceed the wall clock and trip `budget_exceeded_without_status`. Any legitimately-timed-out approval run hard-fails validation.
- **S-7 (medium). Oversized protocol line crashes `execute()` with no terminal record** — `control_plane.py:179,453-479`. The subprocess stdout reader uses the 64 KiB default limit; one large event payload (unbounded `dict[str, Any]`) raises out of `execute()` — no `terminal.json`, no stderr capture, raw `ValueError` to the orchestrator. Every other malformed-message path converts to `RUNNER_FAILURE`; this one should too (and pass `limit=`).
- **S-8 (medium). Runner has ~1 s to flush tracing after its terminal message** — `runner_host.py:521-524`, `control_plane.py:108,487-520`, `native_tracing.py:296-300`. Terminal is sent *before* `tracing.finish()`; the control plane then SIGTERMs after `graceful_shutdown_seconds` (default 1.0 s). A slow Phoenix export can leave `trace-link.json` partial or missing → `native_trace_link_invalid`. Compounding: `_write_trace_link` claims "atomic" but is a bare write, unlike the validator's correct temp-file+rename sidecar pattern (`bundle_validation.py:1033-1040`).
- **S-9 (medium). Gate failures are misclassified as runner failures in both approved workflows** — `maf/workflow_lane.py:962-964`, `langgraph/workflow_lane.py:895-896`. When the evidence gate fails, both lanes raise a generic "did not persist approval request/interrupt" `RuntimeError`, discarding the actual `gate_reasons`. A model marking `evidence_sufficient=false` becomes a runner/protocol failure, corrupting the failure taxonomy the experiment measures.
- **S-10 (low). `scores.json` is schema-checked but never recomputed** — `bundle_validation.py:567-632`. The scorer is deterministic and all its inputs are validated in the same function; re-running `score_incident` would make tampered-but-plausible scores detectable. Similarly, the artifact-path containment check accepts `artifacts/../manifest.json` (`bundle_validation.py:485,513-515`) where the protocol's regex (`runner_protocol.py:173`) would not — this module's stated purpose is adversarial validation, so it should match the stricter rule.

---

## 5. Code-quality findings (non-scoring)

- **Q-1 (high, structural). Duplicated must-stay-identical code between runners with no drift guard.** The lane Pydantic models, evidence gate, and JSON scanners are copied verbatim across `maf/workflow_lane.py:57-153,800-866` and `langgraph/workflow_lane.py:52-129,715-788`. These are exactly the definitions the comparison's validity depends on, yet nothing pins them together the way `contract_bundle_hashes` pins prompts/tools — and `_render_tool_result` has already drifted (F-4). Either hoist them into `agent_lab` (like `harness_contracts`) or add a test that hashes both implementations' source and fails on divergence.
- **Q-2 (medium). Dead retry machinery is a latent parity break** — `InvestigationTask.attempt/feedback` plumbing exists in both lanes but no second-attempt loop is wired. If it ever is: MAF's provenance check reads instance-lifetime accumulated evidence (`maf/workflow_lane.py:559,567-573`) while LangGraph uses per-invocation locals (`langgraph/workflow_lane.py:428-431,467`), so MAF attempt-2 findings could cite attempt-1 evidence and pass where LangGraph's could not.
- **Q-3 (medium). `PrepareCommand` send sits outside the reaping try/finally** — `control_plane.py:231-232,426-429`. A runner that dies instantly can raise `BrokenPipeError` before the `try` is entered: zombie process, leaked stderr task, no terminal.
- **Q-4 (medium). Unsynchronized concurrent span-ledger appends** — `native_tracing.py:141,246-254`. `SimpleSpanProcessor` exports on whatever thread ends a span; concurrent buffered multi-line appends to `spans.jsonl` can interleave mid-line on frameworks that end spans on worker threads → `native_span_ledger_invalid` at validation. Add a lock.
- **Q-5 (medium). `_FileBackedSaver` persists private `InMemorySaver` internals** (`langgraph/checkpoint_compat.py:91-131`) — `storage/writes/blobs` have no stability contract, and the `langgraph>=1.2,<2` range pin admits minor bumps that could change their shapes mid-experiment.
- **Q-6 (medium). Silent command drop when runner stdin is closing** — `control_plane.py:482-485`. A pending `ApprovalCommand` can be silently discarded, burning the full remaining wall clock before dying as a timeout instead of failing fast.
- **Q-7 (low).** Oracle hashes CWD-relative paths (`oracle.py:70` + `git` without `cwd=`) — run from another directory it crashes or silently hashes the wrong repo; `experiment_control.py` does this correctly with `--repository-root`. | `verify_recovery`-style O(n²) JSON re-decoding in `_evidence_ids` (both runners) is pathological on large tool outputs. | `experiment_control.py:333-336` `finally` can hang (unbounded `await server_task`) or mask the body exception. | `workflow_finalization.py:39,41,47,109` raw `KeyError`s on field-incomplete runner output. | MAF checkpoint chmod-after-write TOCTOU (`maf/workflow_lane.py:165-169`) vs LangGraph's correct `O_EXCL`+0600+`os.replace` — mirror the latter. | Token-encoding malleability in `http_auth.py:261-266` (`altchars` doesn't disable standard base64 chars; no forgery, but tokens aren't canonical). | Expired pairs block `pair_id` reuse on long-lived registries (`http_auth.py:100`). | Same-actor concurrent model turns mispair events (`runner_host.py:118,130-133`). | Hardcoded signing secrets in the three compat scripts (fine for compat, worth a comment). | `--token` CLI args visible in `ps` in both `http_compat.py` mains.

Docstring/behavior mismatches worth fixing while in there: `native_tracing.finish()` claims atomic write (it isn't); `RunBundle` claims to assign event sequence numbers (the runner does, in the primary flow); `RunnerUsage` claims pre-dispatch emission (untrue for `output_tokens`); `validate_bundle` claims not to modify evidence files but the CLI default writes `validation.json` into the bundle; `model_compat_suite.py:45`'s `# noqa: S310 - loopback endpoint` comment is unenforced (`--base-url` accepts anything — unlike `NativeTracingProfile`, which actually validates loopback).

---

## 6. Tests, CI, dependencies, hygiene

**Test suite: exceptionally strong.** 104 tests, 100 passed + 4 env-gated live-model skips, 45 s wall locally. Real behavioral assertions throughout — exact event sequences, file modes, digest lengths, negative-space checks ("token not in file", "budget exhaustion must not request finalization"). The suite adversarially tampers with manifests, events, span ledgers, and evidence digests and asserts specific fail-closed finding codes. Nine test files spawn each framework in its own locked venv, mirroring ADR 0007 at test level. Cross-lane equivalence is asserted literally (`test_langgraph_workflow.py:79` byte-compares the two runners' outputs).

Gaps:

- **T-1.** The harness track's test coverage is asymmetric (mirrors F-10): `test_maf_harness.py` + `harness_dispatch_probe.py` exist; there is no Deep Agents equivalent, and harness lanes lack the deterministic offline probes the workflow lanes have. The only Deep Agents harness coverage is framework-neutral gate logic plus one live-gated test.
- **T-2.** CI (`.github/workflows/ci.yml` — three well-factored jobs with `uv lock --check` on all sub-projects) validates only 2 of 5 experiment configs and 2 of 3 model profiles; the unvalidated ones (`explicit-smoke.toml`, `harness-smoke.toml`, the llama.cpp model profile) are exactly what the live tests depend on.
- **T-3.** `test_control_plane.py:591` omits `--no-sync`, unlike every other subprocess call site — can trigger an implicit, network-touching env sync mid-suite.
- **T-4.** `pytest-cov` is installed but no coverage config, threshold, or CI step exists — wire it up or drop it. Actions are tag-pinned, not SHA-pinned.

**Hygiene: clean.** `git ls-files` confirms no caches, no `runs/` artifacts, no secrets tracked; local run artifacts correctly show `"token": "[REDACTED]"`; `.env.example` is loopback-only placeholders; Phoenix image is digest-pinned; the PR template carries a comparison-validity checklist. Root project has zero framework dependencies — neutrality enforced at the packaging layer. One deliberate-looking oddity: `agent-framework-tools==1.0.0b260721` is the repo's only exact pin (reasonable for a beta, deserves a comment). Minor: `.gitignore` lacks `.mypy_cache/`, `htmlcov/`, `.coverage.*`.

---

## 7. Documentation and methodology

**Docs are the repo's standout asset.** All 12 spot-checked claims (CLI commands, config paths, five frozen artifacts, event names, nine terminal states, score weights 0.30/0.25/0.15/0.15/0.10/0.05, threshold 0.85, protocol version, pinned framework versions, run IDs/scores in the committed indexes) verified against code with zero mismatches. `docs/comparison-protocol.md` preregisters confirmatory-vs-exploratory analysis, paired bootstrap CIs, and keeps operational failures in denominators — before any measured study exists. `docs/gotchas.md` version-qualifies every entry and records inconvenient results, including the MAF metering bug and its blast radius.

Issues:

- **D-1.** `docs/findings.md`, `docs/maf_vs_langchain.md`, and `docs/diagrams/` — the branch's primary intellectual output — are **untracked**. Commit them. The deck also duplicates findings numbers; two untracked files must currently change in lockstep.
- **D-2.** Six of seven ADRs still read `Status: Proposed` despite the documented 2026-07-31 approval (`docs/REVIEW.md`), and only 0006 was flipped to Accepted, unexplained.
- **D-3.** ADR 0004 (vLLM + Qwen3.5-9B) is contradicted by practice (llama.cpp + Qwen3-Next-80B, per all experiment configs) with no superseding ADR — violating `docs/adr/README.md:17-18`'s own rule. Post-approval decisions that deserve ADRs (two-phase harness synthesis, evidence-allowlist/correction policy, the JSON-lines runner protocol) live only in gotchas/findings.
- **D-4.** Numeric inconsistency: `docs/gotchas.md` says pre-fix ledgers held "25 to 34" LLM spans; `docs/findings.md` and the deck say "22 to 34". One is wrong.
- **D-5.** SPEC §5.5 incident-family names (`retry amplification`, `client connection leakage`) don't match the code enum (`retry_storm`, `connection_pool_exhaustion` — `src/agent_lab/scenario.py:16-20`).

Methodology gaps the docs don't yet acknowledge (beyond the substantial limitations `findings.md` already lists):

- **M-1.** The harness-track adapter-guard asymmetry (§3.2) is nowhere stated as a confound of the current post-fix results (three MAF `runner_failure` vs three Deep Agents `completed 0.35` at seeds 1742–1744 are downstream of asymmetric adapter policy).
- **M-2.** **Scorer discriminant validity**: every run to date scored 0 on containment because mitigation matching is exact-string. If both lanes always score 0 on ~40% of the weight (containment 0.30 + recovery 0.10), those components cannot differentiate frameworks; the oracle validates the scorer mechanically, not its sensitivity. Consider a graded or semantic-family match, or at least report component-level discrimination alongside totals.
- **M-3.** No prompt-perturbation ablation is planned; a single frozen phrasing could favor one harness's built-in instructions.
- **M-4.** The developer-effort measure (SPEC §9.2) has no ordering control — MAF lanes were debugged first and most, confounding any diary/commit-based effort comparison.

---

## 8. Already known (not re-reported as new)

Previous reviews and the findings/gotchas docs already establish: the MAF metering bug and its fix at `90a9d91` (pre-fix bundles at order seeds 1738–1741 invalidated for model-call metrics); structured output suppressing delegation in both harnesses → the two-phase design; models rewriting opaque evidence IDs → allowlists + bounded correction turn; LangChain callbacks not being fail-closed → `_agenerate`-level enforcement; Deep Agents' auto-added default subagent and unbounded redispatch → limiter; MAF Harness being experimental/non-resumable at 1.12.1; the tracing startup-overhead ablation (+107 ms MAF vs +528 ms LangGraph, startup-only); zero checkpoint-span coverage in both frameworks' native tracing; and universal containment failure due to exact-match mitigation semantics. This review's §3–§5 items are additive to that list.

---

## 9. Prioritized recommendations

**Before baseline/repeated-run milestones (blocks valid data):**
1. Reconcile harness-track asymmetries F-5–F-9 (shared cap constant, symmetric correction policy, one shared provenance validator, matched post-delegation checks) or preregister them explicitly as measured framework opinion; add the Deep Agents dispatch-metering probe (F-10/T-1).
2. Fix S-1 and S-2 (world scoring traps for well-behaved agents).
3. Fix S-5/S-6 so budget-exhausted/cancelled/approval-timeout bundles validate as their legitimate statuses — the statistical plan requires them in denominators.
4. Decide the F-1/F-2 policy (MAF native retry + tool-error semantics) before enabling fault profiles, and correct the "no retry" prompt claim.

**Soon (robustness of the evidence pipeline):**
5. S-7 (protocol line limit → `RUNNER_FAILURE`), S-8 (flush tracing before terminal, or lengthen graceful shutdown; make `_write_trace_link` atomic), S-9 (surface `gate_reasons`), Q-3, Q-4, Q-6.
6. Q-1: hoist the duplicated gate/models/scanners into `agent_lab` or hash-pin them; delete or finish the dead retry machinery (Q-2).

**Housekeeping:**
7. Commit `docs/findings.md`, the deck, and diagrams (D-1); update ADR statuses and write the superseding model-serving ADR (D-2/D-3); fix the 22-vs-25 discrepancy (D-4) and SPEC family names (D-5).
8. CI: validate all configs (T-2), add `--no-sync` (T-3), wire or drop coverage (T-4); recompute scores in `validate_bundle` (S-10).

---

## 10. What I would have done differently

Section 9 lists fixes; this section is the design-level retrospective — choices I'd have made at the start that would have prevented whole classes of the findings above, written with full acknowledgment of hindsight.

**Make symmetry a compile-time property, not a review-time discipline.** The repo's deepest structural bet was that two hand-mirrored adapters could be kept equivalent by care and code review. Almost every fairness finding (F-4 through F-9, Q-1, Q-2) is that bet failing in slow motion. I would have hoisted everything that must be identical — the lane Pydantic models, the evidence gate, the JSON scanners, the provenance validator, the cap constants, the retry/correction policy — into `agent_lab` on day one, exactly as was already done for `harness_contracts` and the prompt/tool bundle, leaving the runner projects with *only* framework-touching glue. The repo already proved this pattern works; it just stopped applying it one layer too early. Anything that genuinely can't be shared should be pinned by a source-hash test, so drift is a red build instead of a latent confound.

**Adopt a "paired-change rule" for adapter guards before the first stabilization run.** The harness-track asymmetries didn't come from carelessness — they came from debugging one lane at a time under a live model and patching whichever lane was misbehaving that day. The countermeasure is procedural: no lane-specific guard (retry allowance, call limiter, validator field, cap) may merge without either the mirrored change in the other lane or a written waiver classifying it as framework-native behavior under test. The PR template already has a comparison-validity checklist; this rule belongs on it. Retrofitting symmetry after results exist (the current situation) is strictly harder than never diverging.

**Design the world's evidence API around one invariant: anything the world returns is citable.** S-2 (nested evidence IDs never registered) is not a bug so much as a missing invariant — evidence registration happens in the transport layer (`_run_tool_locked`) while evidence *emission* happens in domain helpers, so the two can disagree. Registering IDs at the point of minting (`_evidence_id`) rather than at the point of response would have made S-2 unrepresentable. Same instinct for S-1: state that scoring flags are monotonic as a design rule, and the last-write-wins trap never gets written.

**Specify the bundle contract for *unhappy* terminals first.** The control plane and the validator visibly disagree about what a budget-exhausted bundle should look like (S-5/S-6) because the happy path was specified (`run-bundle.md` is thorough) while truncation semantics were left implicit. Given that the statistical plan explicitly keeps operational failures in denominators, I'd have written the run-bundle spec for `BUDGET_EXHAUSTED`/`CANCELLED`/`APPROVAL_TIMEOUT` before the completed-run path, and added one adversarial test per terminal status validating a real control-plane-produced bundle — the suite tampers with completed bundles brilliantly but never validates a non-completed one end-to-end.

**Property-test the simulator instead of example-testing it.** The world's claims — determinism, tick-purity, idempotent replay, evidence citability, monotonic scoring state — are invariants over arbitrary call sequences, which is exactly what property-based testing (Hypothesis) is for. `test_incident_world.py` is a strong example suite, but S-1/S-2/S-3 are all sequence-dependent traps that a generator issuing random tool-call sequences against paired worlds (asserting identical evidence-ID streams and monotonic flags) would likely have found in minutes.

**Follow the repo's own ADR 0004 sequencing, or supersede it in writing before diverging.** Jumping from the decided 9B-vLLM pilot to an 80B llama.cpp deployment without a superseding ADR (D-3) cost twice: the decision record is now misleading, and every stabilization run happened against a slower, more expensive model — with the model-behavior gotchas (structured-output delegation suppression, evidence-ID rewriting) discovered at 80B prices. A cheap-model shakedown phase would have surfaced most adapter bugs faster, reserving the big model for runs whose numbers might be kept.

**Treat the findings docs as evidence, not scratch.** `findings.md` invalidates specific run cohorts and pins version-qualified behaviors — it *is* part of the experiment's provenance chain, and it sat untracked while the runs it governs were committed to `runs/` indexes. I'd commit findings/gotchas in the same PR as the code they describe, and generate the deck's numbers from findings (or drop the duplication) so the two can't diverge (D-4 is that divergence already happening).

Two things I would **not** change, despite their cost: the four-project process isolation (it looks like ceremony until you notice the MCP SDK and both frameworks' instrumentation stacks genuinely conflict — ADR 0007 was right), and the scripted-observation workflow design (giving up "agentic" tool use in the matched lanes bought clean isolation of framework overhead, which is the question the track exists to answer).

---

## 11. Overall verdict

This repository is in the top tier of experimental-rigor engineering: neutrality enforced at the packaging layer, budgets metered on both sides of a trust boundary, un-fakeable evidence provenance, adversarial self-testing, and documentation that is honest about what its runs do and don't establish — all 12 code-verifiable doc claims held. The explicit-workflow track is ready to produce defensible comparisons once the fault-semantics policy (F-1/F-2) is decided. The opinionated-harness track is the right next focus and the branch is clearly mid-flight there; its current adapter asymmetries are the one place where the repo's own fairness standard is not yet met, and the scoring-integrity fixes (S-1/S-2, S-5/S-6) should land before any run whose numbers are meant to be believed.
