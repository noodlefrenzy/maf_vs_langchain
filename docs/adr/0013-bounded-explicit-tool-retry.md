# ADR 0013: Bound explicit-workflow structured tool retries

Status: Accepted

Date: 2026-08-03

## Context

The first five-seed Qwen3.5-27B explicit-workflow pilot reached all 30 planned
lanes at clean source `23617ff`, but it could not estimate task variance. The
standard simulator fault profile injects a transient failure every five to nine
tool calls, while each explicit lane begins with six fixed specialist
observations. Under the frozen zero-retry contract, every lane was therefore
guaranteed to terminate before synthesis. The launch was useful as a diagnostic
but is not an acceptable pilot cohort.

The same cohort exposed a MAF cancellation defect. When one parallel specialist
raised the transient failure, sibling model tasks could continue after workflow
termination and trace finalization. Six bundles changed their span ledgers after
the trace link was written, invalidating span digests, counts, and completeness.
Interleaved sibling model events also caused five structured tool failures to be
classified as model failures by state-based inference.

## Decision

Apply one application-level retry, in both explicit-workflow adapters, only when
the normalized failure is a `WorkflowToolFailure` with `retryable = true`.
Non-retryable structured failures, unstructured transport failures, and model
failures receive no application retry. Count and trace each dispatch separately;
the external service audit and budget remain authoritative.

Keep MAF's native connection-loss reconnect as separately preregistered
framework behavior. It is not one of the shared application attempts and any
replay reaching the service remains visible in the service audit.

When an exception escapes the initial MAF fan-out, cancel and drain every active
sibling investigation before allowing the workflow error to leave the runner.
Classify an escaped `WorkflowToolFailure` directly as `tool_service_failure`
rather than inferring its type from whichever concurrent operation most
recently updated telemetry state.

## Consequences

- The workflow contract hash and source commit change; the cohort collected at
  `23617ff` is diagnostic-only and cannot be pooled with later results.
- The exact Qwen3.5-27B quality gate must pass again at clean source before a
  replacement explicit pilot begins.
- A transient that also fails its second application attempt remains a visible
  `tool_service_failure` in the denominator.
- Retry dispatches consume the same tool-call budget and appear in normalized
  events, role statistics, native traces, and the MCP audit stream.
- The explicit workflow still has no model correction loop or
  whole-investigation retry.
