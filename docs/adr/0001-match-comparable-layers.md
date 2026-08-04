# ADR 0001: Match comparable framework layers

Status: Accepted

Date: 2026-07-31

## Context

Microsoft Agent Framework now offers agent primitives, an opinionated Harness,
and explicit Workflows. The LangChain ecosystem offers `create_agent`, Deep
Agents, and LangGraph. A single “MAF vs LangChain” implementation could compare
different abstraction levels and attribute architecture choices to framework
quality.

## Decision

Run two primary tracks:

1. Agent Framework Workflows vs a custom LangGraph implementing the same
   explicit topology.
2. Agent Framework Harness vs Deep Agents with matched domain roles,
   capabilities, budgets, and completion contract.

Include a third matched track for the unrestricted and compute-matched
single-agent baselines: primitive Agent Framework `Agent` vs LangChain
`create_agent`, each with one model-driven agent and the same union discovery
catalog. Report results by track; do not collapse them into one ecosystem
score.

## Consequences

- The comparison answers narrower, more actionable questions.
- Six implementations are required instead of two.
- Framework-native defaults remain visible in the harness track.
- The explicit workflow provides a controlled topology baseline.
- Cross-track differences are descriptive, not evidence of ecosystem
  superiority.
