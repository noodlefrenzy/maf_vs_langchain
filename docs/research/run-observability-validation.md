# Native run-observability validation

Status: validated on a clean, paired Qwen3.5-9B execution on 2026-08-03

This record validates the versioned resource, model, and control-plane ledgers
against a real concurrent native-workflow pair. It is an instrumentation gate,
not a powered framework comparison. The run used the preregistered
`qwen35_9b_task_quality_pilot_v1` profile, case seed 42, randomized order seed
20260805, and clean source commit `6bc7815a60b760efea57328711f66e94e13e602e`.
Both lanes used the same warm Qwen3.5-9B vLLM endpoint, enabled-uncontrolled
prefix cache, 0.5 second resource cadence, RTX 4090, and pinned runtime
attestation.

## Pair result

| Lane | Run ID | Terminal | Task | Score | Task time | Bundle/pair eligibility |
| --- | --- | --- | --- | ---: | ---: | --- |
| MAF workflow | `85d699f8-1d09-45ca-be71-83a8a285cbf0` | completed | success | 0.916667 | 109.028 s | valid / eligible |
| LangGraph | `b50a2c10-8927-49d2-88cd-976ef1340020` | completed | failure | 0.600000 | 87.941 s | valid / eligible |

MAF executed the expected containment and verified recovery. LangGraph
completed normally but did neither, so the pair does not clear the model
promotion rule. This is the reverse of the earlier clean gate at `bb61e34`,
where LangGraph succeeded and MAF failed its evidence gate. Neither one-seed
pair had two successful lanes; Qwen3.5-9B therefore remains a fit-check model,
not the quality-tier pilot model.

## Model-call ledger

Each lane recorded four normalized requests, four responses, and four
`model_call` records. All records had a unique `chatcmpl-*` provider request ID,
provider-reported queue/TTFT/generation timing, reported input/output/cached
usage, and a structured-parse-success outcome. Reasoning-token usage was
explicitly `provider_detail_not_exposed`, not imputed as zero.

| Lane | Requests | Input | Output | Cached input | Queue sum | TTFT sum | Generation sum | Reconciliation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| MAF workflow | 4 | 3,438 | 14,091 | 1,584 | 16.687 s | 0.966 s | 150.879 s | exact |
| LangGraph | 4 | 2,819 | 11,938 | 528 | 27.891 s | 1.168 s | 129.578 s | exact |

The timing sums intentionally exceed task wall time where calls overlap. In
each lane one specialist queued behind the server's frozen two-sequence limit;
the per-request IDs preserve that scheduling fact without assigning concurrent
whole-GPU load to an individual request.

## Resource ledger

Both owner-only `host.jsonl` files contained their configuration record,
fixed-deadline samples, and summary record. Every GPU sample was collected and
every host sample was collected or the documented first-sample CPU partial.

| Lane | Samples | Mean collection overhead | Peak GPU util. | Peak GPU memory | Peak power | Peak temp. | Peak host CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| MAF workflow | 220 | 39.994 ms | 97% | 21,581 MiB | 329.26 W | 68 C | 17.50% |
| LangGraph | 178 | 40.299 ms | 97% | 21,555 MiB | 332.11 W | 71 C | 15.00% |

The deadline scheduler normally began samples at the requested 0.5 second
cadence. Each lane exposed three isolated 1.03--1.05 second collection gaps,
followed by immediate catch-up samples around 0.04 seconds. These correspond
to collection stalls in the external `nvidia-smi` query. The summary preserves
the resulting maximum cadence error (554.348 ms MAF, 548.094 ms LangGraph)
alongside the measured collection overhead instead of allowing each query's
runtime to accumulate into a permanently slower schedule. Resource metrics are
therefore suitable for run-level correlation, but not sub-500-ms attribution.

## Control and native tracing

All seven control records were present and collected for both lanes. Total
control-plane lifetime was 109.794 seconds for MAF and 88.864 seconds for
LangGraph; task execution accounted for 109.028 and 87.941 seconds
respectively. Launch, preparation, finalization, tracing flush, and teardown
remain separately auditable.

Phoenix export succeeded for both lanes. MAF retained 57 spans and LangGraph
64. Both achieved 20/21 semantic-operation coverage (0.952381); the sole
uncovered operation was the already documented absence of a native checkpoint
span in both frameworks.

## Acceptance

The live pair establishes that the neutral observer:

- retains exact per-request provider timing and usage through both SDKs;
- reconciles normalized requests, responses, metrics, terminal calls, and
  terminal output tokens without inference;
- samples host/GPU resources throughout concurrent model work and reports its
  own disturbance and cadence misses;
- separates framework task time from control-plane phases on one monotonic
  clock; and
- fails neither bundle nor pair validation when all invariants hold.

Run directories remain ignored local evidence. This checked-in record preserves
their identities and aggregate audit results without committing prompts,
responses, traces, or machine telemetry.
