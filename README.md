# Agent Framework Field Lab

This repository is a controlled field lab for comparing Microsoft Agent
Framework with the LangChain ecosystem while holding the model, task, tools,
budgets, and evaluation rules constant.

The comparison has three matched tracks:

| Question | Microsoft lane | LangChain lane |
| --- | --- | --- |
| How do explicit workflow runtimes compare? | Agent Framework Workflows | Custom LangGraph |
| How do opinionated long-horizon harnesses compare? | Agent Framework Harness | Deep Agents |
| Does role separation add value over one agent? | Agent Framework `Agent` | LangChain `create_agent` |

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

Dependency changes must use the rolling 72-hour release quarantine documented
in [`docs/dependency-quarantine.md`](docs/dependency-quarantine.md). The
project-owned lock command covers all four isolated uv projects and CI verifies
the policy before installing repository dependencies.

Framework runners and the MCP service intentionally use separate projects:

```text
runners/maf/          Microsoft Workflows, Harness, and Agent adapters
runners/langgraph/    LangGraph, Deep Agents, and create_agent adapters
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
approval events, control-plane decisions, and owner-only terminal state. MAF
Harness metering occurs below its internal function-invocation loop so every
provider dispatch, including tool-loop iterations, consumes the shared outer
budget. The control plane also finalizes those executions into the five frozen
incident artifacts,
deterministic scores, individually validated bundles, and a persistent paired
experiment with pair-level provenance validation. The opinionated track is also
executable: MAF Harness background agents and declarative Deep Agents subagents
perform native role-scoped delegation, pass a shared evidence gate, and enter
the same approval, execution, finalization, scoring, and pair-validation path.
Harness state is currently process-local and non-resumable. Both runner families
also expose a matched single-agent lane with one model-driven commander, a
duplicate-free union discovery catalog, strict source-endpoint evidence
provenance, and the same outer approval/recovery boundary. Both runner families
now retain correlated, redacted native span ledgers, export best-effort to a
pinned loopback Phoenix deployment, and record validated operation-level trace
completeness without making collector availability run-critical. These smoke
runs exercise the system but do not constitute a benchmark result. Clean-source
harness and compute-matched baseline smoke pairs have now been retained, along
with two corrected Qwen3.5-9B task-quality gates. The first contained one
LangGraph task success and one valid MAF evidence-gate failure; the second
reversed the split, with MAF succeeding while LangGraph completed without
effective containment or recovery. Neither pair had two successful lanes, so
Qwen3.5-9B remains a fit-check model and none of the larger profiles were
promoted. The clean second pair also validates the versioned per-call,
host/GPU, and control-plane ledgers documented in the
[native run-observability validation](docs/research/run-observability-validation.md).
A subsequent balanced 16-launch tracing ablation measured a consistent fixed
control-path cost: median paired increases of 134 ms for MAF and 423 ms for
LangGraph, including only 6--7 ms of explicit trace flush. Native tracing stays
enabled for measured pilots; the full method and limits are in the
[tracing study](docs/research/native-tracing-overhead.md).
The next empirical milestone is a quality-tier model that clears the one-seed
paired gate; production checkpoint-backend selection remains a separate
deployment decision.
Run bundles can already be checked with `agent-lab bundle validate` and
`agent-lab bundle validate-pair`.

Exercise the four multi-agent lanes' process boundary without loading a model:

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

Run the matched opinionated-harness smoke with:

```bash
uv run agent-lab experiment run \
  --experiment configs/experiments/harness-smoke.toml \
  --runs-dir runs \
  --order-seed 1737
```

This uses native MAF Harness background agents and Deep Agents declarative
subagents. The adapter verifies every accepted specialist against its actual
role-scoped MCP calls and exact evidence IDs; see
[`docs/gotchas.md`](docs/gotchas.md) for framework-specific stopping and
structured-output behavior observed with the local model.

Run the matched compute-budget single-agent smoke with:

```bash
uv run agent-lab experiment run \
  --experiment configs/experiments/single-agent-compute-matched-smoke.toml \
  --runs-dir runs \
  --order-seed 1741
```

This compares primitive MAF `Agent` with LangChain `create_agent`. Each lane
has exactly one model-driven incident commander and the same duplicate-free
union of capability-scoped discovery tools. Source-role labels preserve MCP
evidence provenance and do not represent delegated agents. Both adapters use
the same two phases: unstructured MCP discovery must satisfy a two-source gate,
then a tool-disabled structured turn synthesizes only from the observed
source-specific evidence allowlist. Each phase has the same eight-dispatch
local model-call ceiling in both adapters, beneath the shared outer run budget.

To exercise both adapters against a live loopback model without creating
benchmark evidence, run:

```bash
uv run --project services/incident_mcp --no-sync \
  incident-lab-single-agent-compat \
  --model '<model-id-advertised-by-the-endpoint>'
```

The probe uses twin seeded worlds, deliberately edits approval to the known
recovery action, requires both lanes to recover, and validates both bundles and
their pair. It uses greedy compatibility sampling, verifies that per-role and
protocol model/tool/output accounting agree, and marks its output
`measurement_eligible: false`.

The clean-source Qwen3.5-9B sequence is preregistered as a one-seed task-quality
gate followed by separately executable explicit, harness, compute-matched
baseline, and unrestricted-baseline pilot profiles:

- `configs/experiments/qwen35-9b-task-quality-pilot.toml`
- `configs/experiments/explicit-pilot.toml`
- `configs/experiments/harness-pilot.toml`
- `configs/experiments/single-agent-compute-matched-pilot.toml`
- `configs/experiments/single-agent-unrestricted-pilot.toml`

The larger profiles are not promoted when either task-quality-gate lane fails
to complete successfully. Harness sensitivity profiles freeze the same seeds,
repetitions, budgets, and fault schedule while changing exactly one shared
prompt, correction, role-topology, or native-compaction control:

- `configs/experiments/harness-correction-zero-ablation.toml`
- `configs/experiments/harness-correction-two-ablation.toml`
- `configs/experiments/harness-paraphrase-a-ablation.toml`
- `configs/experiments/harness-no-review-ablation.toml`
- `configs/experiments/harness-no-compaction-ablation.toml`

All pilot and ablation profiles share the equal-weight, five-family case
manifest in `configs/cases/pilot-v1.json`. Resolve their runner support,
contract preflights, cases, and balanced per-block lane order without starting
services via `agent-lab experiment plan`; the frozen selection and command are
documented in `docs/pilot-case-plan.md`.

Qwen3.5-9B did not clear that gate. The separately pinned 35B-A3B GPTQ-Int4
candidate was then tested at 16K/8 GiB offload and at a minimal 8K/2 GiB
offload operating point. Both configurations loaded all weights but failed
during vLLM engine initialization because Triton kernels received CPU-offloaded
tensors. A no-offload configuration cannot leave useful KV-cache headroom on
this host, so the candidate is rejected for the current single-4090 stack. The
failed gate and immutable evidence identities remain documented in
`docs/research/qwen35-35b-a3b-gate.md`; no larger profile was promoted.

The effective prompt/policy payload is hashed into both paired manifests and
the pair preflight. Both native adapters consume the same validated
`harness_ablation` request object; supported provenance-correction counts are
zero, one, and two turns. The same object can remove the reliability reviewer
from both lanes or disable both frameworks' native context compaction; active
roles and compaction mode are frozen into the effective normalized topology.

Measured execution refuses a dirty Git worktree. Commit the reviewed source
freeze before running those profiles; protocol probes remain explicitly
non-measurement evidence.

After one or more native profiles finish, build a fail-closed offline report
from their immutable summary indices:

```bash
uv run agent-lab report build \
  --index runs/native-workflow-explicit_workflow_pilot_v1-order-1729.json \
  --index runs/native-workflow-opinionated_harness_pilot_v1-order-1729.json \
  --output-dir reports/pilot-v1
```

The command revalidates every individual bundle and pair, preserves the frozen
left/right lane order, retains operational failures in denominators, and emits
portable `paired-report/v2` JSON plus self-contained HTML. Paired effects cover
scores, task/control latency, calls, all reported token dimensions, provider
queue/TTFT/generation time, client concurrency, GPU/host samples, and native
trace coverage. Continuous effects include deterministic paired-bootstrap
intervals; binary task success includes discordant counts and an exact McNemar
result; scorer components report their observed discrimination per lane.

The new owner-only output directory also contains redacted source/run evidence,
fresh individual and pair validations, a complete private copy of each source
bundle, and a SHA-256 inventory. It is independent of the ignored `runs/` tree
after transfer to access-controlled durable storage. Do not publish the
`private-runs/` subtree; follow [the retention procedure](docs/evidence-retention.md).

No license is granted by this private repository unless one is added later.
