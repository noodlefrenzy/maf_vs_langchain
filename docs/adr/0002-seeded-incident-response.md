# ADR 0002: Use seeded incident response as the primary task

Status: Accepted

Date: 2026-07-31

## Context

The task must require tools and coordination, contain meaningful
nondeterminism, support objective scoring, run locally, and produce a legible
live execution. Live research is hard to reproduce, repository repair can
mostly measure coding ability, and policy/user simulations add another model.

## Decision

Build a local incident-response simulator with:

- multiple causal fault families;
- seeded surface variation and transient tool failures;
- incomplete public tickets and hidden ground truth;
- causal decoys and evidence distributed across role-scoped tools;
- an approval-gated, reversible mitigation;
- direct scoring from diagnosis, evidence, policy, world recovery, and final
  artifact.

Keep retail/user interaction, repository repair, and live research as later
secondary scenarios.

## Consequences

- Scenario generation and scoring are exactly replayable.
- Agents must use MCP and exchange findings.
- The lab can show failure/recovery and HITL behavior in one run.
- Simulator fidelity and tool wording require careful validation.
- Claims remain bounded to an SRE-like investigation workload.
