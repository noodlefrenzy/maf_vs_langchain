# Qwen3.5-27B single-agent pilots

Status: compute-matched pilot complete; unrestricted pilot pending

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

## Interpretation and next step

The cohort supplies the required compute-matched single-agent baseline, but it
does not by itself establish a framework-wide winner or a multi-agent effect.
The primary explicit and harness comparisons remain separate tracks, the
harness replacement pilot is blocked by its task-quality gate, and the measured
study is not approved. Run the separately preregistered unrestricted
single-agent cohort next; it changes only the call and token ceilings and must
not be substituted for the compute-matched result.
