# Neutral observer live/replay validation

Status: validated on a clean, paired Qwen3.5-9B development execution on
2026-08-03

This record validates the neutral observer's live discovery, authenticated
approval, completion, and replay paths. It is an operator acceptance test, not
a framework result: the profile deliberately used
`approval_policy = "observer"`, whereas unattended comparison profiles freeze
scripted approvals.

The run used source commit `b8b69b2fcefe6603eab8f9c8da3914bb04ac7764`,
experiment `qwen35_9b_observer_interactive_smoke_v1`, case seed 42, and order
seed 2026080704. The experiment preallocated both lane bundles before the first
ordered lane started and exposed the same opaque pair ID throughout live
execution and completed replay:
`pair-f1476fddc7b76009d0a9`.

## Acceptance result

| Lane | Run ID | Terminal | Last sequence | Public artifacts |
| --- | --- | --- | ---: | ---: |
| LangGraph | `f4122093-e0b3-451b-a306-164990c1898c` | completed | 66 | 5 |
| MAF workflow | `3ed53efd-caf0-4957-986b-89e63ac768c8` | completed | 66 | 5 |

Each lane reached one public approval request. The loopback API accepted one
operator approval for LangGraph at `2026-08-03T06:43:02.262310Z` and one for
MAF at `2026-08-03T06:45:07.259188Z`. Both owner-only audit files contain
exactly one accepted record, both exclusive decision files have mode `0600`,
and both runners emitted the corresponding normalized `approval.decided`
event at sequence 53. No credential was retained in either decision or audit
record.

After both lanes completed, the pair endpoint reported no pending approvals,
terminal reason `native_workflow_completed`, and five publicly attested
artifacts on each side. Reconnecting to each SSE endpoint with `after=64`
replayed sequences 65 and 66 followed by the `complete` event. Independent
bundle-pair validation reported `valid = true`, `inference_status = "eligible"`,
and no findings.

Structural inference eligibility means that the bundles satisfy the ordinary
integrity contract; it does not promote this interactive development run into
the comparison cohort. Both lanes failed the scenario task-success threshold,
and their scores are intentionally not interpreted here.

## What this closes

The live run confirms that:

- both lane bundles are discoverable under one stable pair before sequential
  execution begins;
- browser/API approval decisions traverse the bounded control plane and retain
  the normal normalized decision event;
- an observer disconnect is non-critical because the completed append-only
  stream resumes from an exact sequence cursor; and
- live and replay views expose only the sanitized normalized event and
  publicly attested artifact surfaces.

Run directories remain ignored local evidence. This checked-in record retains
only identities, aggregate acceptance facts, and UTC timestamps; it does not
commit prompts, model responses, traces, machine telemetry, or credentials.
