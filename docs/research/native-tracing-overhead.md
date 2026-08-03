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
