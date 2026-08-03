# Qwen3.5-27B matched-harness pilot: diagnostic launch

Status: diagnostic launch excluded; replacement gate and pilot complete

Date: 2026-08-03

## Scope and provenance

The first `opinionated_harness_pilot_v1` launch used clean source commit
`a18c076a3aba77dad9f8289bed75524836cd2a40` and the promoted Qwen3.5-27B
Q4_K_M llama.cpp operating point. Characterization was measurement-eligible,
had no skipped cells, and reached a 23,372 MiB whole-device peak. Every MAF and
LangChain compatibility fixture passed.

The launcher was stopped during seed 101 after repeated structural adapter
failures. Four bundles were preserved: one complete pair for repetition 0, one
MAF terminal for repetition 1, and the prepared Deep Agents peer. No cohort
index was finalized.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `77c67e9d8ea09106927f2e1888256bb355618f16cf4a3760b1d2edf5d446dfbe` |
| `model-compatibility.json` | `1488b3fb845748b42dc9775e7490ece057ea102b66cf79f264c396966c48b157` |
| resolved execution plan | `4c015dc0f4fc6f771d864765e30127452fbc0a616c0ced77c8c3587580dc33a2` |

## Observed failures

The complete Deep Agents lane was individually valid but terminated
`budget_exhausted` after 30 model calls and all 64 tool calls. Its telemetry
specialist alone issued 25 model requests because the six-call middleware was
present only on the parent graphs, not inside the native `SubAgent`.

Both MAF attempts terminated `model_failure` at the two-specialist MCP gate.
The commander had started telemetry and change specialists, but the gate ran
before either background task completed an MCP call. Each bundle then gained
four native spans after its trace link was finalized and failed validation on
span-ledger digest, span count (21 recorded versus 25 present), and trace
completeness.

The second Deep Agents bundle was prepared but never executed. Stopping at that
point avoided spending the remaining 26 launches on a cohort known not to
implement its frozen contract.

## Disposition

ADR 0014 applies the phase limiter within each Deep Agents specialist, freezes
a shared completion barrier, reaps parallel Deep task calls on exceptional
exit, and makes MAF wait for or cancel and drain native background work before
gate evaluation and trace finalization. MAF also reserves the six-call ceiling
across all internal completion-loop iterations.

These artifacts remain preserved for diagnosis and regression provenance. They
must not enter pilot aggregates, sample sizing, or a combined report. A clean
model gate and replacement matched-harness pilot are required under the new
harness-contract hash.

## Replacement gate and pilot

The replacement ran at clean source `1020b6f` under harness-contract SHA-256
`7a31ed1b38ef8158d5a0b9f44d973549d3fb79f8f42e9676059199a028c1ba88`.
Before the cohort, a fresh characterization completed every cell with no skips
and a 20,435 MiB peak, the full MAF/LangChain compatibility matrix passed, and
the paired task-quality gate produced two valid, successful 1.0-score lanes.
The gate directly exercised the repaired MAF completion barrier: all three
background specialists settled before commander synthesis and trace
finalization.

The full replacement characterization was independently measurement-eligible,
had no skipped cells, and reached a 20,624 MiB peak. Every compatibility
fixture and every framework/role actor probe passed. The five-seed,
three-repetition cohort then completed all 30 lanes and all 15 matched pairs;
every bundle and pair was valid and inference-eligible, with no late spans or
provenance-integrity findings.

| Replacement artifact | SHA-256 |
| --- | --- |
| gate `characterization.json` | `56268997bda579f782928116ddb8ba403d6c484d7c8622494876c9f3771688a5` |
| gate `model-compatibility.json` | `e857bdc67c4d04318ed982f5f6444116f885ccd03531e0afc9835e8ca1a25fe7` |
| gate resolved execution plan | `96596a5f47f404fb5d6a27b91a3cbefe60ba458c4e329326365ef4a438e7d695` |
| gate cohort index | `d01565c7b77a62386cc8e30e85f988d1b14e3430456cbebf72aaadcb3176f340` |
| pilot `characterization.json` | `488f8ae614604bd86ff119eab4e28c59f26dc6db7e3456f98c529c0f920747fb` |
| pilot `model-compatibility.json` | `59641a42570d91133c937c2f281da5a2231e43ef4c6f9621895d6ea9c15394d3` |
| pilot resolved execution plan | `ea5b0b76e31840e838302760f534802320dd6074dd46265794be3e47c0349916` |
| pilot cohort index | `1010ff519dbe959f63ae8f20371d607f93027833e5aea02deba4a6a7fb840986` |

## Replacement outcomes

Both lanes had zero task successes and a 100% operational-failure rate. These
failures stay in the primary-outcome denominator:

| Lane | Validity | Terminal reason | Count |
| --- | --- | --- | ---: |
| Deep Agents | valid | `ModelCallLimitExceededError` | 15 |
| MAF Harness | valid | strict role/tool/evidence provenance `ValueError` | 11 |
| MAF Harness | valid | `model_call_phase_limit_exhausted` | 3 |
| MAF Harness | valid | structured proposal `ValidationError` | 1 |

Every Deep Agents run issued 11 total model calls and stopped when one native
specialist attempted dispatch seven after consuming its six-call phase limit.
The MAF runs issued 14--17 model calls. Eleven proposals cited blank, invented,
management-plane, or otherwise unobserved tool/evidence provenance after the
single correction turn; three exhausted a native-loop phase reservation; and
one proposal assigned a finding to a non-specialist role.

The combined explicit-plus-harness `paired-report/v3` preserves all 30 matched
pairs. The explicit result remains MAF Workflow 3/15 versus LangGraph 5/15. The
harness result is MAF Harness 0/15 versus Deep Agents 0/15, a paired seed-level
risk difference of 0 with interval [0, 0] and exact sign p = 1. Harness
component maps are empty because no harness lane reached deterministic scoring.

| Combined-report artifact | SHA-256 |
| --- | --- |
| paired report JSON | `4475e5ea34e9f3bb39e28e032a06c3212cf9d36fb0e54857bb68e5f1514fe80c` |
| paired report HTML | `2ec799694a927ef79937aa64f27f5ec6b1fb1cc49be16c17e193c7ed7b838e0b` |
| evidence index | `7d725da8bd066c693349338d5acade610b7fff13c37a5b6068e6f0f3fed2b7e5` |

The evidence-index sidecar verifies. The preregistered study-sizing command
fails closed with `opinionated_harness pilot has zero paired seed variance;
sample size is unidentified`. This is the intended statistical guard: the
replacement closes ADR 0014's lifecycle-integrity defect, but it does not
qualify the canonical harness operating point for a measured comparison.
ADR 0015 revised the canonical shared interaction from this retained failure
evidence without relaxing the hard ceiling or validator: bounded completion is
explicit in the prompts and synthesis/correction receive exact role tool and
evidence allowlists.

Its clean one-pair gate at source `448b355` did not qualify the operating point.
Characterization was measurement-eligible with no skips and a 22,133 MiB peak,
and the complete compatibility matrix passed. Both task bundles and the pair
were valid and inference-eligible. Deep Agents nevertheless failed when its
change specialist ignored the prompt-only tool boundary, used six model
responses, and attempted more schema calls before dispatch seven. MAF's two
active specialists obtained role-specific evidence, but its native background
start/wait/retrieval loop consumed all six commander responses and attempted
dispatch seven. The aggregate reservation failed closed.

| ADR 0015 gate artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `40a20abb7c3606949e396cdad1369e033d1431c1206d31e67ca7ee30e810a717` |
| `model-compatibility.json` | `5b2d0b256600fe240cc42329e8ce9e2b6941512306472f9cebf81ef50e5c6e43` |
| resolved execution plan | `1a7a809aa3d7a07f6a64b5c5a50113c2fc30cf9644bffd47851b266436a2b2eb` |
| cohort index | `bfe95a021b40b46e4fc5ad6d919c48aaaf0948a81ddb4fee9afc3f11d53bac5b` |

Prompt-only bounded completion is therefore rejected for this model. Another
five-seed harness pilot remains blocked pending a newly frozen, mechanically
enforceable specialist completion boundary and bounded MAF result-retrieval
phase (or a different accepted model passing the same gate).
