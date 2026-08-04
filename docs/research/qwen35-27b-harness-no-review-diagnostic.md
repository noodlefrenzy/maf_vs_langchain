# Qwen3.5-27B harness no-review diagnostic

Status: exploratory diagnostic complete; excluded from canonical pilot evidence

Date: 2026-08-04

## Scope and provenance

The preregistered `opinionated_harness_no_review_ablation_v1` cohort ran at
clean source `da77548` against the accepted Qwen3.5-27B Q4_K_M llama.cpp
operating point. It retained the canonical prompt family, ADR 0017 specialist
catalogs, budgets, cases, one provenance-correction turn, native compaction,
and validation rules. The sole planned topology change removed the reliability
reviewer from both lanes, leaving the commander and telemetry/change
specialists active.

Fresh characterization was measurement-eligible with no skipped cells and a
22,409 MiB whole-device peak. A transient WSL `nvidia-smi` timeout stopped the
resource-sampler thread after it had collected 327 aggregate samples. The two
final matrix cells consequently have no cell-local resource samples, so their
individual peaks must not be interpreted; the aggregate peak and task cohort
remain eligible. The complete MAF/LangChain compatibility matrix passed.

All 30 bundles and all 15 pairs validated and were inference-eligible. The
cohort index has `status = failed` because operational failures remain in the
denominator.

## Outcomes

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF Harness | 1/15 | 13/15 | 2/15 |
| Deep Agents | 1/15 | 2/15 | 13/15 |

At the preregistered seed-cluster analysis unit, the Deep-minus-MAF task-
success risk difference is `0`, with paired-bootstrap 95% interval
`[-0.2, 0.2]` and exact sign-flip `p = 1.0` across five seed clusters. The
run-level paired counts are descriptive: one MAF-only and one Deep-only
success, with exact McNemar `p = 1.0`. Equal aggregate success counts conceal
opposite seed-specific successes and sharply different completion behavior.

Seed-level task successes were:

| Seed | MAF Harness | Deep Agents |
| ---: | ---: | ---: |
| 101 | 0/3 | 0/3 |
| 202 | 0/3 | 0/3 |
| 303 | 1/3 | 0/3 |
| 400 | 0/3 | 0/3 |
| 505 | 0/3 | 1/3 |

MAF reached observed maxima of 32 model calls, 19 tool calls, and 6,587 output
tokens. Deep Agents reached 18 model calls, 23 tool calls, and 6,586 output
tokens. Operational failures were retained as follows:

- twelve Deep runs exhausted a native specialist's six-call middleware limit;
- one additional Deep synthesis cited an unknown evidence identifier, covered
  fewer than two specialist roles, and marked its evidence insufficient;
- one MAF commander marked its evidence insufficient; and
- one MAF run exhausted the 32-model-call outer budget.

The completed MAF scores ranged from 0.275 to 0.925 across 13 scored runs. Deep
had only two scored completions, at 0.475 and 0.9. Component distributions are
therefore descriptive and especially sparse for Deep; failed runs remain in
task-success and operational-failure denominators but do not invent scores.

## Interpretation

Removing the reviewer did not make the harnesses operationally equivalent or
simply reduce loop pressure. MAF usually reached a scored terminal, while Deep
usually exhausted a native specialist limit. Yet each lane achieved only one
task success, in a different seed cluster. Normal completion, operational
stability, and task success must therefore remain separate outcomes.

This cohort must not replace the canonical profile, qualify the failed
canonical task-quality gate, enter confirmatory aggregates, or size the
measured study. A causal reviewer-effect estimate would require a matched
reviewer-enabled cohort at the same frozen source and operating point; the
failed canonical gate intentionally blocks that comparison. The result instead
shows that topology is a material framework-specific treatment variable and
that equal task-success totals can hide substantial operational asymmetry.

## Evidence

The offline report revalidated every bundle and pair and produced a portable
checksummed evidence archive. Its evidence-index checksum verifies.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `ff98ce1ff5f514292fd684a5dce099276ea46b6962c5078d9497ddb8d2ab8513` |
| `model-compatibility.json` | `3f2a2ae03c43ea15f5ce79ec3440e3e967af19326bdea2014061c4904d763242` |
| resolved execution plan | `4c3fc10adce75d5cd3de3d370c4567c6465266a35d31eaadde99980ceb50c64b` |
| cohort index | `743ac79850896268ff26dfe0fdf2022448f28b2d306cf2bed1efc84238e69343` |
| paired report JSON | `116610275b802bc06e109e035aad64e46c23b4eb53d57e904df111575b746329` |
| paired report HTML | `dccbbe81b52c4053cd51daabc96ed410ddadfc24a4a2c548ab54e9403e5145d1` |
| evidence index | `5151d411e6a7d74d54d1f24bf618ef6cf85c68bc41e69a050fce9cfadba33416` |
