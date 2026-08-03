# ADR 0011: Select Cosmos DB for production-intended workflow checkpoints

Status: Accepted

Date: 2026-08-03

## Context

The compatibility file savers prove that MAF Workflow and LangGraph can pause
at the same approval boundary and resume after a process restart. They do not
provide a distributed deployment store and cannot support a production-ready
capability claim. Selecting unrelated operational stores for the two lanes
would also add avoidable deployment asymmetry to checkpoint evaluation.

MAF ships a production Cosmos checkpoint provider. Microsoft also ships a
LangGraph checkpointer in `langchain-azure-cosmosdb`, allowing both frameworks
to use one service and authentication model while retaining their native state
formats.

## Decision

Select Azure Cosmos DB as the production-intended checkpoint substrate for
both explicit-workflow lanes. Keep each provider in an optional isolated-runner
dependency group and use separate containers for the providers' incompatible
partition-key schemas.

Connection configuration is environment-only. Require HTTPS, preserve TLS
verification, prefer `DefaultAzureCredential`/managed identity when an account
key is absent, and never place credentials in process arguments or output.

Retain the local compatibility savers for default benchmark bundles. Do not
set their runner capability `production_ready` flag merely because a separate
provider validation passed.

## Consequences

- Both frameworks have passed the same native approval restart test against a
  real Cosmos API surface and in fresh processes.
- The persistence service and production authentication model are symmetric;
  native checkpoint schemas remain framework behavior.
- Optional provider dependencies do not expand default runner installations.
- The Cosmos emulator validates API compatibility and restart semantics, not
  production availability, RBAC, scaling, backup, or cost.
- An intended deployment must repeat the gate against its real Azure account
  before that configured runner can claim production readiness.
