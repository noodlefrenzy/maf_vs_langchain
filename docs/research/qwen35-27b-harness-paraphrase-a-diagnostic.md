# Qwen3.5-27B harness paraphrase-A diagnostic

Status: exploratory diagnostic complete; excluded from canonical pilot evidence

Date: 2026-08-04

## Scope and provenance

The preregistered `opinionated_harness_paraphrase_a_ablation_v1` cohort ran at
clean source `5002a80` against the accepted Qwen3.5-27B Q4_K_M llama.cpp
operating point. It retained the ADR 0017 specialist catalogs, topology,
budgets, cases, one-turn provenance correction, reviewer role, native
compaction, and validation rules, changing both lanes from the canonical
prompt family to the matched paraphrase-A family.

Fresh characterization was measurement-eligible with no skipped cells and a
21,861 MiB whole-device peak. The complete MAF/LangChain compatibility matrix
passed. All 30 bundles and all 15 pairs validated and were inference-eligible.
The cohort index has `status = failed` because 15 operational failures remain
in the denominator.

## Outcomes

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF Harness | 0/15 | 6/15 | 9/15 |
| Deep Agents | 4/15 | 9/15 | 6/15 |

At the preregistered seed-cluster analysis unit, the Deep-minus-MAF task-
success risk difference is `0.266667`, with paired-bootstrap 95% interval
`[0.066667, 0.466667]` and exact sign-flip `p = 0.25` across five seed
clusters. The run-level paired counts are descriptive: no MAF-only successes
and four Deep-only successes. This exploratory five-cluster diagnostic is not
a powered framework comparison; the interval and randomization result must be
reported together.

Seed-level task successes were:

| Seed | MAF Harness | Deep Agents |
| ---: | ---: | ---: |
| 101 | 0/3 | 1/3 |
| 202 | 0/3 | 1/3 |
| 303 | 0/3 | 2/3 |
| 400 | 0/3 | 0/3 |
| 505 | 0/3 | 0/3 |

The paraphrase changed operational behavior despite preserving the shared
contract. MAF reached observed maxima of 32 model calls, 13 tool calls, and
5,875 output tokens. Deep Agents reached 23 model calls, 32 tool calls, and
14,133 output tokens. Operational failures were retained as follows:

- eight MAF runs failed because the commander marked evidence insufficient;
- one additional MAF run exhausted the 32-model-call outer budget;
- three Deep runs exhausted a native specialist's six-call middleware limit;
- one Deep commander marked evidence insufficient;
- one Deep synthesis cited fewer than two specialist roles; and
- one Deep synthesis failed structured proposal validation.

One MAF evidence-gate failure consumed all 32 allowed calls but did not attempt
call 33, so it remained a model failure rather than the separate budget-
exhausted terminal.

## Interpretation

The matched paraphrase was not operationally interchangeable with the
canonical wording at this local-model operating point. It produced no MAF task
successes, many MAF evidence-sufficiency failures, substantially longer Deep
outputs, and recurrent native specialist-limit failures. Prompt text is thus a
material treatment variable for this harness comparison, even when topology,
tools, budgets, validation, and model settings are held fixed.

This cohort must not replace the canonical profile, qualify the failed
canonical task-quality gate, enter confirmatory aggregates, or size the
measured study. A causal canonical-versus-paraphrase effect would require a
matched canonical cohort at the same frozen source and operating point; the
failed canonical gate intentionally blocks that comparison. The result does
satisfy the planned robustness check in the narrower sense that it exposes,
rather than conceals, prompt sensitivity.

## Evidence

The offline report revalidated every bundle and pair and produced a portable
checksummed evidence archive. Its evidence-index checksum verifies.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `f666e6203e663ba01b6a0a3b9990e6de05771a1658dceead6953e0f81d4ca9ce` |
| `model-compatibility.json` | `a71fabe9a98294b4522836b7c5a93e2ccec9416f475d31dd6b923bc15035e031` |
| resolved execution plan | `be402f31b2ea4a4634308b310663336c8bc6758ed8cfe53a296a92c20ebc6df1` |
| cohort index | `b0c337a8ab9507f8a669a59f5e1a51e2a932d70160f81b383f14c6b7b3f2647d` |
| paired report JSON | `f2b19529589df090be7ea9d66f35c5e598911960ee0473bde4cfad725afc1480` |
| paired report HTML | `4ff3d5f06f3c912f4d5201427934f03a0e6502566f7225851c81de6dca761871` |
| evidence index | `a8615e5f10f25d3168388e6347a5576e17965154ce9bb9143f5a165e929e1b52` |
