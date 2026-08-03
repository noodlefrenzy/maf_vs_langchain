# Report evidence retention

The offline report is also the transfer unit for durable study evidence. A
successful `agent-lab report build` creates a new owner-only directory and
refuses to reuse or overwrite an existing destination. Move that directory to
the study's access-controlled durable storage before deleting local run state.
The repository intentionally does not choose or authenticate a cloud bucket,
network share, or archival service on the operator's behalf.

## Archive layout

```text
report-output/
├── paired-report.json
├── paired-report.html
├── evidence-index.json
├── evidence-index.sha256
├── evidence/
│   ├── indices/<source-index-sha256>.json
│   ├── pairs/<pair-key-sha256>.json
│   └── runs/<run-id>/
│       ├── manifest.json
│       ├── terminal.json
│       ├── scores.json
│       ├── validation.json
│       ├── metrics/*.jsonl
│       └── native/<preflight, trace link, tracing profile>
└── private-runs/<run-id>/...
```

`paired-report.json` uses `paired-report/v2`. Its source-index and bundle links
are relative to the report directory, so moving the archive does not break
them or disclose the workstation path. The redacted experiment indices retain
case, ordering, contract, status, score, and trace-summary fields while
replacing run-directory paths with archive-relative paths.

The `evidence/runs/` projection contains the exact provenance, validation,
score, and content-free metric files needed to audit the reported aggregates.
The separately generated pair-validation files use the same portable paths.
Every retained file is mode 0600 and the archive root is mode 0700.

## Private material

`private-runs/` is a byte-for-byte owner-only copy of each source bundle. It can
contain prompts, model outputs, incident artifacts, checkpoints, normalized
events, structural spans, host identifiers, and detailed machine telemetry.
Never publish this directory. The redacted projection also contains host and
runtime identifiers in manifests and metrics, so review it before disclosure.

`evidence-index.json` separates the redacted and private paths and records the
size and SHA-256 of every source-bundle file. An auditor can recompute a private
file's digest without requiring it to be made public. The index also hashes
the report files, redacted source indices, pair validations, and retained run
projection. `evidence-index.sha256` authenticates the index itself.

## Retention procedure

1. Build the report directly on, or copy the complete new directory to, the
   approved access-controlled storage location.
2. Verify `evidence-index.sha256`, then verify a sample of public/redacted and
   private file hashes from `evidence-index.json`.
3. Record the durable object/version identifier in the study decision record.
4. Publish only reviewed report/index files. Keep `private-runs/` restricted.
5. Delete workstation run state only after the archived copy and restoration
   procedure have both been tested.

The report builder validates every source bundle and pair once while building
the analysis and again immediately before the snapshot. If evidence changes
between those boundaries, export fails instead of retaining a mixed-time
archive.
