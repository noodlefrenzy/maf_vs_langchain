# ADR 0009: Use a versioned JSON-lines runner process protocol

Status: Accepted

Date: 2026-08-03

## Context

The root control plane must remain free of framework dependencies while each
runner needs its own locked environment and native instrumentation. Direct
Python imports would violate that isolation and make budgets, approvals,
terminal states, and redaction framework-specific.

## Decision

Communicate with runner subprocesses through the versioned `0.1.0` JSON-lines
protocol documented in `docs/runner-protocol.md`. The control plane owns
process lifecycle, immutable paths, wall-clock enforcement, approvals,
finalization, and the sole persisted terminal record. Runners acknowledge
contract and execution-provenance hashes before execution, emit monotonic usage
and normalized events, and receive secrets only over stdin.

Protocol messages are bounded, run-ID scoped, decoded fail-closed, and drained
during bounded shutdown so legitimate unhappy terminals retain their evidence
tails.

## Consequences

- Framework package graphs and global instrumentation stay isolated.
- Budget and terminal semantics are enforced outside either framework.
- Protocol evolution requires an explicit version and compatibility policy.
- The adapter boundary adds serialization and lifecycle code that must be
  adversarially tested.
