# Native tracing fixed-cost ablation

Date: 2026-08-01

This ablation measures the fixed runner-lifecycle cost of enabling the native
OpenTelemetry path. It is a smoke measurement for the tracing integration, not
an estimate of tracing overhead during a model-driven incident run.

## Method

- Compared `configs/experiments/explicit-smoke.toml` with the matched
  `configs/experiments/explicit-tracing-disabled-smoke.toml` profile.
- Ran 12 paired repetitions of each condition, alternating which condition ran
  first, for 48 total runner launches.
- Used `agent-lab experiment probe`, so each runner performed prepare, tracing
  setup or explicit disable, one manual run span when enabled, terminal flush,
  and shutdown. It made no model or MCP calls.
- Read `elapsed_seconds` from each immutable `terminal.json`; the measure
  includes process launch and protocol setup in both conditions.
- Phoenix 19.10.0 was live on loopback. All 24 traced runs reported
  `collector_status = "exported"` and one local span; all 24 disabled runs
  reported `disabled` and zero spans.
- Condition order alternated by trial; order seeds `2026080101` through
  `2026080112` also varied native lane order.
- Raw per-run values are committed in
  `docs/research/data/native-tracing-overhead-2026-08-01.csv`.

Environment and provenance:

- source commit: `708d922e93f9379d744cf0a91927284b87752217`;
- source-tree digest:
  `08b3b62075b7327eb51136f3d89613678de832d61ba2906cdbd5041161eaf728`;
- WSL2 Linux 6.18.35.2, Python 3.12.12;
- MAF core 1.12.1;
- LangChain 1.3.14, LangGraph 1.2.10, OpenInference LangChain 0.1.68;
- OpenTelemetry SDK/exporter 1.44.0;
- Phoenix image digest
  `sha256:3092f5543a3ddd35db7390cf971027c33be6be1f171274d57f3c8658c2193d67`.

## Results

| Runner | Disabled median | Enabled median | Median paired delta | Mean paired delta |
| --- | ---: | ---: | ---: | ---: |
| MAF Workflows | 255.1 ms | 364.7 ms | +106.8 ms | +105.3 ms |
| LangGraph | 243.8 ms | 778.2 ms | +528.4 ms | +536.4 ms |

The paired deltas ranged from 58.9 to 128.2 ms for MAF and from 511.7 to
569.9 ms for LangGraph. The larger LangGraph fixed cost is consistent with the
OpenInference path importing and patching LangChain's callback manager during
each isolated process launch, but this is an inference from the implementation,
not a component-level timing result.

## Interpretation limits

Percentages are intentionally omitted. The disabled probes last only about a
quarter second and do no useful agent work, so percentages would exaggerate a
mostly fixed startup cost. This ablation also does not measure per-span cost,
the volume of framework-native spans, model latency, tool latency, or resource
sampling overhead.

A measured-study preflight should repeat the same tracing on/off comparison on
representative completed native workflows and report absolute paired deltas.
The matched Qwen3.5-9B profiles
`qwen35-9b-explicit-tracing-enabled-ablation.toml` and
`qwen35-9b-explicit-tracing-disabled-ablation.toml` freeze that follow-up. They
differ only in experiment identity and the tracing-enabled bit; both retain the
same 0.5-second resource observer so sampling disturbance is held constant.
Alternate whole-pair condition order, vary the lane-order seed, and keep task,
provider, resource, control-phase, and native-span measures separate. The
model-driven result is recorded below only after clean-source execution.

## Representative-workflow follow-up

The follow-up ran on 2026-08-03 against the pinned Qwen3.5-9B vLLM service.
Four matched blocks produced eight whole pairs and 16 native runner launches.
Within each block the enabled and disabled conditions used the same lane order;
condition order alternated between blocks, and each lane occupied each pair
position twice per condition. The `20260806xx` order seeds are identifiers, not
collection dates.

Both conditions used seed 42, the same retry-storm case, model request policy,
warm inference server, uncontrolled prefix cache, budgets, and 0.5-second
resource sampler. The profiles differed only in experiment identity and the
tracing-enabled bit. The source tree was clean at commit
`1e858613eca1f0b36146e8b6b304584f4dfd2b18`, with tree digest
`54ef7384eaae836149ac3083357ac3b4c0beb48dfd0408cc7afcb813f9a3198c`.
The model revision was `c202236235762e1c871ad0ccb60c8ee5ba337b9a`
and the vLLM image manifest digest was
`sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9`.

Every bundle and paired comparison passed provenance validation and remained
inference-eligible. All eight enabled launches exported a native trace: MAF
recorded 57 spans per launch and LangGraph 64, both at 0.952381 completeness.
All eight disabled launches recorded `collector_status = "disabled"`, zero
spans, and zero trace completeness. Fifteen launches completed. One disabled
MAF launch ended in a valid `runner_failure` because its incident commander
rejected the available evidence; it is retained as an operational outcome and
was not removed from the timing analysis.

### Measures

- `model_union_seconds` is the union of model-call intervals, not their sum;
  three specialist calls overlap in this workflow.
- `task_non_model_seconds` is task latency minus that union.
- `control_overhead_seconds` is control-plane total minus task latency and
  includes process/protocol setup, finalization, trace flush, and teardown.
- Every delta below is enabled minus disabled within a matched block. Medians
  are medians of the four paired deltas. Ranges show all four observations.

Raw aggregate measurements and run identities are committed in
`docs/research/data/native-tracing-full-workflow-2026-08-03.csv`.

### Results

| Measure | MAF median paired delta (range) | LangGraph median paired delta (range) |
| --- | ---: | ---: |
| End-to-end elapsed | +0.418 s (-51.096 to +40.665) | +0.622 s (-29.636 to +6.443) |
| Task latency | +0.231 s (-51.215 to +40.572) | +0.198 s (-30.181 to +6.048) |
| Model-call union | +0.218 s (-51.536 to +40.557) | +0.570 s (-29.899 to +6.474) |
| Task time outside model calls | +0.024 s (-0.007 to +0.321) | -0.371 s (-0.426 to -0.282) |
| Total control overhead | +0.134 s (+0.094 to +0.225) | +0.423 s (+0.395 to +0.545) |
| Explicit trace flush | +0.0061 s (+0.0058 to +0.0077) | +0.0073 s (+0.0065 to +0.0089) |
| Provider generation per output token | +0.0048 ms (-0.4771 to +0.4562) | +0.1069 ms (-0.1364 to +0.1641) |
| Resource-sampling call cost | +0.247 ms (-1.374 to +2.075) | -0.036 ms (-0.406 to +0.120) |
| Peak GPU memory | -25 MiB (-146 to +35) | +32.5 MiB (-27 to +90) |
| Peak GPU power | -1.90 W (-7.54 to +1.70) | -1.00 W (-3.53 to +1.11) |

The end-to-end and task-latency ranges are dominated by stochastic model work:
output totals varied from 11,236 to 15,988 tokens for MAF and 11,860 to 17,106
for LangGraph, with correspondingly large provider-generation and queue-time
variation. Those raw elapsed deltas do not isolate tracing cost. Subtracting
the model-call union removes most of that variance, but the opposite-signed
MAF and LangGraph task residuals show that four blocks are still too small to
estimate a per-span task-execution effect.

The directly observed result is the control-path fixed cost. Total control
overhead increased in all eight lane-level comparisons. Its median increase,
134 ms for MAF and 423 ms for LangGraph, is consistent in magnitude with the
earlier no-model startup medians of 107 ms and 528 ms. Only about 6--7 ms was
spent in the explicit terminal trace flush; the balance is lifecycle setup and
teardown. GPU memory, GPU power, provider generation per token, and the
unchanged resource observer show no material tracing-related shift at this
sample size.

### Decision and limits

Keep native tracing enabled for measured pilots. On these roughly 82--143
second workflows it provided framework-native span evidence with a stable
sub-second control-path cost, while no material GPU or provider-throughput cost
was detected. Report trace completeness and control overhead separately from
task latency, and never subtract the fixed cost from raw evidence after the
fact.

This is an instrumentation fit check, not a precise throughput benchmark. It
uses one 9B model, one task instance, four matched blocks, sequential lane
execution, and an uncontrolled prefix cache. It does not establish per-span
cost, concurrent tracing capacity, 35B behavior, or a causal task-execution
speedup for LangGraph. A larger study should preserve the same matched block
design and size repetitions from the observed control-overhead variance.
