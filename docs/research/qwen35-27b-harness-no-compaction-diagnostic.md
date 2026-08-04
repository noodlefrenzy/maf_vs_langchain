# Qwen3.5-27B harness no-compaction diagnostic

Status: exploratory diagnostic complete; excluded from canonical pilot evidence

Date: 2026-08-04

## Scope and provenance

The preregistered `opinionated_harness_no_compaction_ablation_v1` cohort ran at
clean source `06cc265` against the accepted Qwen3.5-27B Q4_K_M llama.cpp
operating point. It retained the canonical prompt family, ADR 0017 specialist
catalogs, topology, budgets, cases, one provenance-correction turn, reviewer
role, and validation rules. The sole planned change disabled each framework's
native harness context-reduction mechanism: MAF agents used
`disable_compaction=True`, while the Deep Agents profile excluded
`SummarizationMiddleware` for the coordinator and declarative subagents.

Fresh characterization was measurement-eligible with no skipped cells and a
22,418 MiB whole-device peak. The hardened resource sampler recorded two
transient GPU-source errors and continued collecting 1,038 aggregate samples.
All four matrix cells retained local samples (`70`, `91`, `284`, and `336`),
including 336 samples in the final cell after its recorded GPU error. This is
the first live characterization to confirm that a sampling-source exception no
longer terminates subsequent collection. The complete MAF/LangChain
compatibility matrix passed.

All 30 bundles and all 15 pairs validated and were inference-eligible. The
cohort index has `status = failed` because four operational failures remain in
the denominator.

## Outcomes

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF Harness | 0/15 | 12/15 | 3/15 |
| Deep Agents | 4/15 | 14/15 | 1/15 |

At the preregistered seed-cluster analysis unit, the Deep-minus-MAF task-
success risk difference is `0.266667`, with paired-bootstrap 95% interval
`[0, 0.666667]` and exact sign-flip `p = 0.5` across five seed clusters. The
run-level paired counts are descriptive: no MAF-only successes and four Deep-
only successes, with exact McNemar `p = 0.125`. This exploratory five-cluster
diagnostic is not a powered framework comparison; the interval and
randomization result must be reported together.

Seed-level task successes were:

| Seed | MAF Harness | Deep Agents |
| ---: | ---: | ---: |
| 101 | 0/3 | 1/3 |
| 202 | 0/3 | 0/3 |
| 303 | 0/3 | 3/3 |
| 400 | 0/3 | 0/3 |
| 505 | 0/3 | 0/3 |

MAF reached observed maxima of 21 model calls, 18 tool calls, and 4,413 output
tokens. Deep Agents reached 21 model calls, 31 tool calls, and 9,064 output
tokens. Operational failures were retained as follows:

- three MAF commanders marked their evidence insufficient; and
- one Deep synthesis cited fewer than two specialist roles.

The two seed-101 MAF failures also encountered the fixture's simulated,
retryable `get_change_detail` timeout before the commander rejected its
evidence. The timeout was returned through the normal tool-error path; the
terminal gate reason remained evidence insufficiency. The completed MAF scores
ranged from 0.4625 to 0.8375 across 12 scored runs. Deep's 14 scored
completions ranged from 0.35 to 1.0.

## Interpretation

This operating point did not eliminate either task-quality or evidence-quality
failure. Deep produced four successes concentrated in two seed clusters, while
MAF produced none; both lanes still returned many normally completed but
unsuccessful runs. Disabling native context reduction also did not make the two
frameworks' effective context behavior equivalent: the public controls are
framework-specific, and their native delegation histories, workspace state,
and message construction remain part of the treatment.

This cohort must not replace the canonical profile, qualify the failed
canonical task-quality gate, enter confirmatory aggregates, or size the
measured study. A causal compaction-effect estimate would require a matched
compaction-enabled cohort at the same frozen source and operating point; the
failed canonical gate intentionally blocks that comparison. In particular,
the same aggregate `0/15` versus `4/15` task-success counts seen under
paraphrase-A do not make the two profiles interchangeable: their seed pattern,
failure counts, prompts, and context policy differ.

## Evidence

The offline report revalidated every bundle and pair and produced a portable
checksummed evidence archive. Its evidence-index checksum verifies.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `60cf7a30ce3a114ccde6c1e7336e89838227679ca18651f04934611947c7e1a7` |
| `model-compatibility.json` | `a96b9ec04963749fb3e1a618333de2d37d8ba8357adba9933a9e5ed1da25284f` |
| resolved execution plan | `01c0b9af5d04d2d68c1430eb2b7fdc44b498108edc75d6a9e6258a6e2e0e68b5` |
| cohort index | `562d27cb0bb10dafb538c42ddc4f49e2c6e8686898f0a8fadca70f0166e38444` |
| paired report JSON | `957d5718a3504d2e06dcc2f877d9ec138e18dba92f54a4221e1ebfc75451b8c0` |
| paired report HTML | `83f9f52c712826b9e633671f7e23f950925fe3227d90231ceabe40148587788f` |
| evidence index | `49100b15793a935459905fcfa38425b1e9435b8f557a5db3551c776db4c18316` |
