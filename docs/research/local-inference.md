# Local inference research

Research cutoff: 2026-07-31.

## Detected host

- WSL Linux environment
- NVIDIA GeForce RTX 4090
- 24,564 MiB reported VRAM
- 128 GB host RAM reported by the user
- NVIDIA driver 610.74
- Python 3.12.12
- Docker 29.5.2
- `uv` 0.9.26

These facts make one all-GPU medium model or a larger quantized model with CPU
offload plausible. They do not establish useful throughput or context length.

## Shared serving protocol

Use one OpenAI-compatible Chat Completions endpoint:

- Microsoft documents `OpenAIChatCompletionClient(base_url=...)` for vLLM,
  Ollama, and LM Studio. [MAF OpenAI-compatible
  endpoints](https://learn.microsoft.com/en-us/agent-framework/integrations/openai-endpoints?tabs=dotnet-cli%2Cuser-secrets)
- LangChain documents an OpenAI provider with custom `base_url`, including
  vLLM. [LangChain models](https://docs.langchain.com/oss/python/langchain/models)

Chat Completions is selected over Responses because it currently has the
broadest local-server/tool-call compatibility in both stacks. Both runners
translate one shared `ModelProfile` into their native client construction.

## Cross-framework compatibility gate

Run the hermetic gate before starting or changing a local inference server:

```bash
uv run python -m agent_lab.model_compat_suite \
  --output runs/model-compatibility/stub.json
```

The command starts a loopback-only deterministic Chat Completions stub and runs
the installed `OpenAIChatCompletionClient` and `ChatOpenAI` clients through the
same plain, structured, streaming, one-tool, parallel-tool, multi-turn, and
malformed-tool fixtures, plus plain dispatch probes for workflow specialists,
workflow commanders, harness specialists, and harness commanders. The report
contains redacted request and response
shapes, wire and effective-request SHA-256 hashes, server-default expansion,
explicit temperature, top-p, top-k, min-p, presence and repetition penalties,
thinking mode, seed, output/context ceilings, timeout settings, normalized usage
including reasoning tokens, and package versions. It classifies failures at the
model parser, client serialization, framework tool loop, or transport layer.

`agent_lab.model_request_policy` is the only supported profile-to-request
mapping. Both adapter families consume it directly. A complete per-actor policy
and its canonical SHA-256 are written into pair preflight evidence; the same hash
is frozen into each manifest and acknowledged by the runner before execution.
Adding a `ModelProfile` field without mapping it in both adapters fails closed.

Measured native runs also collect a shared `execution-provenance/v1` preflight
attestation before either lane starts. For llama.cpp this binds the requested
model to the advertised model ID, immutable Hugging Face snapshot revision and
GGUF artifact, tokenizer metadata, chat-template hash and capabilities, server
build, parser modes, endpoint and process serve flags, context/slot settings,
and identity-bearing endpoint-payload hashes. Volatile response timestamps and
sleep state are excluded from held-constant hashes. Compute identity is collected on the configured
serving host, not the machine owning an SSH tunnel; the lonestar profiles record
its kernel plus Vulkan device, RADV/Mesa driver, and Vulkan runtime. Missing or
ambiguous identity, a model mismatch, or a served context smaller than the
profile fails preflight.

The stub is the CI path and does not install or resolve dependencies. Live mode
first reads `/v1/models` and refuses to send fixture prompts unless the exact
requested model identifier is advertised. To run the same native client
fixtures against an already-running loopback vLLM server:

```bash
uv run python -m agent_lab.model_compat_suite \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3.5-9B \
  --server-kind vllm \
  --output runs/model-compatibility/vllm.json
```

Live mode records behavioral and client-side usage results. It marks the lack of
endpoint-side wire/default evidence as a gap; retain the corresponding vLLM
request log with the report when accepting a model profile. LangChain's parsed
`with_structured_output` surface does not expose its raw usage object unless raw
output is requested, so the report records that per-fixture accounting gap
instead of silently treating it as zero.

### Aligned lonestar smoke profile

Development smoke tests use the already-cached aligned
`unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL` served directly by
llama.cpp on `lonestar:8080`. The LiteLLM router is deliberately bypassed so a
marker or routing failure cannot send experiment traffic to a cloud model.
Create a loopback tunnel before running the smoke profile:

```bash
ssh -N -L 8000:127.0.0.1:8080 lonestar

uv run --no-sync python -m agent_lab.model_compat_suite \
  --base-url http://127.0.0.1:8000/v1 \
  --model unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL \
  --server-kind llama_cpp \
  --output runs/model-compatibility/lonestar-aligned-qwen3-next-80b.json
```

This is the active smoke-development profile, not a replacement for the
completed RTX 4090 vLLM characterization or the final paired-study model
selection. Both framework lanes must still use the same profile within a pair.

## Fit-check model: Qwen3.5-9B

Qwen3.5-9B:

- is a 9B-class reasoning-capable model with tool-use instructions;
- has a native 262,144-token context in the model card;
- is about 19.3 GB in its official BF16 repository;
- supports vLLM tool parsing with `qwen3_coder` and reasoning parsing with
  `qwen3`;
- can skip vision profiling with `--language-model-only`;
- currently requires a recent/main-branch or nightly vLLM according to Qwen.

[Official Qwen3.5-9B model card](https://huggingface.co/Qwen/Qwen3.5-9B)

The model card's maximum context is not a promise that 262K fits on this GPU.
The 32K profile is a deliberate fit-test compromise. Peak VRAM, effective
KV-cache capacity, and client compatibility through long tool histories are
recorded in the completed characterization; task-pilot quality remains a
separate model-selection gate.

Qwen recommends thinking-mode sampling parameters for general tasks around
temperature 1.0, top-p 0.95, and top-k 20. The first profile follows those
values; framework adapters must pass any extra supported parameters explicitly
and record the actual request. [Official Qwen3.5-9B model
card](https://huggingface.co/Qwen/Qwen3.5-9B)

The pinned official BF16 weights did not leave usable KV cache on the 24 GB
device, and bounded CPU offload failed in the pinned Qwen GDN/Triton path. The
accepted profile instead loads the same official safetensors with the image's
quarantined BitsAndBytes 4-bit support and BF16 compute. It served 8K, 16K, and
32K contexts at concurrency two with peak whole-GPU allocation between 22,504
and 22,924 MiB across the pre/post-restart runs. Cancellation and
malformed-history recovery passed at every context, and both native framework
clients passed the full 32K compatibility gate before and after restart. See the
[executable runbook and measured results](qwen35-9b-characterization.md) for
the frozen identities, throughput matrix, restart comparison, and evidence
hashes. This establishes fit and compatibility, not sufficient task quality
for the final paired study.

## Quality-tier candidate: Qwen3.5-35B-A3B GPTQ Int4

The 35B-A3B model activates about 3B parameters per token and the official
GPTQ-Int4 repository is about 24.5 GB. That repository size already approaches
the entire 4090 VRAM before runtime state and KV cache. It is therefore an
experimental CPU-offload profile, not the default.
[Official Qwen3.5-35B-A3B model
card](https://huggingface.co/Qwen/Qwen3.5-35B-A3B),
[official GPTQ-Int4
files](https://huggingface.co/Qwen/Qwen3.5-35B-A3B-GPTQ-Int4/tree/main)

The available 128 GB host RAM makes bounded offload possible, but PCIe transfer,
kernel support, latency, and context capacity must be characterized. MoE active
parameter count does not reduce the need to store all expert weights.

The staged gate now pins official revision
`3af5ca2972faf6de1fd6f4efc4d8d319ca751e8b`, loopback port 8002, a 16K
context, `moe_wna16`, 75% GPU-memory utilization, and 8 GiB of CPU offload. See the
[executable quality-tier gate](qwen35-35b-a3b-gate.md). These are candidate
controls until the server and both native clients pass; they are not a selected
measured-study operating point.

## Why not pick the largest model immediately

The experiment needs many model calls and repetitions. A slower model can:

- make the measured study impractically long;
- amplify queueing when subagents run in parallel;
- change timeouts and recovery behavior;
- force a context so small that harness compaction dominates;
- turn framework comparison into quantization/offload comparison.

Model selection should use a two-stage gate:

1. **Compatibility**: valid sequential/parallel/multi-turn tool calls through
   both framework clients, stable reasoning/tool parsing, structured output,
   streaming, and cancellation.
2. **Operating point**: acceptable task pilot quality at measured throughput,
   VRAM, RAM, context, and thermal behavior.

## Required characterization matrix

The executed, pinned protocol for the fit-check is in
[`qwen35-9b-characterization.md`](qwen35-9b-characterization.md). It separates
artifact acquisition from offline measured runs and reserves port 8001 so the
lonestar development tunnel on port 8000 is unaffected.

| Dimension | Values |
| --- | --- |
| Model | Qwen3.5-9B BitsAndBytes 4-bit with BF16 compute; BF16 and CPU offload rejected; 35B-A3B GPTQ Int4 remains experimental |
| Context | 8K, 16K, 32K; larger only if it fits |
| Concurrency | 1 and 2 in-flight requests |
| Prompt type | plain reasoning, one tool, multi-step tools, parallel tools, malformed tool recovery |
| State | cold model, warm model, warm prefix cache |
| Output | 512, 2K, and 8K token caps |

Record time to first token, decode throughput, end-to-end latency, peak VRAM,
host RAM, GPU utilization, parser failures, tool-call validity, and
reasoning-content behavior.

## Reproducibility and WSL notes

- Pin the exact model revision, not only the repository name.
- Pin the vLLM wheel/image digest after the Qwen3.5 compatibility test.
- Record CUDA, driver, PyTorch, tokenizer, and chat-template revisions.
- Bind the endpoint to loopback.
- Keep the Hugging Face cache outside Git and record weight digests.
- Treat the requested inference seed as best effort; GPU kernels and scheduling
  may remain nondeterministic.
- Test after a full WSL/Docker restart before accepting the profile.

## Alternatives retained

- Ollama or llama.cpp may make aggressive quantization/offload easier, but
  vLLM is the first choice because Qwen documents native reasoning and tool
  parsers and because concurrent agent throughput matters.
- Foundry Local would be natural for MAF but would not be a neutral serving
  layer for LangChain.
- A hosted model is useful only as a diagnostic ceiling, not part of the
  primary local comparison.
