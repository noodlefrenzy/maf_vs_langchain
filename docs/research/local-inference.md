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
malformed-tool fixtures. The report contains redacted request and response
shapes, wire and effective-request SHA-256 hashes, server-default expansion,
explicit sampling/output/timeout settings, normalized usage including reasoning
tokens, and package versions. It classifies failures at the model parser,
client serialization, framework tool loop, or transport layer.

The stub is the CI path and does not install or resolve dependencies. To run the
same native client fixtures against an already-running loopback vLLM server:

```bash
uv run python -m agent_lab.model_compat_suite \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3.5-9B \
  --output runs/model-compatibility/vllm.json
```

Live mode records behavioral and client-side usage results. It marks the lack of
endpoint-side wire/default evidence as a gap; retain the corresponding vLLM
request log with the report when accepting a model profile. LangChain's parsed
`with_structured_output` surface does not expose its raw usage object unless raw
output is requested, so the report records that per-fixture accounting gap
instead of silently treating it as zero.

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
The initial 32K profile is a deliberate fit-test compromise. Peak VRAM,
effective KV-cache capacity, and agent quality after long tool histories must
be measured.

Qwen recommends thinking-mode sampling parameters for general tasks around
temperature 1.0, top-p 0.95, and top-k 20. The first profile follows those
values; framework adapters must pass any extra supported parameters explicitly
and record the actual request. [Official Qwen3.5-9B model
card](https://huggingface.co/Qwen/Qwen3.5-9B)

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

| Dimension | Values |
| --- | --- |
| Model | Qwen3.5-9B BF16; 35B-A3B GPTQ Int4 if viable |
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
