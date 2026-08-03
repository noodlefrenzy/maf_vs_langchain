# ADR 0014: Bound native harness subagent lifecycle

Status: Accepted

Date: 2026-08-03

## Context

The first Qwen3.5-27B matched-harness pilot launch at clean source `a18c076`
exposed two adapter defects before a complete cohort was collected.

Deep Agents applied the frozen six-call phase limit to the incident commander
graphs but not to the raw `SubAgent` specifications. One telemetry specialist
therefore continued for 25 model requests; the lane stopped only at the outer
run budget, after 30 model calls and all 64 tool calls. The lane was valid, but
the execution did not honor the matched phase contract.

MAF's background-agent tools returned control while two native specialist
tasks were still running. The adapter evaluated its two-specialist MCP gate,
raised, and finalized tracing before those tasks finished. Each of the two MAF
attempts appended four spans after the trace link was written, producing invalid
span digests, counts, and completeness summaries.

The first launch was stopped after these repeatable structural failures. Its
artifacts are diagnostic evidence, not a pilot cohort.

## Decision

Apply a fresh `ModelCallLimitMiddleware` with the shared six-dispatch ceiling
inside every Deep Agents specialist specification as well as on the commander
delegation and synthesis graphs.

Make subagent settlement an explicit shared harness-contract requirement. A
normal phase does not evaluate the specialist gate until every started native
specialist task has settled. An exceptional phase cancels and drains all
started specialist work before trace finalization.

For Deep Agents, track native `task` tool executions around the parent graph so
parallel sibling tasks can be canceled and reaped when one task fails. For MAF,
retain the native background-agent task handles, use the framework's background
completion loop, and drain them before observing role activity. Do not inject
specialist answers or repair model output in the adapter; retrieval and use of
native results remain observable model/framework behavior.

MAF's native completion loop can perform more than one internal agent run. Add
a pre-dispatch reservation around each adapter phase so all loop iterations
together retain the shared six-call ceiling. Start a fresh reservation for each
shared delegation repair, synthesis, or provenance-correction invocation, which
matches the fresh Deep Agents graph invocation at those boundaries.

## Consequences

- The harness contract and aggregate contract-bundle hashes change. Earlier
  harness bundles cannot be pooled with the replacement pilot.
- A specialist that reaches its sixth model response may finish; dispatch seven
  is rejected before reaching the provider.
- Native background or parallel task failures remain visible, but cannot mutate
  a bundle after terminal trace finalization.
- The exact Qwen3.5-27B gate must pass at a clean source commit before the
  replacement harness pilot begins.
- Hermetic runner probes now exercise the Deep Agents specialist ceiling, both
  frameworks' exceptional task drain, and MAF's aggregate phase reservation.
