# Qwen3.5-27B explicit pilot: diagnostic launch

Status: diagnostic only; replacement gate and pilot required

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
