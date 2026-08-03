# Qwen3.5-27B zero-turn harness correction diagnostic

Status: exploratory diagnostic complete; excluded from canonical pilot evidence

Date: 2026-08-04

## Scope and provenance

The preregistered `opinionated_harness_correction_zero_ablation_v1` cohort ran
at clean source `1f9e634` against the accepted Qwen3.5-27B Q4_K_M llama.cpp
operating point. It retained the ADR 0017 specialist catalogs, prompts,
topology, budgets, cases, and validation rules, changing only the shared
provenance-correction allowance from one turn to zero.

Fresh characterization was measurement-eligible with no skipped cells and a
21,920 MiB whole-device peak. The complete MAF/LangChain compatibility matrix
passed. All 30 bundles and all 15 pairs validated and were inference-eligible.
The cohort index has `status = failed` because six operational failures remain
in the denominator.

## Outcomes

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF Harness | 2/15 | 12/15 | 3/15 |
| Deep Agents | 3/15 | 12/15 | 3/15 |

At the preregistered seed-cluster analysis unit, the Deep-minus-MAF task-
success risk difference is `0.066667`, with paired-bootstrap 95% interval
`[0, 0.2]` and exact sign-flip `p = 1.0` across five seed clusters. The
run-level paired counts are descriptive: one MAF-only success and two
Deep-only successes. The cohort is exploratory and neither interval nor test
supports a framework-difference claim.

Seed-level task successes were:

| Seed | MAF Harness | Deep Agents |
| ---: | ---: | ---: |
| 101 | 0/3 | 0/3 |
| 202 | 0/3 | 0/3 |
| 303 | 1/3 | 2/3 |
| 400 | 0/3 | 0/3 |
| 505 | 1/3 | 1/3 |

Removing correction did not remove either evidence-quality failures or native
loop pressure. MAF reached observed maxima of 21 model calls, 14 tool calls,
and 4,913 output tokens. Deep Agents reached 21 model calls, 29 tool calls, and
8,438 output tokens. Operational failures were retained as follows:

- three MAF runs failed because the commander marked evidence insufficient;
- one Deep run exhausted a native specialist's six-call middleware limit;
- one Deep synthesis cited only one specialist role; and
- one Deep synthesis both cited an unknown evidence identifier and used only
  one specialist role.

## Interpretation

Zero correction turns still exposed viable scored paths in both lanes, but
success remained sparse and concentrated in two incident families. The
failure mix also shows that correction is not the sole cause of the canonical
model-quality problem: evidence sufficiency, provenance, role coverage, and a
native specialist loop all failed before any correction path could rescue
them.

This cohort must not replace the canonical profile, qualify the failed
canonical task-quality gate, enter confirmatory aggregates, or size the
measured study. A causal correction-turn effect would require matched
zero/one/two-turn cohorts at the same frozen source and operating point; the
failed canonical one-turn gate intentionally blocks that comparison. The
result is diagnostic sensitivity evidence only.

## Evidence

The offline report revalidated every bundle and pair and produced a portable
checksummed evidence archive. Its evidence-index checksum verifies.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `2f0020a6f3279445a5aeb2fe07e20d9fe0b7597cf30d059fc33020ee019e0cd5` |
| `model-compatibility.json` | `eda94cf271735e18916411beb1d4ee63f2ea49c2bcd0a0b312528b977d370d4a` |
| resolved execution plan | `5054a8fec8f8a375743258e349f1cb7ed48dec1059bdb51e9249fcf3ca172827` |
| cohort index | `00af0d9dcb6dc0cd749a0e1219b99580a0efafd9723179d501954baf4970b3b8` |
| paired report JSON | `d7ffc612d467faa9cf71a2c8b2bbb6bb24b7aa2e8cac4e257f9ee84c884cd3db` |
| paired report HTML | `f82807c256c45212c0c846b0dda3f2d8c150c2d620a88f359e6f8a4bed85fc9d` |
| evidence index | `52beff0676f9b8200e59f8eb675e64482db2f77e06f3708284101c5cc57e2910` |
