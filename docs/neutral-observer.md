# Neutral paired observer

The local observer renders only the framework-neutral `NormalizedEvent` stream.
It does not import either runner, depend on a framework UI, request model
reasoning, or render sensitive/internal events. Both live execution and replay
of completed `events.jsonl` bundles use the same FastAPI read surface.

## Start the observer

```bash
uv run agent-lab observer serve --runs-dir runs
```

The server refuses non-loopback binds and listens on
`http://127.0.0.1:8787` by default. On first start it exclusively creates
`runs/.observer-token` with mode `0600`; later starts reuse that credential
only if it remains owner-only. Paste its value into the browser's approval-token
field when an interactive run reaches a pending decision. Use `--read-only` to
disable the control endpoint entirely.

The browser shows two synchronized lane timelines with:

- active agents and native delegation/handoff boundaries;
- model and tool lifecycle events, evidence-bearing artifact links, and errors;
- approval, checkpoint, terminal, and budget state;
- the local Phoenix trace deep link when `native/trace-link.json` exists.

The page receives new events with Server-Sent Events. Reconnecting to
`/api/runs/{run_id}/events?after=N` replays only sequences strictly greater
than `N`, so the same endpoint works for an in-progress append-only file and a
completed offline bundle. A pair-level snapshot supplies both lane clocks and
last sequences before the two streams attach.

## Interactive approval smoke

Start the observer first, then run the explicitly interactive development
profile in another terminal:

```bash
uv run agent-lab experiment run \
  --experiment configs/experiments/qwen35-9b-observer-interactive-smoke.toml \
  --runs-dir runs \
  --order-seed 1729
```

Both bundles and their shared `native/observer-pair.json` discovery context are
allocated before the first ordered lane starts. When a public
`approval.requested` event appears, the browser can approve, edit to a canonical
mitigation action ID, or reject it. The bearer credential is accepted only from
loopback and is never stored in the decision payload or audit. The API writes an
exclusive owner-only decision plus `control/approval-api-audit.jsonl`; the
experiment control process converts that decision into the simulator's normal
`ApprovalRecord`. The runner then emits the canonical `approval.decided` event,
and finalization retains the command audit as usual.

`approval_policy = "scripted"` remains frozen in every unattended comparison
profile. Interactive decisions are a development/operator workflow, not a
measured treatment, unless a future protocol explicitly preregisters them.

## Read API and headless rendering

The versioned local API exposes:

- `GET /api/meta` for capabilities and canonical mitigation IDs;
- `GET /api/pairs` and `GET /api/pairs/{pair_id}` for paired snapshots;
- `GET /api/runs/{run_id}` for a single lane snapshot;
- `GET /api/runs/{run_id}/events?after=N&follow=true` for SSE replay/tailing;
- `GET /api/runs/{run_id}/artifacts/{name}` for publicly attested artifacts;
- `POST /api/runs/{run_id}/approval` for bearer-authenticated local control.

The terminal renderer consumes the same sanitized pair endpoint:

```bash
uv run agent-lab observer render pair-0123456789abcdef0123
uv run agent-lab observer render pair-0123456789abcdef0123 --watch
```

Pair IDs come from `/api/pairs`; they are opaque and stable across the live
sidecar and final experiment index.

The complete live-to-replay acceptance record, including one authenticated
approval on each lane, is retained in
[neutral-observer-validation.md](research/neutral-observer-validation.md).

## Redaction boundary

The observer drops all non-public events and projects each public event through
an event-kind field allowlist. `model.text_delta` payloads are deliberately
empty even if a development adapter emitted one. Prompts, responses, tool
arguments/results, arbitrary payload extensions, credentials, and reasoning
content therefore cannot reach either renderer through this API. Artifact
downloads are restricted to paths below the run's `artifacts/` directory that
also have a public `artifact.written` attestation.
