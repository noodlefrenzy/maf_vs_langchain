# Inference profiles

## Active smoke-development profile

Smoke development uses the aligned
`unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF:UD-Q4_K_XL` already served by
llama.cpp on `lonestar:8080`. Expose that direct endpoint locally without going
through the LiteLLM router:

```bash
ssh -N -L 8000:127.0.0.1:8080 lonestar
```

The smoke experiment resolves
`configs/models/qwen3_next_80b_lonestar_llamacpp.toml`, whose exact model ID is
checked against `/v1/models` by the live compatibility gate before any fixture
prompt is sent. This profile supports implementation smoke tests without a new
model download. It is not yet the accepted measured-study model.

## RTX 4090 characterization candidate

Both framework lanes call the same OpenAI-compatible Chat Completions endpoint.
The initial fit-check model is `Qwen/Qwen3.5-9B` in text-only mode. It is a
reasoning and tool-calling model whose BF16 repository is about 19.3 GB, making
it a plausible all-GPU starting point on the detected 24 GB RTX 4090. This is
an estimate, not a benchmark result.

Qwen's current vLLM guidance requires a recent/nightly vLLM for Qwen3.5. The
proposed first command is:

```bash
vllm serve Qwen/Qwen3.5-9B \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 32768 \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --language-model-only
```

Do not treat this command as frozen until the hardware characterization issue
records:

- exact vLLM and CUDA versions;
- peak VRAM at 8K, 16K, and 32K contexts;
- cold and warm time-to-first-token;
- tokens/second with one and two concurrent agents;
- multi-step and parallel tool-call correctness;
- preservation of tool calls after reasoning output;
- WSL shutdown/restart reproducibility.

The quality-tier candidate is
`Qwen/Qwen3.5-35B-A3B-GPTQ-Int4`. Its repository is about 24.5 GB before
runtime overhead, so it cannot be assumed to fit wholly in VRAM. The staged
gate pins 8 GiB of CPU offload, a 16K context, and loopback port 8002; it runs
native-client compatibility before task quality and does not coexist with the
9B GPU server. Follow
[`qwen35-35b-a3b-gate.md`](../../docs/research/qwen35-35b-a3b-gate.md).

See [ADR 0004](../../docs/adr/0004-local-inference.md) for the decision and
[local-model research](../../docs/research/local-inference.md) for sources.
