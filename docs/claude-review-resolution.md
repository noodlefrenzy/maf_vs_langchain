# Claude repository review resolution

Status: implementation reconciliation through 2026-08-04

This ledger maps the findings in `claude_repo_review.md` (reviewed at commit
`90a9d91`) to the current implementation and retained empirical milestones. It
does not rewrite the original review or pool diagnostic launches with accepted
pilot evidence.

## Comparison fairness

| Finding | Resolution |
| --- | --- |
| F-1 | MAF's non-configurable native connection-loss reconnect is preregistered as framework behavior; the shared prompt distinguishes it from the symmetric bounded application retry. |
| F-2–F-4 | Tool errors, per-dispatch accounting, MCP result parsing, and the one-retry structured-error policy moved into the frozen shared explicit-workflow contract. |
| F-5 | All harness actors consume the same shared model request/output policy. |
| F-6–F-9 | Both harness adapters import one hashed delegation, strict tool/evidence provenance, one-correction, phase-limit, subagent-completion, and explicit-failure policy. Native specialist work settles before the gate and is canceled and drained on exceptional exit. |
| F-10 | Deep streaming is disabled/funneled through `_agenerate`; hermetic probes verify per-dispatch accounting, pre-dispatch budget failure, the Deep specialist ceiling, MAF's aggregate native-loop ceiling, and exceptional subagent draining. |
| F-11 | LangChain input and output hiding now both follow `capture_sensitive`; an isolated runner test covers both flag values. |

The pre-fix harness runs remain stabilization evidence only. A comparable
harness result requires a new pair under the current
`harness_contract_sha256`.

## Evidence and scoring integrity

| Finding | Resolution |
| --- | --- |
| S-1–S-4 | Recovery verification is monotonic, nested returned evidence is citable, invalid approval control calls do not advance the world, and approval-token comparison uses bytes. |
| S-5–S-6 | Shutdown tails are drained and the validator has explicit partial-stream and approval-timeout semantics for legitimate unhappy terminals. |
| S-7 | The control plane sets a 1 MiB protocol-line limit and converts overflow into a reaped `runner_failure` bundle. |
| S-8 | Runner tracing finishes before terminal emission; shutdown grace is five seconds and `trace-link.json` is written with an exclusive atomic replace. |
| S-9 | Shared `EvidenceGateFailed` preserves gate reasons and maps failures into the experiment terminal taxonomy. |
| S-10 | Validation recomputes deterministic scores from validated inputs and enforces strict artifact-path syntax/containment. |

## Robustness and hygiene

| Finding | Resolution |
| --- | --- |
| Q-1–Q-2 | Must-match workflow schemas, prompts, parsers, gates, and accounting are shared and hash-frozen; dead retry fields were removed. |
| Q-3–Q-4 | Prepare-send failure is inside the reaping lifecycle; span-ledger writes are lock-serialized. |
| Q-5 | LangGraph restart compatibility persists and replays public saver operations through the public serializer instead of serializing `InMemorySaver` internals. |
| Q-6 | Sending to closing runner stdin raises immediately. |
| Q-7 | Repository hashing is CWD-stable; evidence extraction is linear; server cleanup is bounded; finalization validates fields explicitly; MAF checkpoint temp files start at 0600; credentials use canonical base64url; expired pair IDs are reusable; concurrent same-actor turns retain their own parent events; compat keys are labeled non-production; tokens are environment-only rather than CLI arguments. |
| Docstrings | The tracing atomicity claim is now true; usage documentation distinguishes pre-dispatch call reservation from post-response token accounting; live model compatibility enforces an HTTP loopback `/v1` endpoint. |

## Tests and CI

| Finding | Resolution |
| --- | --- |
| T-1 | Added the Deep Harness dispatch/budget/stream-fallback probe and shared harness-policy unit tests. |
| T-2 | CI plans every experiment profile and validates every model and study profile. |
| T-3 | Every identified nested `uv run` test/probe uses `--no-sync`. |
| T-4 | CI enforces branch coverage with a 55% floor (60% at introduction); coverage artifacts and mypy cache paths are ignored; GitHub Actions are commit-SHA pinned. |

The follow-up supply-chain gate is also mechanical: all four uv projects apply
a rolling 72-hour `exclude-newer` cooldown, every locked registry artifact's
upload time is audited fail-closed, and every dependency-installing CI job
depends on that audit.

The related P1 neutral-observability backlog is also implemented: completed
bundle replay and live paired timelines use one FastAPI/SSE surface, with
Phoenix/artifact links and a Rich renderer over the same sanitized API.
Optional operator approvals are loopback bearer-authenticated, owner-only,
audited, and deadline-bound; unattended profiles continue to freeze the
scripted policy. A clean interactive pair validated live discovery, one
approval on each lane, successful completion, and exact-cursor replay; the
acceptance record is retained in
`research/neutral-observer-validation.md`.

## Documentation and methodology

| Finding | Resolution |
| --- | --- |
| D-1 | Findings, deck, and diagram sources/assets are tracked. |
| D-2 | The 2026-07-31 decisions are marked Accepted; ADR 0004 is marked Superseded. |
| D-3 | ADRs 0008–0010 record split fit/development model roles, the runner protocol, and symmetric two-phase harness policy. |
| D-4 | Retained ledgers establish the pre-fix MAF range as 22–34 (34, 25, 29, 22), now consistent across findings, deck, and gotchas. |
| D-5 | SPEC family labels now include their exact generator enum IDs. |
| M-1 | Adapter-guard asymmetry is removed and the old cohort is explicitly excluded from comparison. |
| M-2 | Canonical action IDs repaired exact mitigation scoring; the pilot must still report component-level discrimination. |
| M-3 | Matched prompt-paraphrase sensitivity is now a preregistered pre-study requirement. |
| M-4 | Retrospective developer-effort evidence is explicitly descriptive until implementation order is balanced or randomized. |

## Remaining empirical milestones

The review's code and documentation findings are resolved or explicitly
governed above. The following are experiment work, not unresolved repository
defects. The single-agent adapters, two-phase source-evidence gate, paired live
compatibility probe, preregistered profiles, and offline report builder are
implemented. Current milestone status is:

1. A post-parity harness pair was collected at source `2b1a7e9`; its MAF lane
   failed provenance validation and its Deep Agents lane completed at 0.35, so
   the failure remains in the denominator.
2. A compute-matched single-agent smoke pair was collected at `2b1a7e9`; MAF
   completed successfully at 0.916667 while LangChain failed its evidence gate.
   The unrestricted and five-seed profiles remain unmeasured.
3. The corrected Qwen3.5-9B 32K gate at `bb61e34` produced one successful
   LangGraph run (0.916667) and one valid MAF evidence-gate failure. A second
   clean gate at `6bc7815` reversed the split: MAF succeeded at 0.916667 while
   LangGraph completed at 0.6 without effective containment or recovery. Since
   neither pair had two successful lanes, Qwen3.5-9B remains the fit-check model.
   The pinned Qwen3.5-35B-A3B GPTQ-Int4 follow-up loaded at both tested CPU-
   offload points but failed engine initialization. The pinned Qwen3.5-27B
   Q4_K_M llama.cpp follow-up then passed both restart characterizations, the
   complete client suite, and both lanes of its paired task-quality gate at
   source `6989bb0`; that exact profile is promoted to the five-seed pilots.
4. The symmetric canonical/paraphrase-A, zero/one/two-turn, no-review, and
   no-compaction harness ablation controls and profiles are implemented and
   protocol-probed, but unmeasured. Active roles, prompts, and native compaction
   mode are hash-frozen into pair preflight and effective topology.
5. Cosmos DB is selected as the symmetric production-intended checkpoint
   service. MAF's first-party provider and Microsoft's LangGraph provider each
   passed an HTTPS, fresh-process approval resume against a pinned emulator.
   The compatibility file savers remain the default, so runner metadata still
   reports `production_ready = false`; actual Azure RBAC and availability are
   deployment acceptance gates. See `production-checkpoints.md`.
6. The fail-closed offline builder successfully revalidated the available
   clean-source smoke and Qwen-gate indices. Its v3 output now reports task,
   token, provider-timing, client-concurrency, host/GPU, control, and trace
   effects and creates portable redacted evidence plus a checksummed owner-only
   copy of every raw bundle. The archive no longer depends on workstation paths;
   transfer and publication rules are documented in `evidence-retention.md`.
   The combined accepted-model pilot report is now complete and checksummed.
   It retains 30 explicit and 30 harness lanes; the harness component maps are
   empty because all harness outcomes were operational failures, so the
   component-discrimination gate remains open.
7. The rolling 72-hour dependency quarantine is now enforced in every uv
   project and as a prerequisite CI job. It covers direct and transitive locked
   registry artifacts without adding package or index exclusions.
8. The pilot case manifest now covers all five generator families exactly once;
   seed 400 replaces the duplicate retry-storm seed 404. Every experiment
   profile passes the no-service execution planner in CI, and actual execution
   exclusively records the resolved case/ordering/contract plan before starting
   services. The first 27B explicit pilot launch at source `23617ff` is retained
   as diagnostic-only evidence: the old zero-retry contract made the standard
   fault cadence guarantee failure, and undrained MAF fan-out siblings produced
   six invalid late-span bundles. ADR 0013 adds one symmetric, metered retry and
   drains escaped fan-out failures. The clean replacement gate and 30-lane pilot
   at `a18c076` completed with every lane and pair valid. MAF task success was
   3/15 and LangGraph task success was 5/15; the descriptive pilot report and
   portable evidence archive are complete.
9. Run evidence now carries monotonic normalized-event timestamps, separately
   timed control-plane phases, per-call model JSONL with optional
   cached/reasoning usage, and fixed-cadence host/GPU JSONL with measured
   sampling overhead. Validation fails closed on identity, schema, clock,
   sample-count, phase-shape, or usage reconciliation drift. Provider-specific
   request IDs and the pinned vLLM queue/TTFT/generation extension are preserved
   symmetrically by both adapters, while detailed usage provides per-request
   cached tokens. The profile separately preregisters warm/cold server and
   prefix-cache policy. A clean live pair validated exact reconciliation,
   complete GPU collection, measured cadence misses, and seven-phase control
   timing; see `research/run-observability-validation.md`.
10. The representative-workflow tracing ablation is complete at clean source
    `1e85861`: four matched blocks per condition balance condition order and
    lane order across 16 launches. Tracing added median paired total-control
    overhead of 134 ms for MAF and 423 ms for LangGraph; explicit flush was
    6--7 ms, every enabled trace met the expected 0.952381 completeness
    contract, and no material GPU/provider shift was detected. The raw
    aggregate dataset and interpretation limits are retained in
    `research/native-tracing-overhead.md`.
11. The measured-study protocol is now machine-readable and CI-validated. It
    freezes `task_success`, a 0.10 practical risk difference, seed-cluster
    analysis, multiplicity, failures, invalid pairs, outliers, stopping,
    prompt sensitivity, ablations, evidence retention, and descriptive-only
    developer effort. Approval fails closed without a passed model gate, pilot
    report hash, computed seed/runtime plan, component discrimination, clean
    source, and reviewer sign-off. Both accepted-model pilots and their combined
    report now exist, but sample sizing correctly fails closed because the
    harness track has zero paired seed variance; see
    `measured-study-preregistration.md`. Report schema v3 makes the frozen
    analysis unit executable: repetitions are averaged within seed, bootstrap
    intervals resample seeds, paired sign-flip p-values operate on seed-level
    effects, and the complete two-track family receives Holm adjustment.
12. The first accepted-model matched-harness pilot launch at `a18c076` was
    stopped during seed 101. Deep Agents omitted the frozen phase limiter from
    native specialists, while MAF evaluated the gate and finalized traces before
    background specialists settled. ADR 0014 moves the limiter inside every
    Deep specialist, freezes a cross-framework completion barrier, drains both
    native task mechanisms on exceptional exit, and bounds MAF's completion
    loop across the whole phase. The retained launch is diagnostic-only. The
    clean replacement gate and 30-lane pilot at `1020b6f` completed with every
    bundle and pair valid and inference-eligible, closing the lifecycle and
    late-span defects. Empirically, however, both canonical lanes were 0/15:
    every Deep Agents run reached the specialist six-call ceiling, while MAF
    produced eleven strict-provenance failures, three phase-limit failures, and
    one structured-proposal validation failure. The combined report therefore
    has zero harness paired seed variance and no harness component scores; a
    revised, newly hash-frozen harness operating point must be clean-gated and
    piloted before measured-study sizing can proceed.
