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
│   └── framework-topology.json
└── metrics/
    ├── model.jsonl
    └── host.jsonl
```

The core writer creates this full directory shape and writes immutable
manifests, events, JSON artifacts, scores, and terminal records. The explicit
MAF and LangGraph workflow slices populate native approval checkpoints and
finalize their terminal workflow state into the five scorer artifacts before
the normalized `run.finished` event. Native-trace and metric producers remain
future integration layers.

## `manifest.json`

Written before execution and never updated. It contains:

- run, experiment, lane, generator version, seed, and repetition identifiers;
- exact model profile;
- budgets;
- source commit, exact source-tree digest, and dependency/image/model digests;
- framework versions;
- effective prompt, tool catalog, and world manifest hashes;
- host/GPU/server fingerprint;
- capture/redaction policy.

Mutable terminal status belongs in `terminal.json`, not the manifest.

## `events.jsonl`

Append-only normalized events in observation order. The schema is implemented
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

## Native evidence

Framework-native traces, graph renderings, state histories, and checkpoint
metadata are preserved by reference or export. Native formats are never
treated as interchangeable. Correlation uses run ID plus trace/span IDs stored
on normalized events.

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

## Validation

`agent-lab bundle validate <run-dir>` checks:

- known schema version;
- immutable-manifest hash;
- contiguous event sequence;
- one start and one terminal event;
- event run/lane IDs match the manifest;
- terminal budgets reconcile with events;
- artifact and native-evidence digests;
- scorer version and ground-truth manifest linkage;
- within-pair held-constant hashes.

`agent-lab bundle validate-pair <left-run-dir> <right-run-dir>` additionally
checks matched lane topology and held-constant prompt, tool, world, model,
budget, source, seed, repetition, and generator provenance. Framework
dependency-lock digests are required but intentionally lane-specific.

Invalid bundles stay available for debugging. The validator writes
`validation.json` or `pair-validation.json` with
`inference_status = "invalid_provenance"` and returns a nonzero CLI status.
