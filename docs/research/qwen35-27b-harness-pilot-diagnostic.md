# Qwen3.5-27B matched-harness pilot: diagnostic launch

Status: diagnostic only; replacement gate and pilot required

Date: 2026-08-03

## Scope and provenance

The first `opinionated_harness_pilot_v1` launch used clean source commit
`a18c076a3aba77dad9f8289bed75524836cd2a40` and the promoted Qwen3.5-27B
Q4_K_M llama.cpp operating point. Characterization was measurement-eligible,
had no skipped cells, and reached a 23,372 MiB whole-device peak. Every MAF and
LangChain compatibility fixture passed.

The launcher was stopped during seed 101 after repeated structural adapter
failures. Four bundles were preserved: one complete pair for repetition 0, one
MAF terminal for repetition 1, and the prepared Deep Agents peer. No cohort
index was finalized.

| Artifact | SHA-256 |
| --- | --- |
| `characterization.json` | `77c67e9d8ea09106927f2e1888256bb355618f16cf4a3760b1d2edf5d446dfbe` |
| `model-compatibility.json` | `1488b3fb845748b42dc9775e7490ece057ea102b66cf79f264c396966c48b157` |
| resolved execution plan | `4c015dc0f4fc6f771d864765e30127452fbc0a616c0ced77c8c3587580dc33a2` |

## Observed failures

The complete Deep Agents lane was individually valid but terminated
`budget_exhausted` after 30 model calls and all 64 tool calls. Its telemetry
specialist alone issued 25 model requests because the six-call middleware was
present only on the parent graphs, not inside the native `SubAgent`.

Both MAF attempts terminated `model_failure` at the two-specialist MCP gate.
The commander had started telemetry and change specialists, but the gate ran
before either background task completed an MCP call. Each bundle then gained
four native spans after its trace link was finalized and failed validation on
span-ledger digest, span count (21 recorded versus 25 present), and trace
completeness.

The second Deep Agents bundle was prepared but never executed. Stopping at that
point avoided spending the remaining 26 launches on a cohort known not to
implement its frozen contract.

## Disposition

ADR 0014 applies the phase limiter within each Deep Agents specialist, freezes
a shared completion barrier, reaps parallel Deep task calls on exceptional
exit, and makes MAF wait for or cancel and drain native background work before
gate evaluation and trace finalization. MAF also reserves the six-call ceiling
across all internal completion-loop iterations.

These artifacts remain preserved for diagnosis and regression provenance. They
must not enter pilot aggregates, sample sizing, or a combined report. A clean
model gate and replacement matched-harness pilot are required under the new
harness-contract hash.
