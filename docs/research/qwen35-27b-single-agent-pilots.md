# Qwen3.5-27B single-agent pilots

Status: compute-matched and unrestricted pilots complete

## Compute-matched cohort

The preregistered `single_agent_compute_matched_pilot_v1` cohort ran at clean
source `567f28e` against the accepted Qwen3.5-27B Q4_K_M llama.cpp operating
point. It used the same 900-second, 32-model-call, 64-tool-call, and
32,000-output-token ceilings as each primary multi-agent lane. Five incident
families each received three matched repetitions, for 15 pairs and 30
sequential lane runs.

The cohort-specific characterization was measurement-eligible with no skips
and a 22,188 MiB whole-device peak. The complete MAF/LangChain compatibility
matrix passed. All 30 bundles and all 15 pairs validated and were
inference-eligible. The cohort index has `status = failed` because operational
failures remain in the denominator:

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF single agent | 3/15 | 9/15 | 6/15 |
| LangChain single agent | 11/15 | 15/15 | 0/15 |

MAF's six failures were deterministic within two incident families. All three
seed-101 repetitions proposed a reliability finding without making a call
through the runbook endpoint, so strict source provenance rejected them. All
three seed-303 repetitions explicitly marked the available evidence
insufficient, so the shared evidence gate rejected them. These are retained
model/framework outcomes, not invalid bundles.

At the preregistered seed-cluster analysis unit, the LangChain-minus-MAF task-
success risk difference is `0.533333`, with paired-bootstrap 95% interval
`[0.133333, 0.933333]` and exact sign-flip `p = 0.25` across five seed
clusters. This is descriptive pilot evidence, not a powered or confirmatory
result. Run-level repetitions are not treated as independent samples.

The offline report revalidated all pairs, retained failures, component scores,
native traces, resource ledgers, and portable evidence. Its evidence-index
checksum verifies. Component discrimination is observable for root-cause and
recovery scores in LangChain's 15 scored runs and for containment and
root-cause scores in MAF's nine scored runs.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `809b2eb7aba58f27e5a583c4b82ce83ee3393ab74979566497702d20d1755663` |
| `model-compatibility.json` | `0fcc23243a03cf7a84a3cddc0010e96307df8ae9b38920c8cbc3b058f578ca3e` |
| resolved execution plan | `fff00e614296fcc31c917b10880b40db59630db60412425ad91c02d9223b98e2` |
| cohort index | `395f45109af9e2356e73a8b3cbd1672a8830a4c6e849760544d3a79a47b39cf0` |
| paired report JSON | `80496e2450fb17cee2e5ea5fd2af727ca72f4036982309cddd67cd5ee3cfd8d1` |
| paired report HTML | `bd8cd4fe1c24b46224c45a131612e4c1fa0958b891dac44a846fcc01e05b61b5` |
| evidence index | `35f7df594179387c302a84fc6d56b0c21daa85b82f359a811a60b0639d22729b` |

## Unrestricted cohort

The separately preregistered `single_agent_unrestricted_pilot_v1` cohort ran
at clean source `f600117` with the same model, cases, repetitions, randomized
lane order, 900-second wall-clock limit, and approval policy. Only the ceilings
changed: 64 model calls, 128 tool calls, and 64,000 output tokens. Its fresh
characterization was measurement-eligible with no skips and a 22,194 MiB
whole-device peak, and the complete compatibility matrix passed.

All 30 bundles and all 15 pairs validated and were inference-eligible. As in
the compute-matched cohort, the index has `status = failed` because operational
failures remain in the denominator:

| Lane | Task successes | Normal completions | Operational failures |
| --- | ---: | ---: | ---: |
| MAF single agent | 3/15 | 9/15 | 6/15 |
| LangChain single agent | 11/15 | 14/15 | 1/15 |

MAF repeated the same failure families as the compute-matched cohort: all
three seed-101 repetitions omitted a runbook-endpoint call for the reliability
finding, and all three seed-303 repetitions marked the evidence insufficient.
One LangChain seed-202 repetition cited fewer than two capability sources and
was rejected by the shared evidence gate.

The relaxed ceilings were nonbinding. Across all unrestricted runs, observed
maxima were 10 model calls, 22 tool calls, and 3,981 output tokens. Those are
below the compute-matched ceilings of 32, 64, and 32,000 and exactly equal the
maxima observed in the compute-matched cohort. The higher-ceiling condition
therefore provides no evidence of budget rescue at this operating point.

The aggregate result and seed-cluster inference are identical to the
compute-matched cohort: MAF 3/15, LangChain 11/15, a LangChain-minus-MAF risk
difference of `0.533333`, paired-bootstrap 95% interval
`[0.133333, 0.933333]`, and exact sign-flip `p = 0.25` across five seed
clusters. Two LangChain seed-202 repetitions changed success/status in
opposite directions between cohorts, leaving the aggregate unchanged. That is
consistent with the protocol warning that a requested model seed does not make
GPU generation bitwise deterministic; it is not evidence that the ceiling
change caused an outcome.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `c72ba55df4a585432b6badee35eb66065e52ee21cb972b8059d2e75f32d1b303` |
| `model-compatibility.json` | `4b18ce41e80c8bde1e61071fcbeaa3d978349ce24bd42f93981362ccf05cc2d6` |
| resolved execution plan | `d52c68eb11d778f0d47bcb7ede259c07d37e4cefd2d9d8f15bbe9bea22b2ce29` |
| cohort index | `6d793efd28fe523826a2b5e2bcd3b5862a5a43831e287ef21bddb5696a785df9` |
| paired report JSON | `6022399996488f53f3e8d845ab9a799b2838e7663684639bd542a57f37dbc823` |
| paired report HTML | `01bba2f7e5794f785b088638f1236d413cc24b526f6ce03d8909af58b02bfff7` |
| evidence index | `31a6f63824e2a3d9e203915e37fb25c141e8eb9c7ba4f54597897c7612cfd3b3` |

## Interpretation and next step

The two cohorts supply the required compute-matched baseline and its
higher-ceiling sensitivity check, but they do not establish a framework-wide
winner or a multi-agent effect. The primary explicit and harness comparisons
remain separate tracks, the harness replacement pilot is blocked by its
task-quality gate, and the measured study is not approved. Retain the
compute-matched result as the primary single-agent baseline; the unrestricted
result is a sensitivity condition and must not be substituted for it.
