# Architecture decision records

The initial records were accepted by the 2026-07-31 design review. Later
implementation decisions are recorded separately; reversing an accepted
decision requires a superseding ADR rather than rewriting its history.

| ADR | Decision |
| --- | --- |
| [0001](0001-match-comparable-layers.md) | Compare explicit workflows and opinionated harnesses as separate tracks |
| [0002](0002-seeded-incident-response.md) | Use a seeded incident-response simulator as the primary task |
| [0003](0003-mcp-capability-boundary.md) | Make MCP the shared tool and role-capability boundary |
| [0004](0004-local-inference.md) | Use one vLLM Chat Completions endpoint and a measured Qwen model tier |
| [0005](0005-normalized-events-and-run-bundles.md) | Preserve native telemetry plus a normalized append-only evidence bundle |
| [0006](0006-neutral-observability.md) | Use Phoenix for native traces and a neutral paired observer |
| [0007](0007-process-and-dependency-isolation.md) | Isolate framework runners and the MCP service in separate projects/processes |
| [0008](0008-separate-fit-and-development-models.md) | Separate the local fit-check runtime from the provisional development-smoke runtime |
| [0009](0009-runner-process-protocol.md) | Use a versioned JSON-lines runner process protocol |
| [0010](0010-symmetric-harness-contract.md) | Freeze a symmetric two-phase harness adapter contract |
| [0011](0011-cosmos-checkpoint-backend.md) | Select Cosmos DB for production-intended workflow checkpoints |
| [0012](0012-promote-qwen35-27b-llamacpp.md) | Promote the pinned Qwen3.5-27B Q4_K_M llama.cpp operating point to measured pilots |
| [0013](0013-bounded-explicit-tool-retry.md) | Apply one symmetric retry to explicitly retryable structured tool failures |
| [0014](0014-bound-native-harness-subagent-lifecycle.md) | Enforce native specialist phase limits and settle all subagent work before harness phase exit |
| [0015](0015-make-harness-bounds-and-provenance-actionable.md) | Expose bounded completion and exact role provenance in the shared harness prompts |
| [0016](0016-enforce-harness-specialist-completion.md) | Enforce the specialist completion turn and bound MAF background-result retrieval |

ADR 0012 supersedes ADR 0008 for measured-pilot runtime selection while
preserving the earlier fit-check and development-smoke evidence as separate
historical cohorts.
