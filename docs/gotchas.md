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
affected by the process umask. The MAF runner wraps the storage backend and
changes every saved checkpoint to mode `0600`; the checkpoint directory is
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

The live approval controller and runner use a two-phase JSON-lines protocol:
the runner emits one approval request, consumes one decision, and emits one
terminal payload. If the runner exits before phase one, `stdout.readline()`
returns EOF (`b""`), not a structured error. The controller must collect and
surface child stderr before attempting `json.loads`; otherwise the useful
framework traceback is replaced by an unrelated JSON decoding error.

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
