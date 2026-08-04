# ADR 0006: Use Phoenix plus a neutral paired observer

Status: Accepted

Date: 2026-07-31

## Context

MAF has native OpenTelemetry support. LangGraph/Deep Agents have rich event
streams and LangSmith integrations. A first comparison should remain local and
not require either vendor's hosted observability product, while still showing
both systems live and post-hoc.

## Decision

Self-host Phoenix on loopback as the common OTLP trace collector and native
trace UI. Send MAF native spans directly and instrument LangChain with
OpenInference. Build a separate framework-neutral observer over normalized
events for two-column live comparison and replay.

Use a browser UI backed by FastAPI/SSE plus a headless Rich renderer over the
same sanitized API. Link each run to Phoenix for native drilldown. Replay reads
the same append-only bundles as live tailing, and a requested event sequence is
the reconnect cursor.

Interactive approval controls remain loopback-only, bearer-authenticated, and
separate from read access. The API persists an owner-only audited decision for
the neutral control plane; unattended comparison profiles continue to freeze a
scripted approval policy.

Every runner also writes a redacted structural span ledger and a trace-link
sidecar into its run bundle. This makes completeness and correlation auditable
without making Phoenix availability part of task execution. Collector export
is attempted only after a bounded loopback reachability check, and capture of
prompts, outputs, tool arguments, and results is disabled by default.

## Consequences

- Data and traces remain local.
- Both frameworks use one trace store.
- Side-by-side semantics do not depend on native span shape.
- Phoenix and instrumentation versions become recorded experiment inputs.
- Browser and terminal renderers share one structural, content-redacted view,
  avoiding renderer-specific semantics.
- Phoenix availability cannot be allowed to stop a run.
