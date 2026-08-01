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
6. Check the framework-specific [implementation gotchas](docs/gotchas.md)
   before changing an adapter.
7. Record the review in
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
authenticated Streamable HTTP service for isolated paired world clones. The
control plane now supplies a typed runner protocol, isolated run paths,
deterministic lane ordering, outer lifecycle/budget enforcement, native
MAF/LangGraph process handlers, a mock failure matrix, and matched native
MAF/LangGraph explicit workflows through investigation, commander synthesis,
evidence gating, native approval checkpoints, token-bound mitigation, and
recovery verification. Those live explicit workflows now execute through the
normalized runner protocol with pre-dispatch usage, portable model/tool and
approval events, control-plane decisions, and owner-only terminal state. It
also finalizes those executions into the five frozen incident artifacts,
deterministic scores, individually validated bundles, and a persistent paired
experiment with pair-level provenance validation. It does not yet claim a
benchmark result. The next workflow milestone is the matched opinionated
MAF-Harness/Deep-Agents pair and native tracing.
Run bundles can already be checked with `agent-lab bundle validate` and
`agent-lab bundle validate-pair`.

Exercise the full four-lane process boundary without loading a model:

```bash
uv run agent-lab experiment probe \
  --experiment configs/experiments/smoke.toml \
  --runs-dir runs \
  --order-seed 1729
```

This command resolves the profile, creates verified paired world clones,
starts the authenticated loopback MCP service, records lane order, launches
each native runner sequentially, enforces outer limits, and persists protected
world checkpoints and terminal bundles. It is explicitly a protocol probe:
no model or MCP tool is called, no score is produced, and its bundles are not
benchmark evidence.

Run the persistent paired explicit-workflow smoke against the configured local
OpenAI-compatible endpoint with:

```bash
uv run agent-lab experiment run \
  --experiment configs/experiments/explicit-smoke.toml \
  --runs-dir runs \
  --order-seed 1729
```

This invokes the model and MCP tools. It uses the declared scripted `approve`
policy without correcting the model's action, writes scored bundles, validates
each bundle and the pair, and saves a summary index. Summary `status = "ok"`
means execution and provenance succeeded; task success remains a separate
per-run score field.

No license is granted by this private repository unless one is added later.
