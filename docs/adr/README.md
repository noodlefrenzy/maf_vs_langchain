# Architecture decision records

The initial records are **Proposed** until the design review. They document
choices already reflected in the skeleton so review can approve, amend, or
reject them explicitly.

| ADR | Decision |
| --- | --- |
| [0001](0001-match-comparable-layers.md) | Compare explicit workflows and opinionated harnesses as separate tracks |
| [0002](0002-seeded-incident-response.md) | Use a seeded incident-response simulator as the primary task |
| [0003](0003-mcp-capability-boundary.md) | Make MCP the shared tool and role-capability boundary |
| [0004](0004-local-inference.md) | Use one vLLM Chat Completions endpoint and a measured Qwen model tier |
| [0005](0005-normalized-events-and-run-bundles.md) | Preserve native telemetry plus a normalized append-only evidence bundle |
| [0006](0006-neutral-observability.md) | Use Phoenix for native traces and a neutral paired observer |
| [0007](0007-process-and-dependency-isolation.md) | Isolate framework runners and the MCP service in separate projects/processes |

When a record is accepted, implementation changes that reverse it require a
superseding ADR rather than editing history.
