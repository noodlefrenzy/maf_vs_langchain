# ADR 0008: Separate fit-check and development-smoke model runtimes

Status: Accepted

Date: 2026-08-03

Supersedes: ADR 0004

## Context

ADR 0004 selected a local vLLM Chat Completions endpoint and Qwen3.5-9B as the
first hardware and compatibility gate. During adapter stabilization, an
already-cached Qwen3-Next 80B A3B GGUF served by llama.cpp on `lonestar` was
also used through a loopback SSH tunnel. That second runtime exposed useful
model-behavior and harness-integration failures, but its use was not recorded
as a decision and could be mistaken for final model selection.

Qwen3.5-9B has now completed reproducible 8K, 16K, and 32K vLLM fit tests and
the full native-client compatibility gate.

## Outcome update

The corrected one-seed task-quality gate at source `bb61e34` produced a
successful LangGraph lane and a valid MAF evidence-gate failure. Qwen3.5-9B
therefore remains the accepted fit-check model but is not promoted as the
quality-tier pilot model. The larger profiles remain gated pending another
quality-tier candidate.

## Decision

Keep two explicitly labeled runtime roles:

- Qwen3.5-9B on the pinned vLLM/BitsAndBytes stack is the accepted local
  fit-check candidate and the next task-quality pilot profile.
- Qwen3-Next 80B A3B GGUF on remote llama.cpp is a provisional
  development-smoke profile only. It may diagnose adapters but does not replace
  the fit check or establish the final measured-study model.

Every pair still uses one identical model profile and endpoint for both lanes.
Final selection occurs only after a preregistered task-quality/operating-point
pilot. Runtime and model cohorts must never be pooled.

## Consequences

- Existing llama.cpp stabilization evidence remains useful but exploratory.
- The completed 4090 characterization satisfies ADR 0004's fit requirement.
- Model-serving implementation is an explicit cohort variable.
- A measured-study model cannot be inferred from whichever endpoint is most
  convenient during development.
