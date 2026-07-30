# Paired observer

Planned neutral live and replay UI.

The observer consumes only normalized events. It must not import either agent
framework, call the model, or access hidden world state.

Recommended first slice:

- FastAPI read API plus Server-Sent Events;
- local browser client with two synchronized lane timelines;
- active-agent/topology projection;
- tool calls, evidence, errors, retries, checkpoints, and budget meters;
- approval control with an audited decision;
- replay of completed `events.jsonl`;
- deep links into Phoenix traces and bundle artifacts.

The same server API should support a small Rich/Textual renderer for headless
use. See the live-comparison section of the
[specification](../../docs/SPEC.md#10-live-comparison).
