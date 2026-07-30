# ADR 0006: Use Phoenix plus a neutral paired observer

Status: Proposed

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

Start with a browser UI backed by FastAPI/SSE; provide a terminal renderer over
the same API later. Link each run to Phoenix for native drilldown.

## Consequences

- Data and traces remain local.
- Both frameworks use one trace store.
- Side-by-side semantics do not depend on native span shape.
- Phoenix and instrumentation versions become recorded experiment inputs.
- Browser UI implementation adds work but offers clearer synchronized
  timelines than either framework's own console.
- Phoenix availability cannot be allowed to stop a run.
