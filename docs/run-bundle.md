# Run-bundle format

Each execution writes an immutable directory under `runs/<run-id>/`.

```text
<run-id>/
├── manifest.json
├── manifest.sha256
├── events.jsonl
├── scores.json
├── terminal.json
├── artifacts/
│   ├── incident-report.json
│   ├── evidence-ledger.json
│   ├── command-audit.json
│   ├── evaluation-state.json
│   └── world-manifest.json
├── checkpoints/
│   └── index.json
├── native/
│   ├── trace-link.json
│   ├── observer-pair.json
│   ├── tracing-profile.json
│   ├── tracing-profile.sha256
│   ├── traces/
│   │   └── spans.jsonl
│   └── framework-topology.json
└── metrics/
    ├── model.jsonl
    ├── host.jsonl
    └── control.jsonl
```

Interactive development runs may also contain owner-only
`control/approval-<request-digest>.json` and
`control/approval-api-audit.jsonl`. A timed-out handler also closes the inbox
with owner-only `control/approval-closed.json` before terminal teardown, so a
late decision cannot be accepted. The credential itself is never written to a
bundle.

The core writer creates this full directory shape and writes immutable
manifests, events, JSON artifacts, scores, terminal records, and three
versioned metric ledgers. The explicit
MAF and LangGraph workflow slices populate native approval checkpoints and
finalize their terminal workflow state into the five scorer artifacts before
the normalized `run.finished` event. Native MAF and OpenInference/LangChain
spans are also projected into a redacted local ledger and exported to a
loopback Phoenix collector when it is reachable. Each completed model call
emits a content-free `metric.recorded` event and `metrics/model.jsonl` preserves
input/output plus provider-reported cached-input/reasoning token counts without
turning absent detail into zero. `metrics/host.jsonl` samples host CPU/RAM and
GPU utilization/memory/power/temperature on the profile's held-constant
cadence. `metrics/control.jsonl` separates launch, preparation, task,
finalization, tracing flush, and teardown. See [metrics.md](metrics.md).

## `manifest.json`

Written before execution and never updated. It contains:

- run, experiment, lane, generator version, seed, and repetition identifiers;
- exact model profile;
- budgets;
- source commit, exact source-tree digest, and dependency/image/model digests;
- framework versions;
- effective prompt, tool catalog, and world manifest hashes;
- host/GPU/server fingerprint;

The separately hashed `native/tracing-profile.json` records the tracing
endpoint, timeout, and capture/redaction policy before runner launch.

`native/observer-pair.json` is a non-evidentiary live-discovery sidecar. Both
paired bundles receive the same opaque pair ID and run IDs before the first
ordered lane starts; the final experiment index remains authoritative for
replay and inference.

Mutable terminal status belongs in `terminal.json`, not the manifest.

## `events.jsonl`

Append-only normalized events in observation order. Every control-plane runner
event carries both UTC observation time and a system-monotonic timestamp. The
schema is implemented
by `agent_lab.contracts.NormalizedEvent`.

Core event families:

- run lifecycle;
- agent lifecycle and handoff/delegation;
- model request/response and public text deltas;
- tool request/start/result/failure;
- state update and checkpoint;
- approval request/decision;
- artifacts, metrics, and errors.

Payloads are deliberately event-specific. Any field needed for scoring or a
headline measure must be promoted into a documented, versioned payload
contract before the measured study.

The current adapters use `model.requested` and `model.responded` as structural
message boundaries without retaining message text. Because native task calls
are non-streaming, they do not synthesize `model.text_delta` events. Explicit
workflows emit handoffs when specialist execution begins and when a validated
finding returns to the commander. Harness lanes emit delegation when a native
specialist first reaches its role-scoped MCP boundary and emit the return only
for findings that survive provenance validation. Agent, model, tool, metric,
and handoff payloads carry a deterministic `<lane>/<actor>` namespace; this
preserves nested subagent identity without guessing unavailable native span
namespaces.

## Native evidence

Framework-native traces, graph renderings, state histories, and checkpoint
metadata are preserved by reference or export. Native formats are never
treated as interchangeable. Correlation uses run ID plus trace/span IDs stored
on normalized events.

`native/trace-link.json` records the local ledger digest, all observed trace
IDs, the primary Phoenix deep link, collector status, and an operation-level
completeness matrix. `native/traces/spans.jsonl` is not a raw OTLP dump: it is a
content-free structural projection with span identity, timing, status,
instrumentation scope, operation family, and a small attribute allowlist.
Prompts, messages, tool arguments, and tool results are excluded.
Concurrent local span exports are serialized, and `trace-link.json` is written
through an owner-only temporary file plus atomic rename before the runner's
terminal protocol message. Validators therefore never observe a partially
written trace link as a completed runner outcome.
`native/tracing-profile.json` and its digest keep the held-constant tracing and
capture inputs immutable without changing the frozen v0.1 run-manifest schema.

Phoenix is auxiliary. `unavailable` and `export_failed` are warnings, not run
failures, because the local ledger and normalized evidence remain available.
Tracing can be disabled explicitly for overhead ablations; such a run records
`collector_status = "disabled"`, no trace IDs, and the resulting zero native
coverage instead of silently omitting the measurement.

## Privacy

Default bundles do not include:

- raw chain of thought or provider reasoning content;
- secrets, authorization headers, or raw environment variables;
- unredacted sensitive tool inputs/results;
- hidden world ground truth before the evaluator stage;
- full model prompts unless an explicit sensitive-capture development profile
  is used.

Hashes and structural metadata make it possible to compare effective inputs
without retaining sensitive content.

## `terminal.json` and unhappy-path bundles

`terminal.json` is written exactly once by the control plane. In addition to
the authoritative status, reason, elapsed time, and usage counters, it records
two lifecycle facts:

- `execution_started` says whether the control plane successfully dispatched
  `start`; and
- `event_stream_complete` says whether it observed the runner's terminal
  message after draining the bounded shutdown tail.

The legal shapes for the nine terminal statuses are:

| Status | Normalized event contract | Usage and artifact contract |
| --- | --- | --- |
| `completed` | Execution started; exactly one `run.started` and final `run.finished`; complete stream | Event-derived usage must equal terminal usage; all scorer artifacts and scores are required |
| `approval_rejected` | Same complete lifecycle as `completed` | Exact usage; partial task artifacts may be declared but are not scored as completed work |
| `model_failure`, `tool_service_failure` | Started, complete lifecycle ending in the matching status | Exact usage through the failed boundary; no completed-run scorer artifacts required |
| `budget_exhausted` | Normally complete after control-plane cancellation and drain; a forced exit may be explicitly incomplete | Terminal counters are exact when complete and upper bounds over observed events when incomplete |
| `approval_timeout` | Same as `budget_exhausted`, with the approval deadline as the authoritative wall-clock terminal | The expected wall-clock overage is legal only for this status; partial evidence stays in the denominator |
| `cancelled` | Complete when the runner acknowledges cancellation; explicitly incomplete after forced termination | Exact or bounded-partial usage according to `event_stream_complete` |
| `runner_failure` | May be pre-start, complete after a classified runner failure, or explicitly incomplete after crash/forced termination | Pre-start usage must be zero; observed partial usage cannot exceed terminal counters |
| `invalid_provenance` | May be pre-start or partial, but remains explicitly ineligible for comparative inference | Evidence is retained for diagnosis and never promoted to a completed score |

An incomplete stream still requires contiguous identities for every event that
was observed. It may omit the runner's final lifecycle events, and terminal
usage may exceed event-derived usage, but it may never under-report observed
requests or tokens. Files preserved without an observed `artifact.written`
event are retained as unattested partial artifacts and excluded from scoring.

## Validation

`agent-lab bundle validate <run-dir>` checks:

- known schema version;
- immutable-manifest hash;
- contiguous event sequence;
- unique event IDs and parent links that resolve to earlier events;
- the status-specific start/finish and bounded partial-stream contract;
- event run/lane IDs match the manifest;
- terminal budgets reconcile with events;
- metric identities, schemas, monotonic clocks, sampling counts/overhead, phase
  intervals, and token aggregates reconcile with the terminal/event stream;
- artifact and native-evidence digests;

`agent-lab report build --index <native-index> ...` is read-only over source
evidence. It re-runs individual and pair validation, refuses protocol-probe or
reused bundles, reconstructs left/right order from the frozen pair preflight,
and writes a new owner-only output directory exclusively. Operational failures
remain in task-success and reliability denominators; score effects use only
pairs with two valid scorer outputs and report that analyzed-pair count
explicitly. Other paired effects cover task/control latency, model/tool calls,
reported token dimensions, provider timing, client concurrency, host/GPU
metrics, and native trace coverage, retaining explicit missingness.

The report output contains portable relative links, redacted experiment/run
evidence, fresh validation results, and a full `private-runs/` copy. A versioned
`evidence-index.json` records every retained and private file's SHA-256, and its
sidecar authenticates the index. See [evidence-retention.md](evidence-retention.md)
for the publication boundary and durable-storage procedure.
- strict lexical artifact paths (one JSON filename directly under
  `artifacts/`, with no traversal or nested path segments);
- trace-link identity, local span-ledger digest, event correlation, and
  recomputed completeness;
- scorer version and ground-truth manifest linkage, followed by deterministic
  score recomputation from the validated incident, report, audit, and terminal
  world state;
- within-pair held-constant hashes.

`agent-lab bundle validate-pair <left-run-dir> <right-run-dir>` additionally
checks matched lane topology and held-constant prompt, tool, world, model,
effective request-policy, execution-runtime, budget, source, seed, repetition,
and generator provenance. Framework
dependency-lock digests are required but intentionally lane-specific.

Invalid bundles stay available for debugging. The validator writes
`validation.json` or `pair-validation.json` with
`inference_status = "invalid_provenance"` and returns a nonzero CLI status.
