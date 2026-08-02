# Qwen3.5-9B RTX 4090 characterization runbook

Status: implementation ready; measurements pending local artifact acquisition.

This runbook is the execution contract for issue 13. It deliberately uses port
`8001`; port `8000` remains available for the `lonestar` SSH tunnel.

## Frozen inputs and quarantine evidence

Registry timestamps were checked in UTC on 2026-08-02 at 04:23 UTC, before any
image pull or model download:

| Input | Immutable identity | Authoritative timestamp | Age at check |
| --- | --- | --- | --- |
| vLLM amd64 nightly | commit `6f91edf96d3f3272945809c04702380053bff4de`, manifest `sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9` | Docker Hub `last_pushed` 2026-07-29 06:13:56 UTC | about 94 hours |
| Qwen3.5-9B | revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a` | Hugging Face `lastModified` 2026-03-02 00:51:43 UTC | about 153 days |

The selected vLLM manifest is the linux/amd64 child of multi-architecture tag
`nightly-6f91edf96d3f3272945809c04702380053bff4de`. The Docker Hub repository
reported its compressed size as 8,738,929,920 bytes. Hugging Face reported the
model repository at 19,319,118,862 bytes. A measured run uses `--pull never`,
`HF_HUB_OFFLINE=1`, and `TRANSFORMERS_OFFLINE=1`; missing artifacts fail closed.

## One-time artifact acquisition

Acquisition is intentionally separate from measurement. Set an explicit cache
path with sufficient free space:

```bash
export AGENT_LAB_HF_CACHE=/absolute/path/to/huggingface-cache
mkdir -p "$AGENT_LAB_HF_CACHE"

docker pull \
  vllm/vllm-openai@sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9

docker run --rm --pull never \
  --entrypoint /usr/bin/python3 \
  --volume "$AGENT_LAB_HF_CACHE:/root/.cache/huggingface" \
  vllm/vllm-openai@sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9 \
  -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3.5-9B', revision='c202236235762e1c871ad0ccb60c8ee5ba337b9a')"
```

Do not substitute a tag, `latest`, or a newer nightly. Any replacement artifact
needs a fresh authoritative publication-time check and must be strictly older
than 72 hours.

## Measurement protocol

Start from a clean, committed worktree. Each invocation creates a fresh server,
waits for health, verifies the execution-provenance contract, runs one context
slice, writes a mode-0600 report without overwriting prior evidence, and stops
only its named container:

```bash
export AGENT_LAB_HF_CACHE=/absolute/path/to/huggingface-cache

scripts/run-qwen35-vllm-characterization.sh \
  8192 before_restart runs/model-characterization/qwen35-9b-8192-before.json
scripts/run-qwen35-vllm-characterization.sh \
  16384 before_restart runs/model-characterization/qwen35-9b-16384-before.json
scripts/run-qwen35-vllm-characterization.sh \
  32768 before_restart runs/model-characterization/qwen35-9b-32768-before.json
```

For each context, the report contains:

- a zero-prior-request cold gate from vLLM metrics;
- cold-first-request, warm-model, and repeated-prefix TTFT;
- output caps 512, 2,048, and 8,192 where the prompt plus cap fits;
- one and two simultaneously in-flight requests;
- per-request decode throughput and batch aggregate throughput;
- whole-GPU memory/utilization, whole-host memory, and container memory;
- client-disconnect cancellation followed by server drain and health proof; and
- source, model revision, tokenizer config/template, vLLM/PyTorch/CUDA stack,
  image manifest/content digests, parser selection, and every serving flag.

The 32K invocation also writes a sibling `*-compat.json` report by exercising
the installed MAF and LangChain clients with the exact model profile. It runs
plain, streaming, strict structured, one-tool, parallel-tool, multi-turn, and
parser fixtures after the performance matrix. The characterization report's
raw malformed-history probe separately requires a corrected JSON tool call.

The 8,192-output cap is structurally skipped for an 8,192 total context because
it leaves no usable prompt. It is measured at 16K and 32K. Prompt sizes are
calibrated with the running server's `/tokenize` endpoint and recorded rather
than estimated with a different tokenizer.

After all three reports, fully shut down WSL and Docker from Windows, start them
again, and restore any unrelated tunnel separately. Then repeat with
`after_restart` and distinct output paths:

```bash
scripts/run-qwen35-vllm-characterization.sh \
  8192 after_restart runs/model-characterization/qwen35-9b-8192-after.json
scripts/run-qwen35-vllm-characterization.sh \
  16384 after_restart runs/model-characterization/qwen35-9b-16384-after.json
scripts/run-qwen35-vllm-characterization.sh \
  32768 after_restart runs/model-characterization/qwen35-9b-32768-after.json
```

The reports are not accepted merely because generation succeeds. The final gate
also requires both native framework clients to pass streaming, strict structured
output, sequential and parallel tools, multi-turn tool execution, malformed-call
recovery, and the exact effective request policy against the 32K server.

## Serving contract

The script and preflight require BF16 safetensors, one tensor-parallel worker,
two maximum sequences, explicit batched-token/context ceilings, generation
defaults disabled (`--generation-config vllm`), Qwen `qwen3` reasoning and
`qwen3_coder` tool parsers, text-only serving, chunked prefill, automatic tool
choice, prefix caching, usage details, per-request timing metrics, tokenizer
identity exposure, and the requested seed. Docker publishes the server only as
`127.0.0.1:8001`; the container's internal wildcard bind is accepted only when
Docker inspection proves that exact loopback publication.
