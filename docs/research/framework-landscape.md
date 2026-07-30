# Framework landscape

Research cutoff: 2026-07-31. Sources are primary project documentation,
repositories, papers, and package registries.

## Executive finding

“Microsoft Agent Framework vs LangChain” is too coarse to be a fair
experiment. The current ecosystems contain comparable layers:

| Layer | Microsoft Agent Framework | LangChain ecosystem |
| --- | --- | --- |
| Model/tool agent primitive | `Agent` and chat clients | LangChain `create_agent` and chat models |
| Opinionated long-horizon harness | Agent Framework Harness | Deep Agents |
| Explicit graph/workflow runtime | Agent Framework Workflows | LangGraph |
| Framework-native visibility | OpenTelemetry workflow/agent spans | LangGraph event streams and LangSmith-oriented tracing |

The study should compare matched layers and retain a single-agent baseline.

## Microsoft Agent Framework

### Current shape

Microsoft's current overview divides the product into Agents, Harness, and
Workflows. It describes agents with tools/MCP, an opinionated harness for
long-running work, and graph-based workflows with state, checkpointing, and
human-in-the-loop behavior. [Microsoft Agent Framework
overview](https://learn.microsoft.com/en-us/agent-framework/overview/)

The framework reached a stated v1.0 release in April 2026; the package registry
showed `agent-framework` 1.12.1 at the research cutoff. The modular provider and
orchestration packages do not all share the same latest version number, so the
project should lock a tested set rather than assume one global version.
[PyPI: agent-framework](https://pypi.org/project/agent-framework/),
[Microsoft samples](https://github.com/microsoft/Agent-Framework-Samples)

### Workflows

Built-in orchestration patterns include sequential, concurrent, handoff, group
chat, and Magentic coordination. Handoff and group chat are dynamic while the
workflow layer also supports deterministic routing, requests/approvals, and
checkpoint storage. [Workflow
orchestrations](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/),
[handoff
orchestration](https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/orchestrations/handoff),
[group chat
orchestration](https://learn.microsoft.com/en-gb/agent-framework/workflows/orchestrations/group-chat)

The important comparison implication is that a built-in MAF orchestration
should not be compared with an unconstrained Deep Agent. Its closest peer is a
custom LangGraph expressing the same topology and termination semantics.

### Harness

The Agent Framework Harness now bundles the tool loop, per-service-call history
persistence, context compaction, todo and mode providers, file memory/access,
approval policy, OpenTelemetry, optional skills, optional background agents,
optional shell, and optional looping. Background agents provide parallel
delegation. [Agent Framework
Harness](https://learn.microsoft.com/en-us/agent-framework/agents/harness)

This is now a direct conceptual peer to Deep Agents. Comparing the two should
preserve their built-in scaffolding and record effective prompt/tool surfaces,
because those defaults are part of the product value.

### Local model and MCP support

The Python OpenAI chat-completions client accepts a custom `base_url`, and the
documentation explicitly lists vLLM, Ollama, and LM Studio as compatible local
servers. Chat Completions is the broad-compatibility path for local models.
[OpenAI-compatible
endpoints](https://learn.microsoft.com/en-us/agent-framework/integrations/openai-endpoints?tabs=dotnet-cli%2Cuser-secrets)

Agent Framework supports local MCP tools and approval around function tools.
Its MCP abstractions include stdio and Streamable HTTP clients.
[Using MCP
tools](https://learn.microsoft.com/en-us/agent-framework/user-guide/model-context-protocol/using-mcp-tools),
[adding
tools](https://learn.microsoft.com/en-us/agent-framework/journey/adding-tools)

At the cutoff, `agent-framework-core` 1.12.1 declared `mcp>=1.24,<2` in its
`all` extra, while MCP 2.0.0 had just appeared on PyPI. That is why the initial
service/runner projects pin MCP 1.x and isolate dependencies.
[PyPI: agent-framework-core](https://pypi.org/project/agent-framework-core/),
[PyPI: MCP](https://pypi.org/project/mcp/),
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

### Observability

MAF emits standard agent telemetry plus workflow spans for sessions,
invocations, executor processing, edge processing, and message sends. Python
enables this globally and can configure OTLP exporters. Sensitive data is off
unless explicitly enabled. [Workflow
observability](https://learn.microsoft.com/en-us/agent-framework/workflows/observability)

This is a likely MAF strength worth measuring: the study should score span
coverage and debugging ergonomics, while retaining a neutral event model so a
richer native schema does not bias task scoring.

## LangChain, LangGraph, and Deep Agents

### Layering

LangGraph is the graph runtime: state, nodes, and edges define an evolving
application. LangChain's `create_agent` is a higher-level agent loop built on
that runtime. Deep Agents is an opinionated harness built on the same stack.
[LangGraph Graph
API](https://docs.langchain.com/oss/python/langgraph/graph-api),
[Deep Agents
repository](https://github.com/langchain-ai/deepagents)

The package registry showed LangChain 1.3.14, LangGraph 1.2.10, and Deep Agents
0.7.1 at the cutoff. These are rapidly moving projects, so lock files and
effective API smoke tests are mandatory.
[PyPI: LangChain](https://pypi.org/project/langchain/),
[PyPI: LangGraph](https://pypi.org/project/langgraph/),
[PyPI: Deep Agents](https://pypi.org/project/deepagents/)

### LangGraph and multi-agent patterns

LangChain documents subagents, handoffs, skills, routers, and custom workflows.
Custom LangGraph workflows are recommended when deterministic logic must mix
with agentic routing, loops, or parallel work. [Multi-agent
overview](https://docs.langchain.com/oss/python/langchain/multi-agent),
[custom
workflows](https://docs.langchain.com/oss/python/langchain/multi-agent/custom-workflow)

LangGraph persistence saves checkpoints organized into threads and supports
human-in-the-loop, memory, time travel, and fault recovery. [LangGraph
persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

The framework exposes token, message, state, checkpoint, task, debug, custom,
and typed event streams. These are strong inputs to a live observer and
framework-neutral adapter. [LangGraph
streaming](https://docs.langchain.com/oss/python/langgraph/streaming),
[event
streaming](https://docs.langchain.com/oss/python/langgraph/event-streaming)

### Deep Agents

Deep Agents supplies planning/todos, filesystem backends, context
summarization, subagents, memory, skills, human approval, and optional shell
execution through sandbox backends. It accepts any LangChain chat model that
supports tool calling, including self-hosted models through vLLM, Ollama, or
llama.cpp. [Deep Agents
overview](https://docs.langchain.com/oss/python/deepagents/overview),
[customization](https://docs.langchain.com/oss/python/deepagents/customization)

Deep Agents exposes subagent progress, model tokens, and tool calls through
LangGraph subgraph/event streaming. [Deep Agents
streaming](https://docs.langchain.com/oss/python/deepagents/streaming),
[Deep Agents event
streaming](https://docs.langchain.com/oss/python/deepagents/event-streaming)

The key experiment implication is context: synchronous subagents isolate their
working context and return results to a coordinator. MAF background agents and
Deep Agents subagents should receive matched role descriptions and capability
boundaries, but each harness should keep its native context-management
strategy.

### Local model and MCP support

LangChain chat models accept a custom OpenAI-compatible `base_url`; the docs
explicitly cite vLLM. [LangChain
models](https://docs.langchain.com/oss/python/langchain/models)

`langchain-mcp-adapters` converts tools from one or more MCP servers into
LangChain tools. Its multi-server client is stateless by default and creates a
fresh session for a tool invocation unless explicit session management is
used. That lifecycle difference must be controlled in the adapter.
[LangChain MCP](https://docs.langchain.com/oss/python/langchain/mcp)

## Neutral observability

Phoenix is a self-hostable tracing and evaluation UI that accepts OTLP and
provides OpenInference instrumentation for LangChain. MAF can export its native
OpenTelemetry spans to the same collector. Phoenix therefore supplies one
local trace store without requiring either Microsoft cloud services or
LangSmith. [Phoenix
overview](https://arize.com/docs/phoenix),
[self-hosting
configuration](https://arize.com/docs/phoenix/self-hosting/configuration),
[Phoenix OTEL
setup](https://www.arize.com/docs/phoenix/tracing/how-to-tracing/setup-tracing/setup-using-phoenix-otel)

Phoenix does not eliminate native schema differences. A separate normalized
event envelope remains necessary for a synchronized side-by-side view,
deterministic scoring, and trace-completeness comparison.

## Risks to verify in code

- MAF and LangGraph may serialize tool history differently against the same
  OpenAI-compatible endpoint.
- Reasoning-content fields may be preserved, dropped, or counted differently.
- MCP session lifetime defaults differ.
- Harness-added tools/prompts can create unequal context overhead.
- Parallel subagents share one vLLM queue, so scheduling may dominate latency.
- Preview/beta subpackages and recent API changes make lock files insufficient
  without an executable compatibility test.
- “Same model parameters” is not the same as identical wire payloads; capture
  hashes and selected redacted request structure.

These are measurements for the lab, not reasons to preselect a winner.
