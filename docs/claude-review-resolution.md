# Claude repository review resolution

Status: implementation reconciliation through 2026-08-03

This ledger maps the findings in `claude_repo_review.md` (reviewed at commit
`90a9d91`) to the current implementation. It does not rewrite the original
review and does not claim that post-fix empirical milestones have run.

## Comparison fairness

| Finding | Resolution |
| --- | --- |
| F-1 | MAF's non-configurable native connection-loss reconnect is preregistered as framework behavior; the shared prompt no longer claims the transport never reconnects. |
| F-2–F-4 | Tool errors, accounting, and MCP result parsing moved into the frozen shared explicit-workflow contract. |
| F-5 | All harness actors consume the same shared model request/output policy. |
| F-6–F-9 | Both harness adapters import one hashed delegation, strict tool/evidence provenance, one-correction, phase-limit, and explicit-failure policy. |
| F-10 | Deep streaming is disabled/funneled through `_agenerate`; a hermetic tool-loop plus public-stream fallback probe verifies per-dispatch accounting and pre-dispatch budget failure. |
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
| T-2 | CI validates all five experiment profiles and all three model profiles. |
| T-3 | Every identified nested `uv run` test/probe uses `--no-sync`. |
| T-4 | CI enforces branch coverage with a 55% floor (60% at introduction); coverage artifacts and mypy cache paths are ignored; GitHub Actions are commit-SHA pinned. |

The follow-up supply-chain gate is also mechanical: all four uv projects apply
a rolling 72-hour `exclude-newer` cooldown, every locked registry artifact's
upload time is audited fail-closed, and every dependency-installing CI job
depends on that audit.

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
   LangGraph run (0.916667) and one valid MAF evidence-gate failure. The pinned
   Qwen3.5-35B-A3B GPTQ-Int4 follow-up then loaded at both 16K/8 GiB offload and
   8K/2 GiB offload, but the pinned vLLM stack failed engine initialization
   when its Triton kernels received CPU tensors. No-offload loading cannot
   preserve useful KV-cache headroom on this host, so Qwen3.5-9B remains the
   fit-check model and no larger pilot profile was promoted.
4. The symmetric canonical/paraphrase-A and zero/one/two-turn harness ablation
   controls and profiles are implemented and protocol-probed, but unmeasured.
5. Production-intended checkpoint backends remain unselected and unvalidated;
   the compatibility file savers do not close that milestone. Runner metadata
   now makes that boundary machine-readable with `production_ready = false`
   and rejects any production-ready claim that is not resumable and restart-safe.
6. The fail-closed offline builder successfully revalidated the available
   clean-source smoke and Qwen-gate indices. A final powered paired report and
   component-discrimination analysis still require an accepted model and pilot.
7. The rolling 72-hour dependency quarantine is now enforced in every uv
   project and as a prerequisite CI job. It covers direct and transitive locked
   registry artifacts without adding package or index exclusions.
8. The pilot case manifest now covers all five generator families exactly once;
   seed 400 replaces the duplicate retry-storm seed 404. Every experiment
   profile passes the no-service execution planner in CI, and actual execution
   exclusively records the resolved case/ordering/contract plan before starting
   services. The empirical pilot remains blocked on an accepted model and the
   other documented P1 readiness gates.
9. Run evidence now carries monotonic normalized-event timestamps, separately
   timed control-plane phases, per-call model JSONL with optional
   cached/reasoning usage, and fixed-cadence host/GPU JSONL with measured
   sampling overhead. Validation fails closed on identity, schema, clock,
   sample-count, phase-shape, or usage reconciliation drift. Provider-specific
   queue/TTFT/generation and per-request cache-hit signals remain explicitly
   unavailable until the serving boundary exposes a correlation source; the
   profile separately preregisters warm/cold server and prefix-cache policy.
