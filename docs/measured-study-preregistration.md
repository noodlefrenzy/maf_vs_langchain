# Measured-study preregistration

Status: blocked after accepted-model pilots; harness variance/discrimination
and independent-review gates remain open

The machine-readable source of truth is
[`configs/studies/measured-v1.toml`](../configs/studies/measured-v1.toml).
Validate it with:

```bash
uv run agent-lab study validate configs/studies/measured-v1.toml
```

Validation accepts an explicitly blocked protocol but fails closed if an
approved protocol omits the selected model, clean source commit, pilot-report
digest, final seed count, runtime estimate, component-discrimination result,
reviewer identity, or approval time. CI validates every study profile.

## Primary estimand

The single primary outcome is `task_success`: deterministic score at least
0.85 and no safety-critical failure. Operational failures count as false, so
model, tool-service, runner, budget, approval-timeout, cancellation, and valid
provenance-gate failures cannot disappear from the denominator. Only a pair
that fails the bundle/provenance integrity contract is excluded as a whole;
any replacement is allocated before outcome unblinding and the invalid pair is
retained for audit.

The smallest practically meaningful difference is a 10 percentage-point
absolute paired task-success difference. The two co-primary framework
contrasts are MAF Workflow versus LangGraph and MAF Harness versus Deep Agents.
They use two-sided familywise alpha 0.05 with Holm correction. Planning uses
the conservative alpha/2 boundary for each track; final analysis uses the
preregistered Holm procedure.

The analysis unit is the seeded incident case, not an individual stochastic
repeat. Each seed's five binary repetitions are averaged, and inference pairs
those seed-level success proportions. This avoids treating repeated model
samples of one hidden world as independent cases. The measured-study floor is
20 unique seeds and five repetitions per seed.

## Pilot sizing gate

The five-seed, three-repetition profiles are pilots only. The Qwen3.5-27B
Q4_K_M llama.cpp profile cleared both lanes of the paired gate, and the
still-blocked protocol freezes that profile with `model_gate_passed = true`.
The replacement explicit and harness pilots now exist in one fail-closed
`paired-report/v3`; run:

```bash
uv run agent-lab study size \
  configs/studies/measured-v1.toml \
  --pilot-report reports/combined-pilot/paired-report.json
```

For each track, the sizing command computes the sample standard deviation of
paired seed-level success-proportion differences, inflates that standard
deviation by the preregistered factor 1.25, and applies the normal paired-mean
planning approximation at 80% power and conservative per-track alpha 0.025.
It uses the larger track requirement, never goes below 20 seeds, fixes five
repetitions per seed, hashes the exact pilot report, and estimates sequential
GPU hours from median observed pair duration. Zero paired variance, fewer than
five seeds, fewer than three repetitions for any seed, a missing track, or an
unaccepted model fails closed instead of manufacturing a sample size.

The approximation is a planning rule, not the final test. The final report
retains raw paired points, averages repetitions within each seed, resamples
seed clusters for confidence intervals, and performs a two-sided paired
sign-flip randomization test on the seed-level differences. When both
confirmatory tracks are present, it applies the preregistered Holm correction
at familywise alpha 0.05. Run-level McNemar output is descriptive only.
Reviewer approval must assess the pilot's small-sample variance stability and
may increase—but not decrease—the computed seed count before freezing the
protocol.

## Confirmatory and exploratory analyses

Confirmatory:

- the task-success contrast within the explicit-workflow track;
- the task-success contrast within the opinionated-harness track; and
- operational-failure rates as a required reliability companion, without
  reclassifying them away from the primary outcome.

Secondary baseline analyses use the same seeds and report whether any apparent
multi-agent benefit survives the compute-matched single-agent ceiling. They do
not change the two-track primary family. Unrestricted single-agent results,
continuous scores, latency, tokens, tool use, resource metrics, tracing,
component scores, and coordination measures are secondary or exploratory.

The canonical/paraphrase-A prompt sensitivity, zero/one/two correction-turn,
no-review, and no-compaction profiles are matched exploratory analyses. Their
raw effects and uncertainty are reported without turning post hoc significance
into a confirmatory claim. Developer-effort evidence remains descriptive
because implementation order and debugging history were not randomized.

## Frozen conduct rules

- Run all planned blocks; there is no efficacy or futility stopping.
- Safety or infrastructure may halt execution, but the interrupted cohort is
  incomplete and cannot be relabeled a final study.
- Do not remove statistical outliers. Report all provenance-valid points.
- Freeze source, generator/case manifest, scorer, prompts, tools, model/server,
  budgets, fault schedule, approval policy, tracing/resource policy, and lane
  order generation before approval.
- Keep scripted approvals in every measured profile.
- Preserve portable redacted evidence plus owner-only raw bundles under the
  retention policy.
- Require observed component-level discrimination in the accepted pilot.
- Record reviewer identity and approval time before the first measured launch.

The command currently fails closed because the harness pilot produced zero
successes in both lanes and therefore zero paired seed variance. Its component
maps are also empty because no harness lane reached deterministic scoring.
Sample size and the required component-discrimination decision are therefore
unidentified; treating the measured-study floor as an estimate would violate
the preregistered rule. The explicit pilot remains usable evidence, but cannot
substitute for the missing harness variance.

The protocol remains blocked pending a newly frozen and clean-gated harness
pilot that supplies nonzero seed-level variance and scored component evidence,
plus independent reviewer approval. The model-selection blocker itself remains
closed for the exact accepted Qwen3.5-27B runtime; the observed canonical
harness/model interaction, rather than artifact fit or provenance integrity,
is the empirical blocker.
