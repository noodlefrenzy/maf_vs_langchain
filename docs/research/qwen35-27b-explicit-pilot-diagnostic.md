# Qwen3.5-27B explicit pilot: diagnostic launch

Status: diagnostic launch excluded; replacement gate and pilot complete

Date: 2026-08-03

## Scope and provenance

The first full `explicit_workflow_pilot_v1` launch ran five frozen seeds, three
repetitions, and both lanes for 30 total runs. It used clean source commit
`23617ff650c8eba6963ea78431d44c544a9f406d` and the promoted Qwen3.5-27B Q4_K_M
llama.cpp operating point.

Fresh-source characterization was measurement-eligible with no skipped cells
and a 23,267 MiB whole-device peak. The complete MAF and LangChain compatibility
suite passed before the cohort began.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `19359455ee4b19a83759e3f709d804ddcc205631b62be71f036af63cc7640c3e` |
| `model-compatibility.json` | `33231442f2b5ae9cc3436c934bddcddb5d36ba668948f2652399b7859ab87e98` |
| resolved execution plan | `fbcfb2ebfe4592c4f166630b29733b6195ebf1819adc934b7349322f7fb0b363` |
| failed cohort index | `ed940ca55e7c974bb41983258b486cfc0a9bb321aa3a8c8f88e154e04ac37593` |

## Outcomes

The cohort index ended `failed` after preserving all 30 lane outcomes:

| Lane | Validity | Terminal status | Count |
| --- | --- | --- | ---: |
| LangGraph | valid | `tool_service_failure` | 15 |
| MAF Workflow | valid | `tool_service_failure` | 8 |
| MAF Workflow | valid | `runner_failure` evidence gate | 1 |
| MAF Workflow | invalid | `tool_service_failure` | 1 |
| MAF Workflow | invalid | misclassified `model_failure` | 5 |

The valid `runner_failure` was seed 505, repetition 0: the commander cited
fewer than two specialist roles. It is a genuine model-quality observation, but
one isolated outcome cannot rescue the cohort's pilot purpose.

## Diagnosis

Each explicit lane begins with six fixed specialist observations. The standard
fault profile deterministically fails every fifth through ninth service call,
depending on the seed. The v0.1 zero-retry adapter therefore guaranteed at
least one transient failure before every lane could finish its initial fan-out.
The resulting zero task-success variance is a contract artifact, not evidence
about relative framework quality or a basis for sample sizing.

The six invalid MAF bundles exposed a separate concurrency race. One specialist
failure escaped while sibling model tasks were still running; those tasks
appended spans after tracing had been finalized. The retained ledgers no longer
matched the trace-link digest, span count, or completeness summary. In five of
those runs, an interleaved model-start event also displaced the global
last-operation marker, so the escaped structured tool failure was labeled
`model_failure`.

## Disposition

ADR 0013 introduces one symmetric application retry only for explicitly
retryable structured failures, meters every attempt, directly classifies escaped
structured tool failures, and cancels and drains MAF fan-out siblings before
finalization. The original artifacts remain preserved as diagnostic evidence.
They must not be included in pilot aggregates or pooled with the replacement
cohort. A clean-source model gate must pass under the new workflow-contract hash
before that replacement cohort starts.

## Replacement result

The replacement cohort ran at clean source `a18c076` after a clean gate. All 30
lanes and all 15 pairs validated. MAF completed 14 lanes and retained one valid
evidence-gate `runner_failure`; LangGraph completed all 15. Task success was
3/15 (20%) for MAF and 5/15 (33.33%) for LangGraph. Both lanes exercised the
new visible retry policy and showed component-level score discrimination.

The offline pilot report is descriptive only. Its seed-level task-success risk
difference was +0.1333 for LangGraph minus MAF, with exact sign p = 0.5 and a
bootstrap interval of [0, 0.2667]. It is pilot evidence for sample sizing, not
the preregistered final inference.

| Replacement artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `50eab85088909de2a95a9101d820bc14803c990ec3224e9223ce306396bc822d` |
| `model-compatibility.json` | `80e2d2153772a63a0e481bf46f9bceed29503e094d0a219cd64b144b85a7e168` |
| resolved execution plan | `a7bb3bff65a6b209c0d8122add1e49f13574fbc4b248d66e2426f96c4515d788` |
| cohort index | `9957ea26e7bb4539ba09def94b19c4890eaf5052485cba7c71cac750c94fd781` |
| paired report JSON | `815e78f4d84fbf1bb78de3452dbf3d91a8c2c7b36dfd21dfb6321b3a89e960d2` |
| evidence index | `aa51808f12ee1985ebacc23618b10c48fde69db2869c4f16153cc00bafa002bc` |
