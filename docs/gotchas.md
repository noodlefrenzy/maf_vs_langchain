# Framework implementation gotchas

This is a running log of behavior observed while implementing the matched
framework lanes. Treat each entry as version-specific evidence, not a promise
about future releases. Recheck it when a runner lockfile changes.

## Microsoft Agent Framework

### Approval request payloads live in `WorkflowEvent.data`

Observed with `agent-framework-core` 1.12.1.

`WorkflowRunResult.get_request_info_events()` returns `WorkflowEvent` objects.
The correlation identifier is exposed as `event.request_id`, while the object
passed to `WorkflowContext.request_info()` is exposed as `event.data`. There is
no `event.request_data` attribute. Reading that invented attribute lets a
workflow pause and checkpoint successfully, then crashes the controller before
it can display the approval request.

The MAF runner therefore validates `approval_event.data` as its persisted
approval envelope and uses `approval_event.request_id` for resume correlation.

### Handler discovery is sensitive to runtime type information

Observed with `agent-framework-core` 1.12.1.

MAF discovers handlers and response handlers from their annotations when an
executor is constructed. Postponed annotations can leave types as strings at
the point where the installed release performs compatibility checks. The
workflow lane keeps annotations available at runtime and supplies explicit
`request`, `response`, `output`, and `workflow_output` types to
`@response_handler`.

When adding a handler, construct the workflow in a deterministic test. Importing
the module alone does not exercise handler discovery.

### `Executor.execute` is framework-owned

Observed with `agent-framework-core` 1.12.1.

A domain handler named `execute` shadows the framework's own executor entry
point. The graph can build but fails when the runtime tries to dispatch the
node. Use a domain-specific handler name such as `mitigate`.

### File checkpoint permissions need explicit hardening

`FileCheckpointStorage` persists approval state, but its default file mode is
affected by the process umask. The MAF runner wraps the storage backend,
pre-creates its atomic temporary file with exclusive mode `0600`, and verifies
the final checkpoint mode; the checkpoint directory is
mode `0700`.

## LangGraph and LangChain

### An interrupt is resumed with the same thread configuration

Observed with `langgraph` 1.2.10.

The first `ainvoke` returns an `__interrupt__` marker. Durable state, including
the public approval request, should be read from `graph.get_state(config)`. The
resume must use `Command(resume=public_response)` and the exact same
`configurable.thread_id`; changing the thread starts or addresses different
state.

The approval token is deliberately kept outside graph state. Only the public
decision is supplied to `Command`, while a process-local token vault is consumed
by the mitigation node.

### The project file checkpointer is a compatibility adapter

The explicit LangGraph lane uses the project-owned `_FileBackedSaver` rather
than assuming that an in-memory saver proves restart behavior. Its JSON file is
part of the runner compatibility surface, not a general-purpose LangGraph
storage implementation. Tests verify that the file never contains the approval
token.

## Cross-process compatibility harness

The first live approval harness used an ad hoc two-phase JSON-lines exchange.
If the runner exited before phase one, `stdout.readline()` returned EOF
(`b""`), not a structured error; attempting `json.loads` then hid the useful
framework traceback behind an unrelated decoding error. That harness has been
replaced by the normalized runner protocol and control plane, but all subprocess
entry points should still surface child stderr when EOF arrives unexpectedly.

## Model output versus orchestration compatibility

The aligned live model consistently inferred that the checkout deployment
should be rolled back, but it omitted the separate retry cap required by the
seed-42 action and added descriptive prose. The simulator intentionally checks
the normalized action exactly, so both otherwise-correct framework executions
were applied but ineffective and recovery remained false. This is a model/task
result, not a MAF or LangGraph runtime failure.

The live compatibility harness needs a positive recovery path to exercise
pause, resume, token binding, mutation, and verification independently of model
quality. Its reviewer therefore records an explicit `edit` decision to the
known seed-42 recovery action. The original model proposal remains in the
approval request, and the edited action is visible in the terminal execution.
The payload labels the policy as `seed_42_edit_to_known_recovery_action`.

That policy knowingly uses fixture truth and makes the run benchmark-ineligible.
Real experiment runs must obey `docs/comparison-protocol.md`: use the declared
framework-neutral approval policy and never inspect evaluator ground truth
before execution terminates.

The persistent `experiment run` path does exactly that: it approves the model's
proposal without editing it. In the first seed-42 paired smoke, both frameworks
earned `0.60`: full diagnosis, evidence provenance, approval/safety, and artifact
credit, but no containment or recovery credit. Both bundles and the pair were
provenance-valid. An operational summary status of `ok` must not be confused
with `task_success`.

### Single-agent structured output also needs a discovery phase

Attaching the baseline proposal schema to the initial primitive-agent turn can
produce a valid-looking answer before any MCP call occurs. Both single-agent
adapters therefore run the same unstructured discovery phase first, require
real calls through at least two capability-scoped endpoints, and only then run
a tool-disabled structured synthesis turn with the exact observed evidence
allowlist. A bounded repair prompt can request one of the still-unused sources;
schema validity alone never satisfies the discovery or provenance gates. Each
phase also has an eight-dispatch model-call ceiling; this keeps a tool-seeking
primitive agent from reaching a framework-specific graph recursion failure
before the shared outer budget can report a meaningful outcome.

The `incident-lab-single-agent-compat` command exercises this path in twin
worlds and uses the same fixture-truth approval edit as the explicit workflow
compatibility probes. It uses greedy sampling to make the positive-path check
repeatable, asserts per-role versus protocol usage equality, and is
intentionally labeled non-measurement evidence.

## Approval protocol boundary

The simulator, not the runner, creates approval tokens. A protocol message that
contains only `approve` or `edit` is insufficient: the runner must receive the
exact authorized action and its bound token before the command MCP tool can
execute. The control plane therefore turns an approval-request event into an
authoritative `ApprovalRecord`, verifies the request ID, and sends the public
record fields plus token over runner stdin.

This command is deliberately ephemeral. Do not persist raw control-plane input
or include the token in an `approval.decided` event. Mock-process tests scan the
event and stderr outputs to keep that boundary honest.

## Opinionated harness integration

Importing MAF 1.12.1's public Harness file-store surface emits an
`ExperimentalWarning` before any agent is created. Background agents and the
file store are usable, but their process-local state is not a restart-safe
checkpoint. The runner descriptor therefore advertises the MAF Harness lane as
non-resumable even though the outer protocol can pause in memory for approval.

MAF's coordinator also treated a top-level structured response schema as an
immediate completion target: the aligned model returned specialist-shaped data
without calling any `background_agents_*` tool. The role-provenance check caught
this because no specialist MCP call existed. The adapter now keeps the first
Harness turn unstructured so native delegation can occur, applies the shared
minimum-two-specialist evidence gate as an in-session continuation when needed,
then asks the same session for the structured proposal in a tool-disabled final
turn. The model may summarize concrete MCP names using conceptual aliases such
as `metrics_query`; exact tool-name prose is therefore retained as model output
but not trusted. Actual per-role MCP calls and every cited evidence ID remain
exactly verified. Do not remove that provenance check merely because the final
object validates as JSON.

In a later smoke run, two MAF specialists did make real MCP calls, but the
coordinator rewrote an opaque `ev-...` identifier as a descriptive timestamp
while producing the structured proposal. The adapter does not normalize or
silently repair that output. Instead, its tool-disabled final turn includes the
role-specific set of IDs actually observed by the protocol adapter and tells
the coordinator to copy from that allowlist byte-for-byte; the subsequent
provenance validator still fails closed if the model cites anything else.
One run ignored the allowlist and invented a `change-...` identifier anyway.
The MAF lane permits one tool-disabled correction turn containing the validator
error and the same observed allowlist; a second mismatch remains a runner
failure rather than being rewritten by adapter code.

Deep Agents 0.7.1 automatically adds a `general-purpose` subagent unless a
matching `HarnessProfile` disables it. That would create a fifth, unmatched
role, so the experiment registers an OpenAI-family profile that disables the
extra subagent and the unavailable `execute` tool while retaining native todo,
filesystem, summarization, and specialist dispatch behavior.

The matched no-compaction profile disables context reduction through public
but framework-specific hooks. MAF receives `disable_compaction=True` on every
specialist and coordinator Harness agent. Deep Agents receives the documented
`"SummarizationMiddleware"` exclusion through its `HarnessProfile`, which
applies to the coordinator and declarative subagents. Native runners accept one
request per process, so Deep Agents' additive global profile registry cannot
leak the disabled mode into a later condition. Do not execute both compaction
conditions through one reused runner process without adding an explicit
profile-registry reset contract.

Using `response_format` on each declarative Deep Agents subagent caused the
local OpenAI-compatible model to remain in an inner model/tool loop instead of
returning to the coordinator; one smoke run reached the 36-call outer budget.
The subagents now return a tightly specified natural JSON message, while only
the coordinator owns the frozen structured proposal. The adapter still parses
and verifies the coordinator proposal against actual role-scoped MCP results.

Leaving the coordinator's structured-output strategy on automatic selection
was also unstable with this local model. After every specialist had returned,
one run made 19 short commander calls while repeatedly trying to finish the
schema and exhausted the shared 36-call budget. The lane now selects
LangChain's provider-native JSON-schema strategy explicitly; this endpoint
supports that mechanism, and it avoids treating structured completion as a
tool-retry loop.

Provider-native structured output cannot be attached to the first Deep Agents
turn either: doing so made the coordinator return a schema-valid proposal in
one call without invoking any `task` subagent. As with MAF Harness, the lane is
therefore two-phase. An unstructured Deep Agents graph performs native
delegation and is continued until the observed two-specialist MCP gate is met;
a second provider-structured graph receives that conversation and the exact
role-specific evidence allowlist solely for synthesis. A proposal is never
accepted based on schema validity alone.

An unstructured Deep Agents coordinator can also continue redispatching tasks
after enough specialist evidence exists. This is distinct from the
structured-output retry loop: one run spent 22 commander calls in the
delegation graph. The adapter attaches LangChain's native per-invocation model
call limiter to that coordinator, ending a delegation invocation after six
commander calls while retaining its task-result messages. The framework-neutral
36-call outer budget still counts commander and specialist calls and remains
the authoritative run limit.

With those guards frozen, the order-seed-1737 paired smoke completed both
lanes and produced inference-eligible bundles. Deep Agents scored `0.35` and
MAF Harness scored `0.275`; both had `task_success = false` because their chosen
mitigations did not recover the seeded incident. Earlier repetitions produced
valid runner failures for invented citations and valid budget exhaustion for
runaway coordinator turns. Treat those terminal states as experiment outcomes,
not as reasons to relax provenance or silently raise a lane-specific outer
budget.

Deep Agents may truthfully mention built-in workspace operations such as
`grep`, but `HarnessSpecialistFinding.tools_used` is deliberately narrower: it
may list only role-scoped MCP tools actually observed by the adapter. Workspace
operations remain native trace evidence and are not accepted as provenance for
an incident claim. MAF uses the same rule.

LangChain's asynchronous callback manager logs and suppresses exceptions raised
by callback hooks. A budget exception raised from `on_chat_model_start` was
therefore printed to stderr while generation continued, producing a usage/event
mismatch. Hard pre-dispatch budget enforcement now wraps the model's
`_agenerate` call itself; callbacks remain unsuitable as a fail-closed control
boundary.

### Synchronous stdin and long native awaits

The current real-workflow protocol session reads the next command while paused
for approval and again after its terminal message. It does not keep a competing
background `readline` alive during model or MCP awaits: cancelling
`asyncio.to_thread(sys.stdin.buffer.readline)` does not stop the underlying
thread and can make that thread consume a later approval command.

Local usage checks therefore fail before an over-budget model, tool, or
approval dispatch, while wall-clock cancellation during an in-flight native
await still relies on the control plane's terminate/kill fallback. A future
fully graceful cancellation path should use an actual asynchronous pipe reader
or a dedicated single reader that feeds a command queue; do not add multiple
stdin readers.

### Finalize before `run.finished`

Completed bundles require service-owned audit and hidden terminal state, while
`run.finished` must remain the last normalized event. Writing scorer artifacts
after the runner exits would either place `artifact.written` events after the
terminal event or leave files undeclared.

The runner therefore writes an owner-only public workflow snapshot and emits a
terminal-state event, but waits for a `finalize` command. Only then does the
service-side finalizer join the snapshot with the terminal world audit, write
and score the frozen artifacts, and return their canonical digests. The control
plane verifies every file and digest before acknowledging. The runner emits the
artifact events and only then finishes. Hidden state is inspected after native
execution has stopped and is never returned to the model or workflow graph.

## Native tracing and Phoenix

### The OpenTelemetry tracer provider is process-global and effectively one-shot

Observed with OpenTelemetry SDK 1.44.0.

`set_tracer_provider()` establishes global process state and does not provide a
normal replace/reset lifecycle. This is another reason the lane isolation in
ADR 0007 is functional: each runner process installs exactly one provider for
exactly one run, then shuts it down before exit. Tests or future worker pools
must not try to execute multiple independently configured runs in one process.

The provider must be installed before importing and constructing the native
workflow. MAF retrieves its tracers through the global provider; OpenInference
is also passed the same provider explicitly. A manual root span supplies stable
run correlation while framework-native spans retain their own shapes.

### MAF instrumentation is enabled by default and disabling it is sticky

Observed with `agent-framework-core` 1.12.1.

MAF's observability settings default to enabled even when no exporter has been
configured. `disable_instrumentation()` sets a sticky process-local opt-out;
reenabling later requires `enable_instrumentation(force=True)`. The runner calls
the former for tracing-disabled overhead ablations and the latter for traced
runs. It always passes the manifest's sensitive-capture policy explicitly so an
ambient `ENABLE_SENSITIVE_DATA` value cannot broaden capture.

MAF exposes one coarse `enable_sensitive_data` switch rather than independent
prompt, response, and tool-payload controls. Measured profiles therefore keep
it false. The local span ledger applies a second framework-neutral structural
allowlist regardless of that native setting.

### OpenInference needs an explicit content-hiding configuration

Observed with `openinference-instrumentation-langchain` 0.1.68.

Instrumenting LangChain with a tracer provider alone is not sufficient for the
experiment's privacy contract. `TraceConfig` has separate switches for inputs,
outputs, message text, images, prompts, choices, tools, invocation parameters,
and embeddings. The runner sets them explicitly. Outputs and choices stay
hidden even in a future sensitive-input development profile because raw
provider reasoning is prohibited by the manifest schema.

OpenInference patches LangChain's callback-manager initialization in process.
The runner therefore instruments before importing a lane implementation and
calls `uninstrument()` during trace finalization. Process isolation prevents
those wrappers from leaking into the MAF lane.

The fixed setup cost is visible even without a model call. In the 12-repetition
protocol-probe ablation, LangGraph/OpenInference tracing added a median 528 ms
to the runner lifecycle, compared with 107 ms for MAF tracing. This tiny probe
contains only the manual root span, so the result mainly measures imports,
patching, provider setup, and OTLP flush; it is not a per-span cost or a fair
percentage estimate for long agent runs. See
`docs/research/native-tracing-overhead.md` for the method and limitations.

### An OTLP timeout alone is not the clean Phoenix-down path

Observed with the OpenTelemetry OTLP/HTTP exporter 1.44.0.

The HTTP exporter owns retry behavior in addition to its request timeout. To
keep Phoenix auxiliary, the runner performs a bounded loopback TCP check before
constructing the remote exporter. A closed tunnel therefore becomes
`collector_status = "unavailable"` immediately, without an export retry path.
If the port accepts connections but OTLP export still fails, the failure is
bounded by the configured exporter timeout and recorded as `export_failed`.

Neither state fails the agent run. A content-free local `spans.jsonl` ledger is
written independently, and the bundle validator treats collector degradation
as a warning while still failing closed on ledger, correlation, or
completeness tampering.

### MAF Harness model accounting belongs below the function-invocation loop

Observed with `agent-framework-core` 1.12.1.

`OpenAIChatCompletionClient.get_response()` is the public entry to a layered
client. Its `FunctionInvocationLayer` may issue several provider requests while
executing tools, then return one response whose usage is aggregated across the
whole loop. Instrumenting that public method therefore counts agent invocations,
not model dispatches. In the seed-42 stabilization smoke this produced only four
to six normalized model calls for MAF Harness runs whose native ledgers contained
22 to 34 LLM spans (34, 25, 29, and 22 at order seeds 1738–1741).

The harness adapter installs a MAF `ChatMiddleware` instead. That layer executes
below `FunctionInvocationLayer`, immediately around each provider request. It
reserves the outer model-call budget before dispatch and records the individual
response usage afterward. A hermetic two-request tool-loop probe also verifies
that a one-call budget prevents the second wire request.

Bundles captured before this correction remain valid task and provenance
artifacts, and their native span ledgers preserve the underlying LLM operations.
Their normalized MAF Harness model-call count and per-dispatch latency/usage
events must not be used in system-measure comparisons.

### Native operation kinds do not line up one-for-one

Observed in the seed-42 explicit pair with MAF 1.12.1, LangGraph 1.2.10, and
OpenInference LangChain 0.1.68.

OpenInference reports the LangGraph approval node as a generic `CHAIN`, not as
an interrupt or approval kind. Its span name is `approval`, so completeness
classification must apply the stable semantic name before the broad native
kind. OpenInference also emits many internal `CHAIN` spans for runnable
sequences, assignments, fallbacks, and routing functions. Counting every chain
as an agent inflated four role turns into 49 apparent agent operations. Only
role-named chains and native `AGENT` spans are projected as agents.

MAF emits MCP session negotiation spans with `mcp.method.name = initialize` and
`tools/list` in addition to actual `tools/call` spans. Those are valuable native
transport details but are not agent tool dispatches and must not count toward
the normalized tool-call denominator.

Neither native trace contained a checkpoint span even though both adapters
persisted and normalized the approval checkpoint. The completeness matrix keeps
that checkpoint as uncovered rather than adding a synthetic native span. In the
first traced full pair this left checkpoint coverage at zero for both lanes;
that is a real observability result, not a provenance failure.

The runtime context exposed to the normalized adapter also differs. In the
second traced explicit pair, MAF's 57 normalized events correlated across seven
native span IDs, while all 57 LangGraph events carried the shared root span ID.
OpenInference creates its callback spans below the adapter calls but does not
make those callback spans the adapter's active context. Both lanes still share
one verified trace ID per run, but MAF provides finer event-to-span drilldown.
Do not post-hoc guess a LangGraph child span from timing or name alone; retain
that correlation-granularity difference as measured evidence.

### Portable handoffs must be grounded in observable boundaries

MAF Workflow and LangGraph expose different native coordination objects, while
the two harnesses hide still more of their delegation lifecycle inside runtime
state. The normalized adapter therefore does not infer handoffs from span names
or model prose. Explicit lanes record delegation at specialist entry and return
after a validated finding; harness lanes record delegation on the specialist's
first real role-scoped MCP call and return only after the final proposal passes
role/evidence provenance checks. This preserves a comparable coordination
event without pretending the native lifecycle shapes are identical.

The adapter records the stable normalized namespace as `<lane>/<actor>` on
agent, model, tool, metric, and handoff events. OpenInference did not expose a
safe, stable nested namespace attribute in the observed projected spans, so the
adapter does not fabricate deeper graph paths from span names.

Model messages remain content-free structural request/response events. A
completed call also records input/output and optional cached-input/reasoning
token counts as `metric.recorded`; absence stays explicit rather than becoming
zero. Non-streaming calls do not generate synthetic text deltas or timing
estimates; exact queue/TTFT/generation fields are retained only when the
provider response reports them. Raw prompts, messages, tool arguments/results,
and reasoning content remain excluded.
