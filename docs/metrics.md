# Run metrics

Control-plane runs write three owner-only JSONL ledgers under `metrics/`. Every
record includes a schema identifier and run ID. The bundle validator checks
identity, structure, monotonicity, and aggregate reconciliation; a malformed or
internally inconsistent ledger makes the bundle ineligible.

## Resource metrics

`host.jsonl` uses schema `agent-lab.resource-metrics/1.0.0`. Its first and last
records freeze the enabled state, cadence, sample count, observed cadence
error, and mean/max collection overhead. Enabled measured profiles use a 0.5 s
cadence. Each intervening sample contains:

- UTC and system-monotonic timestamps plus actual interval and collection cost;
- host CPU utilization and used/total RAM from procfs;
- each NVIDIA device's UUID/index, used/total memory, utilization, power, and
  temperature from `nvidia-smi`;
- `collected`, `partial`, `unavailable`, or `error` status and a reason instead
  of fabricated zeroes.

The whole-device sample is correlated to a run and to model-call monotonic
intervals. It intentionally does not assign concurrent whole-GPU utilization
to an individual request.

## Model metrics

`model.jsonl` uses schema `agent-lab.model-metrics/1.0.0`. One content-free
record is projected for every normalized `model.requested` boundary. A linked
response supplies total call latency and input/output usage. Cached-input and
reasoning tokens are included only when the native SDK reports those fields.
The provider response being accepted by the SDK is the current parser outcome;
it is not a claim that later application-level structured validation passed.

The pinned vLLM response extension reports a provider request ID, queue delay,
TTFT, and generation duration for each non-streaming completion. Both native
clients retain and normalize those exact fields; another provider that omits
them records `null` with `provider_not_reported`. The held-constant profile distinguishes
cold/warm server state and disabled/reset/warm/uncontrolled prefix caching.
Current measured profiles declare a warm loaded server and
`enabled_uncontrolled` cache; that is not a per-request cache-hit claim.
Whole-device thermal state is joined by overlapping monotonic time in
`host.jsonl`.

Provider request IDs correlate timings without serializing concurrent calls or
subtracting aggregate Prometheus histograms. The final summary reconciles request/response and normalized metric counts with
the terminal's model-call/output-token totals. A complete event stream requires
exact output reconciliation; a documented partial stream may only leave the
terminal at or above observed usage.

## Control phases

`control.jsonl` uses schema `agent-lab.control-metrics/1.0.0` and always emits
the same seven records: subprocess launch, runner preparation, task execution,
finalization, tracing flush, teardown, and total control-plane lifetime. A
phase whose boundary was not reached is retained as `unavailable`, not omitted.

Normalized runner events use the same system monotonic clock, so task latency
is independently derived from `run.started` to `run.finished` in the model
summary. This separates framework work from process startup and evidence flush.

## Compute matching

The v0.1 hard budgets cover wall time, model dispatches, tool dispatches, output
tokens, and approval requests. Input, cached-input, and reasoning token counts
are reported efficiency outcomes. They do not participate in the compute gate
because neither framework can bound them symmetrically before a provider
dispatch. Missing usage is never imputed.
