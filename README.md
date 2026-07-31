# Agent Framework Field Lab

This repository is a controlled field lab for comparing Microsoft Agent
Framework with the LangChain ecosystem while holding the model, task, tools,
budgets, and evaluation rules constant.

The comparison has two matched lanes:

| Question | Microsoft lane | LangChain lane |
| --- | --- | --- |
| How do explicit workflow runtimes compare? | Agent Framework Workflows | Custom LangGraph |
| How do opinionated long-horizon harnesses compare? | Agent Framework Harness | Deep Agents |

The primary scenario is a seeded incident-response simulation. Specialized
agents receive role-scoped MCP tools, investigate hidden state, tolerate
transient tool failures, coordinate a diagnosis, and request approval for a
mitigation. Each paired run gets an isolated copy of the same world.

## Start here

1. Read the [experiment specification](docs/SPEC.md).
2. Review the [architecture](docs/architecture.md) and
   [comparison protocol](docs/comparison-protocol.md).
3. Inspect the [frozen v0.1 contract bundle](docs/contracts-v0.1.md).
4. Read the [research summary](docs/research/framework-landscape.md) and
   [task selection](docs/research/task-options.md).
5. Review the decisions in [docs/adr](docs/adr/README.md).
6. Record the review in
   [issue #10](https://github.com/noodlefrenzy/maf_vs_langchain/issues/10)
   and follow implementation on the
   [project board](https://github.com/users/noodlefrenzy/projects/21).

The core skeleton is runnable without either agent framework or a downloaded
model:

```bash
uv sync
uv run agent-lab scenario generate --seed 42
uv run agent-lab config validate configs/experiments/smoke.toml
uv run pytest
```

Framework runners and the MCP service intentionally use separate projects:

```text
runners/maf/          Microsoft Workflows and Harness adapters
runners/langgraph/    LangGraph and Deep Agents adapters
services/incident_mcp Seeded incident world exposed through MCP
```

This separation is part of the experiment design, not ceremony: framework
instrumentation and MCP SDK versions can conflict, and every lane must be
observable and restartable as its own process.

## Current status

The v0.1 design is approved and implementation is underway. The repository now
contains the frozen shared prompt/tool/schema bundle, a stateful and auditable
incident simulator, approval-gated mitigation and recovery semantics, the
deterministic scorer, a scripted-oracle end-to-end run-bundle path, and the
authenticated Streamable HTTP service for isolated paired world clones. It
does not yet claim a benchmark result. The next implementation milestones are
the control plane and matched explicit-workflow adapters. Run bundles can
already be checked with `agent-lab bundle validate` and
`agent-lab bundle validate-pair`.

No license is granted by this private repository unless one is added later.
