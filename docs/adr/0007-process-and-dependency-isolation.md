# ADR 0007: Isolate runners and services

Status: Proposed

Date: 2026-07-31

## Context

Frameworks have independent dependency cadences, global instrumentation, event
loops, and MCP client defaults. At the research cutoff, MAF's package metadata
still constrained MCP below 2 while MCP 2.0 had just released. A shared Python
environment could create conflicts or cross-instrumentation that do not exist
in real deployments.

## Decision

Keep a small root package for contracts/configuration/scenario/run-bundle code.
Run MAF, LangGraph/Deep Agents, and the incident MCP service as separate Python
projects and OS processes. Give every project an independent committed lock
file before measured runs.

Communicate through versioned JSON/event APIs, MCP, OTLP, and the model's HTTP
API. Framework-native Python types never cross a process boundary.

## Consequences

- Dependency resolution and instrumentation are isolated.
- Crashes and global state do not contaminate a paired lane.
- Environment digests are explicit.
- Development and startup have more moving parts.
- The control plane needs robust process lifecycle and health checks.
- MCP 2.0 can be tested in the service without forcing an unplanned MAF
  upgrade.
