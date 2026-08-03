# Frozen pilot cases and execution plan

Status: implemented and protocol-validated; empirical pilot remains gated on an
accepted quality-tier model.

The pilot and required harness ablations use one version-controlled case
manifest, [`configs/cases/pilot-v1.json`](../configs/cases/pilot-v1.json). It
selects exactly one deterministic case from every v0.1 incident family with
equal weight:

| Seed | Incident family | Case ID |
| ---: | --- | --- |
| 101 | `cache_ttl_regression` | `inc-00000065-054a6860f3` |
| 202 | `schema_drift` | `inc-000000ca-2af1f3a899` |
| 303 | `retry_storm` | `inc-0000012f-0a7d2fe67b` |
| 400 | `feature_flag_mismatch` | `inc-00000190-1370e1fa02` |
| 505 | `connection_pool_exhaustion` | `inc-000001f9-d5c6ef13ca` |

The previous provisional list used seed 404, which generated a second retry
storm and omitted feature-flag mismatch. Seed 400 replaces only that duplicate.
The loader rederives every case ID and family from generator v0.1.0 and fails
closed on drift, duplicate seeds, missing families, or unequal weights. The
manifest's canonical SHA-256 is
`7053887477730791818c9c936418142f4e74d6bd7251b0990ee0d886060ea4f3`.

## Pre-execution planning

Resolve any checked-in profile without starting a service or invoking a model:

```bash
uv run agent-lab experiment plan \
  --experiment configs/experiments/explicit-pilot.toml \
  --order-seed 1729
```

The planner uses the same profile loading, runner descriptors, track selection,
world cloning, contract preflight, case-manifest verification, and lane-order
resolver as execution. Its output records source identity, model profile,
declared execution mode, case-manifest source and hash, every seed/repetition,
per-block lane order, and contract-preflight hashes. With the frozen order seed
1729, the 15 three-repetition blocks assign first position 8/7 between the two
lanes, and both orders occur for every incident seed.

Native execution writes this plan with exclusive creation before starting the
incident service or either runner. The final experiment index links the same
plan and case selection. CI runs the planner over every checked-in experiment
profile after installing the locked isolated environments; a structurally
unexecutable profile therefore fails before measurement.

The old combined four-track `paired_pilot.toml` was removed. Explicit workflow,
opinionated harness, compute-matched single-agent, and unrestricted single-agent
profiles remain separate comparisons and must not be pooled.

Planning proves structural readiness only. It does not waive the model-quality,
checkpoint-backend, tracing/resource-metric, evidence-retention, or reviewer
approval gates for measured execution.
