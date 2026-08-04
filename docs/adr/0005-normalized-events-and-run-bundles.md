# ADR 0005: Add normalized events without replacing native telemetry

Status: Accepted

Date: 2026-07-31

## Context

MAF and LangGraph expose different event and trace models. Using only one
framework's concepts would bias the live UI and evaluator. Reducing everything
to a lowest-common-denominator trace would discard meaningful operability
differences.

## Decision

Preserve both:

- native framework events, checkpoints, graph/state views, and OTLP spans for
  framework-specific debugging;
- a small, versioned `NormalizedEvent` stream for paired rendering, budget
  enforcement, deterministic scoring, and derived measures.

Persist immutable provenance, append-only JSONL events, artifacts, metrics,
terminal state, native-evidence references, and scores as one run bundle.
Prohibit raw reasoning capture in schema v0.1.

## Consequences

- A neutral live/replay UI becomes possible.
- Native observability quality remains measurable.
- Adapters add code and can lose information, so trace-completeness tests are
  required.
- Schema and redaction policy must be frozen before measured runs.
- Completed bundles support offline reports without live services.
