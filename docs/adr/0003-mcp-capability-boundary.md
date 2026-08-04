# ADR 0003: Make MCP the tool and capability boundary

Status: Accepted

Date: 2026-07-31

## Context

Both ecosystems support MCP. Providing in-process framework-specific tools
would duplicate schemas and could hide lifecycle or serialization differences.
Prompt-only roles also fail to prove that agents coordinated: one agent can
quietly use another role's capabilities.

## Decision

Expose all scenario evidence and actions through one benchmark-owned MCP
service. Use run-scoped credentials and role claims to vary visible tool
catalogs. Keep ground truth and evaluator tools on a separate, inaccessible
boundary. Require approval before a state-changing tool executes.

Freeze tool names, descriptions, schemas, errors, and result semantics before
measured runs. Start on MCP Python SDK 1.x for compatibility and spike MCP 2.0
separately.

## Consequences

- Both stacks see the same protocol and tool definitions.
- Role separation is structurally testable.
- MCP lifecycle, retries, errors, and overhead become visible measurements.
- A single-agent baseline needs an explicit union catalog.
- The service must implement authentication, state cloning, audit logs, and
  protocol compatibility correctly.
