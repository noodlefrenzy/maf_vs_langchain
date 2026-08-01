# Local Phoenix

The experiment uses the pinned `arizephoenix/phoenix:version-19.10.0` image in
`compose.yaml`. Both the UI/HTTP OTLP receiver on port 6006 and the gRPC OTLP
receiver on port 4317 bind only to loopback. Phoenix state persists in the
ignored `infra/phoenix/data/` directory.

Start and inspect it from the repository root:

```bash
docker compose -f infra/phoenix/compose.yaml up -d
docker compose -f infra/phoenix/compose.yaml ps
curl --fail http://127.0.0.1:6006/
```

Experiment profiles send OTLP/HTTP traces to
`http://127.0.0.1:6006/v1/traces`. The runner first performs a bounded TCP
reachability check. If Phoenix is absent, execution continues, the redacted
local span ledger is retained, and `native/trace-link.json` reports
`collector_status = "unavailable"`.

Stop the service without deleting its persisted database:

```bash
docker compose -f infra/phoenix/compose.yaml down
```

Do not use `down --volumes` or delete `infra/phoenix/data/` when run traces must
remain available.
