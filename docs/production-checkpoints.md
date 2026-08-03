# Production checkpoint backend

The selected production-intended persistence substrate is Azure Cosmos DB for
both explicit-workflow lanes. MAF uses its first-party
`CosmosCheckpointStorage`; LangGraph uses Microsoft's
`CosmosDBSaverSync` from `langchain-azure-cosmosdb`. The two implementations
use separate containers because their required partition keys differ:
`/workflow_name` for MAF and `/partition_key` for LangGraph.

This is a deployment choice, not a measured treatment. Checkpoint latency and
storage cost are not compared across lanes, and the compatibility file savers
remain the default for local experiment bundles.

## Install the optional providers

```bash
uv sync --project runners/maf --group checkpoint-production
uv sync --project runners/langgraph --group checkpoint-production
```

The direct provider versions are exact pins in the isolated runner locks:

- `agent-framework-azure-cosmos==1.0.0b260721`, aligned with the existing MAF
  dated package cohort;
- `langchain-azure-cosmosdb==1.0.0`;
- `azure-identity==1.25.3` for MAF managed-identity/RBAC support; and
- `aiohttp==3.14.3`, the async Azure SDK transport omitted from the MAF
  provider's declared dependencies.

All direct and transitive artifacts pass the repository's rolling 72-hour
quarantine. No production provider is installed into a runner unless its
optional group is requested.

## Connection boundary

The validation commands read connection details only from the environment:

```text
AGENT_LAB_COSMOS_ENDPOINT=https://account.documents.azure.com:443
AGENT_LAB_COSMOS_DATABASE=agent-lab
AGENT_LAB_COSMOS_CONTAINER=checkpoint-validation
AGENT_LAB_COSMOS_KEY=<optional account key>
AGENT_LAB_COSMOS_CA_FILE=<optional CA bundle path>
```

If the key is absent, both providers use `DefaultAzureCredential`; managed
identity/RBAC is the production default. The endpoint must be absolute HTTPS,
credentials are rejected in its URL, TLS verification cannot be disabled, and
the optional CA setting must name a real file. The key is represented as a
Pydantic secret and is never placed in process arguments or validation output.

Checkpoint storage is trusted private infrastructure. Both providers can
rehydrate framework objects from stored state, so an identity with checkpoint
write access must be treated as code-adjacent and scoped accordingly.

## Cross-process validation

With a test Cosmos account configured, run:

```bash
uv run python scripts/validate_production_checkpoints.py
```

The script allocates separate suffixed containers, pauses each framework at
its native approval boundary, exits the prepare process, reconstructs the same
workflow in a fresh process, resumes the exact request/checkpoint, and verifies
that a different post-resume checkpoint was persisted. It emits a
credential-free JSON result and fails closed on identity, phase, decision,
checkpoint-lineage, or process-boundary drift.

## 2026-08-03 acceptance record

Both lanes passed against the pinned Linux Cosmos emulator release
`EN20260430`, amd64 manifest digest
`sha256:8c9d7e9c836df1171b68079d415b2d93aedb4952dc7f66ad12663f8cc2f309db`.
The emulator ran in HTTPS mode and the clients verified its exported root CA;
certificate verification was not bypassed.

| Lane | Provider format | Prepare PID | Resume PID | Result |
| --- | --- | ---: | ---: | --- |
| MAF workflow | `agent-framework-azure-cosmos-checkpoint/v1` | 1419615 | 1419629 | resumed, new checkpoint |
| LangGraph | `langchain-azure-cosmosdb-checkpoint/v1` | 1419673 | 1419756 | resumed, new checkpoint |

The emulator is a development/CI service, not a production deployment. This
acceptance closes provider API compatibility and process-restart semantics. It
does not establish cloud availability, geo-replication behavior, throughput,
cost, backup/restore, or managed-identity authorization. Those remain
deployment acceptance gates against the intended Azure account.

The normal runner descriptors continue to report `production_ready = false`
because comparison profiles still use owner-only local bundle storage. A
future deployment profile may claim `true` only after wiring these providers
into the live runner path and repeating the same gate against its actual cloud
configuration.

Primary references:

- [Agent Framework checkpoint providers](https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/checkpoints)
- [Azure Cosmos DB framework integrations](https://learn.microsoft.com/en-us/azure/cosmos-db/gen-ai/integrations)
- [Azure Cosmos DB emulator](https://learn.microsoft.com/en-us/azure/cosmos-db/emulator)
