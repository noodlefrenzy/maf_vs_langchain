# Qwen3.5-27B two-turn harness correction diagnostic

Status: exploratory diagnostic complete; excluded from canonical pilot evidence

Date: 2026-08-04

## Scope and provenance

The preregistered `opinionated_harness_correction_two_ablation_v1` cohort ran
at clean source `c7e0f55` against the accepted Qwen3.5-27B Q4_K_M llama.cpp
operating point. It retained the ADR 0017 specialist catalogs, prompts,
topology, budgets, cases, and validation rules, changing only the shared
provenance-correction allowance from one turn to two.

Fresh characterization was measurement-eligible with no skipped cells and a
21,765 MiB whole-device peak. The complete MAF/LangChain compatibility matrix
passed. All 30 bundles and all 15 pairs validated and were inference-eligible.
The cohort index has `status = failed` because four operational failures remain
in the denominator.

## Outcomes

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF Harness | 4/15 | 12/15 | 3/15 |
| Deep Agents | 7/15 | 14/15 | 1/15 |

At the preregistered seed-cluster analysis unit, the Deep-minus-MAF task-
success risk difference is `0.2`, with paired-bootstrap 95% interval
`[0, 0.466667]` and exact sign-flip `p = 0.5` across five seed clusters. The
run-level paired counts are descriptive: two MAF-only successes and five
Deep-only successes. The cohort is exploratory and neither interval nor test
supports a framework-difference claim.

Seed-level task successes were:

| Seed | MAF Harness | Deep Agents |
| ---: | ---: | ---: |
| 101 | 1/3 | 1/3 |
| 202 | 0/3 | 1/3 |
| 303 | 1/3 | 3/3 |
| 400 | 0/3 | 0/3 |
| 505 | 2/3 | 2/3 |

The extra turn created a real compute sensitivity. MAF reached the outer
32-model-call ceiling once; its observed maxima were 32 model calls, 15 tool
calls, and 5,518 output tokens. Deep Agents reached 20 model calls, 30 tool
calls, and 9,344 output tokens. Operational failures were retained as follows:

- two MAF runs failed because the commander marked evidence insufficient;
- one MAF run exhausted the 32-model-call outer budget; and
- one Deep run hit its native six-call specialist middleware limit during the
  second correction path.

## Interpretation

Two correction turns exposed viable scored paths that the final one-pair
canonical gate did not observe, including successes in both lanes. They also
increased work enough to trigger both outer and native phase limits, and the
effect was not consistent across repetitions or incident families.

This cohort must not replace the canonical profile, qualify the failed
canonical task-quality gate, enter confirmatory aggregates, or size the
measured study. A causal correction-turn effect would require a matched
canonical cohort at the same frozen source and operating point; that cohort is
intentionally blocked by the canonical gate. The result instead narrows the
diagnosis: a second correction can sometimes rescue evidence use, but it is
not a reliable or free remedy for the canonical model-quality failure.

## Evidence

The offline report revalidated every bundle and pair and produced a portable
checksummed evidence archive. Its evidence-index checksum verifies.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `d58c8800053c86e96ee34085f9d107f1a535c549300e76cf94f151e4ff2131ca` |
| `model-compatibility.json` | `61afadd601aeebe05fffc00fdf94d9cb1adb597a31f6ebba5ba37ec7c9302704` |
| resolved execution plan | `ad3cc1525056daa620bb4e61e812d1e5b0bd24b6939675ab4dedfab07aa460cf` |
| cohort index | `4af9e5c058c9fd94672a3231448c906648b39da33c314278221da4e8b929c127` |
| paired report JSON | `2754baf9b96738a5f69b4ab92117b8c2f116cc516a68b16ba8ce71b8e6053b94` |
| paired report HTML | `35a65a2e2b6aebd412bb60e02a3e7a1cd8a05df98ccfb5190671825542969c57` |
| evidence index | `85146fb3b7b6657e781bb8a721a7fa99a1fba45283562f5e81893800a5430b71` |
