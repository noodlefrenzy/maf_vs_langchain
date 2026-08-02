# Explicit-workflow parity contract

The MAF Workflows and LangGraph lanes share one logical v0.1 contract even
though their native schedulers, checkpoint stores, and client libraries differ.
`agent_lab.workflow_contracts` owns the schemas, specialist observation plan,
dynamic specialist and commander instructions, deterministic evidence gate,
MCP result parser, structured tool-error normalization, and role-level usage
accounting. Both adapters import those objects directly. Its serialized policy
is frozen as `workflow_contract_sha256` in the contract lock, runner descriptor,
pair preflight, and run bundle.

## Retry and tool-error policy

Each explicit adapter makes one application-level call for each observation in
the shared plan. The adapter does not retry a structured tool failure, including
`transient_timeout`; the simulator's retryable flag remains evidence for later
policies rather than permission for a hidden v0.1 retry. MAF and LangChain expose
MCP `isError` results differently, so `invoke_tool_once` converts both the MAF
exception surface and the LangChain returned-content surface into the same
`WorkflowToolFailure`. The tool boundary is recorded as failed before the error
leaves the role.

MAF's installed MCP transport may reconnect and replay once after a connection
loss. That native transport behavior cannot be disabled through its public
adapter configuration and is preregistered as framework-native resilience under
test, not normalized adapter policy. The prompt therefore says an adapter
requested an observation once while acknowledging that a native transport may
reconnect. Server audit events and the external tool-call budget remain the
authoritative count if a transport replay reaches the service.

Role statistics count model and logical tool dispatches before awaiting a
response, so failed calls are not lost and later attempts accumulate rather than
replace prior usage. External telemetry and server audit remain authoritative
for enforcement.

## Fault and transition matrix

| Case | Required paired outcome | Coverage |
|---|---|---|
| successful fan-out, synthesis, evidence gate | byte-equal normalized packet | deterministic native MAF/LangGraph probes |
| insufficient or ungrounded evidence | shared ordered gate reasons | shared contract guard matrix and protocol classification |
| approval | one interrupt, one authorized action, recovery | deterministic native approved probes |
| rejection | terminal rejection, no mitigation or recovery | deterministic native rejected probes |
| model timeout | `model_failure` after a pre-dispatch model event | workflow protocol fault injection |
| tool timeout | `tool_service_failure`, retryable code retained at adapter boundary | shared native-surface normalization and protocol fault injection |
| invalid tool arguments | `tool_service_failure`, non-retryable code retained at adapter boundary | shared native-surface normalization and protocol fault injection |
| any usage budget | `budget_exhausted` with partial evidence retained | control-plane budget matrix |
| runner/world restart at approval | same request resumes; approved action applies once | paired native framework/world restart test |
| held-constant drift | neither paired lane starts | runner descriptor, workflow-contract, model-policy, runtime, and pair-preflight hash tests |

## Unavoidable native differences

- MAF uses Workflow executors, fan-out/fan-in edges, and native checkpoint
  storage; LangGraph uses state reducers, conditional edges, interrupts, and a
  file-backed saver.
- The MAF MCP transport has the preregistered reconnect behavior above;
  LangChain's installed adapter does not provide the same replay path.
- MAF structured responses are decoded by Agent Framework; LangGraph uses
  ChatOpenAI structured output. Both target the exact same Pydantic classes.
- Framework-native exception wrappers and scheduling spans remain in native
  traces. The normalized terminal taxonomy, budgets, audit provenance, and
  completion contracts are shared.

The v0.1 explicit workflow has no investigation retry loop. The unused
`attempt`/`feedback` fields were removed so a future retry policy must be added
as a new frozen contract rather than silently activating lane-specific state.
