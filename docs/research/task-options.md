# Task investigation and selection

## Selection criteria

The primary task needs to:

- genuinely require multiple capabilities or agents;
- force tool use because the prompt lacks the answer;
- expose tools through MCP without favoring one framework;
- include controlled, replayable nondeterminism;
- have hidden ground truth and deterministic outcome scoring;
- exercise parallel work, conflict resolution, retries, checkpointing, and
  approval;
- run locally without paid APIs or a mutable live web;
- generate a legible live execution for a human reviewer;
- be difficult enough for a local model without becoming mostly a model-size
  benchmark.

## Candidate tasks

| Candidate | Strengths | Weaknesses | Decision |
| --- | --- | --- | --- |
| Seeded incident response | Natural specialist roles, parallel evidence, causal decoys, objective recovery state, approval and fault injection | Simulator/tool realism requires careful design | Primary |
| Disrupted travel or retail support | Strong policy/tool/user interaction; clear transaction outcomes | User simulator adds another LLM or large policy fixture; less obviously multi-agent | Secondary |
| Collaborative repository repair | Deterministic tests, real artifacts, strong planner/executor/verifier split | Local model coding ability can dominate; shell sandbox broadens risk and setup | Future benchmark/ablation |
| Live research dossier | Excellent search/MCP/tool diversity and readable traces | Mutable ground truth, rate limits, citation judging, and network variance weaken pairing | Ecological demo only |
| Logistics/market simulator | Rich planning under changing state and measurable reward | Can become a game/RL benchmark unlike normal agent engineering | Future stress test |
| Fraud/claims investigation | Hidden evidence, role permissions, reversible/irreversible actions | Requires a credible policy/data simulator; domain errors can be high stakes | Alternate scenario |

## Relevant benchmark lessons

### Tool use and dynamic environments

GAIA popularized human-legible real-world questions requiring reasoning,
multimodality, browsing, and tools. It is useful as an ecological follow-up,
but hidden answers and live web dependencies make it less suitable for the
first paired framework study. [GAIA paper and
dataset](https://huggingface.co/gaia-benchmark)

MCP-Universe focuses on long-horizon interaction with real MCP servers, broad
tool spaces, and dynamic evaluation. Its repository spans repository
management, financial analysis, navigation, browser automation, and search.
The lesson adopted here is outcome-oriented evaluation over tool-format
compliance; the primary lab substitutes a seeded local environment for mutable
external services. [MCP-Universe
repository](https://github.com/SalesforceAIResearch/MCP-Universe),
[MCP-Universe paper](https://arxiv.org/abs/2508.14704)

MCP-AgentBench similarly argues for outcome-oriented evaluation across
operational MCP servers and interaction complexity. [MCP-AgentBench,
AAAI](https://doi.org/10.1609/aaai.v40i37.40347)

### User/tool policy workflows

The current τ-bench project models customer-service domains with policy, agent
tools, tasks, and optionally user tools. It is a good model for stateful tools
and policy compliance, but the user simulator becomes an additional source of
variance. The incident lab adopts explicit policy and state-transition grading
without placing another model in the primary loop.
[τ-bench repository](https://github.com/sierra-research/tau2-bench)

### Multi-agent coordination

MultiAgentBench evaluates collaboration/competition across scenarios and
compares coordination topologies. Its main lesson is to measure milestone and
coordination behavior, not only final answers. [MultiAgentBench
paper](https://arxiv.org/abs/2503.01935),
[MARBLE code](https://github.com/MultiagentBench/MARBLE)

TeamBench enforces Planner, Executor, and Verifier access boundaries in
separate containers and uses seeded generators plus deterministic partial
graders. Its results also caution that teams can add overhead when a solo agent
already succeeds, and that verifier approval can be wrong. The field lab adopts
real capability separation, seeded variants, deterministic grading, and
single-agent/role ablations rather than assuming teamwork helps.
[TeamBench paper](https://arxiv.org/abs/2605.07073),
[TeamBench project](https://teambench.github.io/)

## Why incident response wins

Incident response creates a compact, visually understandable example of the
behaviors the user wants to compare:

- telemetry and change-history investigations can run independently;
- symptoms create plausible but incorrect causal stories;
- evidence must be exchanged and reconciled;
- bounded tool failures test recovery;
- state-changing mitigation naturally triggers human approval;
- the world can directly determine whether recovery occurred;
- every seed can change the fault and decoy without changing task structure;
- the final artifact is readable, while intermediate events make each
  framework's orchestration visible.

Most importantly, the task makes coordination *necessary* by giving specialists
different MCP capabilities. A single-agent baseline with the union catalog
then measures the cost/value of that separation.

## Secondary scenarios after the primary study

1. Adapt a small τ-bench-style retail case to test multi-turn user interaction.
2. Run a TeamBench-inspired repository task with planner/executor/reviewer
   filesystem separation.
3. Add an MCP-Universe or GAIA-like live research task as an ecological demo.

They should reuse the same run/event/evaluation contracts without changing the
primary study midstream.
