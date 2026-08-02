# Qwen3.5-9B RTX 4090 characterization runbook

Status: pre-restart 8K/16K/32K characterization accepted; full WSL/Docker
restart reproduction pending.

This runbook is the execution contract for issue 13. It deliberately uses port
`8001`; port `8000` remains available for the `lonestar` SSH tunnel.

## Frozen inputs and quarantine evidence

Registry timestamps were checked in UTC on 2026-08-02 at 04:23 UTC, before any
image pull or model download:

| Input | Immutable identity | Authoritative timestamp | Age at check |
| --- | --- | --- | --- |
| vLLM amd64 nightly | commit `6f91edf96d3f3272945809c04702380053bff4de`, manifest `sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9` | Docker Hub `last_pushed` 2026-07-29 06:13:56 UTC | about 94 hours |
| Qwen3.5-9B | revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a` | Hugging Face `lastModified` 2026-03-02 00:51:43 UTC | about 153 days |
| BitsAndBytes | version `0.50.0`, already contained in the pinned image | PyPI earliest file upload 2026-07-24 19:48:51 UTC | about 250 hours |

The selected vLLM manifest is the linux/amd64 child of multi-architecture tag
`nightly-6f91edf96d3f3272945809c04702380053bff4de`. The Docker Hub repository
reported its compressed size as 8,738,929,920 bytes. Hugging Face reported the
model repository at 19,319,118,862 bytes. A measured run uses `--pull never`,
`HF_HUB_OFFLINE=1`, and `TRANSFORMERS_OFFLINE=1`; missing artifacts fail closed.

## Fit observations and selected candidate

The first 8K startup attempt on 2026-08-02 used vLLM's 0.90 GPU-memory
reservation. The immutable image and model loaded successfully, with 16.8 GiB
reported for model loading. During compilation and warmup, observed whole-GPU
allocation peaked at approximately 23.2 GiB while unrelated processes retained
approximately 4.14 GiB after the container exited. The server exited with code
1 before becoming healthy, so no benchmark report was produced. The original
ephemeral-container launcher removed the final server log, preventing a more
specific failure classification.

An 8K BF16 follow-up with a 0.75 reservation reported -3.50 GiB available for
KV cache. Reducing the chunked-prefill ceiling from 8,192 to 1,024 tokens and
raising the reservation to 0.83 still reported -1.31 GiB. The BF16 profile is
therefore rejected on this 24 GB device: it cannot leave any KV cache, much less
the useful capacity required for two in-flight requests. A 4 GiB CPU-weight
offload reduced model loading from 16.8 to 12.77 GiB, but vLLM's Qwen GDN
warmup rejected the resulting CPU tensor in a Triton kernel. CPU offload is
also rejected for this pinned stack.

The selected candidate dynamically loads the same immutable official
safetensors through the pinned image's BitsAndBytes 0.50.0 support. Its 8K fit
probe loaded the model in 7.56 GiB and became healthy with 8.07 GiB of KV cache,
186,181 token slots, and a reported maximum 22.73 concurrent 8K requests. A
short Chat Completions request also completed without a server error. The
measured profile uses BF16 compute, in-flight 4-bit weight quantization, a 0.75
GPU-memory reservation, and an 8,192-token chunked-prefill ceiling at every
context size. No package or model artifact was installed for this change;
BitsAndBytes was already inside the quarantined image.

The first benchmark request then exposed that this vLLM build streams thinking
under the OpenAI delta field `reasoning`, while the initial measurement client
recognized only `reasoning_content`. The client now treats either field as a
generated-token event for TTFT and throughput without retaining its text.
The next run also showed that the cancellation probe's configured 8,192-token
cap was added to its calibrated 2,048-token prompt at the 8K slice. Cancellation
now records and caps generation to the exact remaining served context.
Finally, vLLM correctly rejected invalid JSON embedded in a historical OpenAI
`tool_calls` object before inference. The recovery fixture now mirrors the
reachable client path: preserve the malformed output as inert transcript text,
record the parse failure in the next user correction, and require one valid
replacement tool call.

The launcher preserves the complete vLLM log beside each report as
`*-server.log` before removing only its named container. Reports and logs use
mode-0600 creation permissions and are never overwritten.

## Pre-restart results

All three pre-restart reports were collected on 2026-08-02 from clean source
commit `cc85017d74e710c2b8b1505e1a645c582644532b`. Their execution-provenance
attestations were complete and each report set `measurement_eligible` to true.
Times are seconds. Resource peaks are whole-device or whole-host observations,
not increments over the recorded baseline.

The accepted pre-restart identity is:

| Component | Recorded identity |
| --- | --- |
| Model and tokenizer revision | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` |
| Tokenizer config SHA-256 | `bcdef5ac54ccaeb41edf18fa52115d579bdbe7fb8e4a25216973c0a5108ce882` |
| Tokenizer identity SHA-256 | `4131d7f0092daa3bbae86865c2e9f180268c8188676fa1e502ac6c302f1d4daa` |
| Chat-template SHA-256 | `a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715` |
| Image content digest | `sha256:c68a8de36a9de31b9453ba051031366c55d3379d63896381da9a82641f91e475` |
| Server/runtime | vLLM `0.26.1rc1.dev77+g6f91edf96`; PyTorch `2.13.0+cu130`; CUDA `13.0`; BitsAndBytes `0.50.0` |
| Reasoning/tool parsers | `qwen3` / `qwen3_coder`, automatic tool choice enabled |
| Serve-command SHA-256, 8K / 16K / 32K | `accc8c0f4a959c2ff01fab1e8a88b74f4b7acd6655ffef342aa270aa6b4d7a25` / `38b8a6d56015032f9445eb80cd5c0e0c469e1609b92e1bf6145e420ff8f4b577` / `cc826176233c623fe7ad3c479dd40b86ca4aecb598e4eb53192933b46ab0968d` |
| Attestation SHA-256, 8K / 16K / 32K | `7daee2b8cde68f2f4a1c7b433e542f5ac325dfb81403f267c1df4e58b3aaa722` / `ddb1187685d5d9d1ac65e921c5bff13ddd07de46bced9fd9d10f23ad85560f15` / `8ef458f61d2872701a747d5eb3f4fa743dc2b631a142aebe562691b2e89485ae` |

| Served context | Cold TTFT | Warm TTFT | Prefix-cache TTFT | Peak GPU MiB | Peak GPU use | Peak host MiB | Peak container MiB |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8,192 | 1.173 | 0.740 | 0.150 | 22,504 | 97% | 13,476 | 4,065 |
| 16,384 | 1.923 | 1.489 | 0.171 | 22,773 | 100% | 13,546 | 4,079 |
| 32,768 | 3.860 | 3.026 | 0.207 | 22,924 | 100% | 13,563 | 4,086 |

The matrix below reports aggregate generated tokens divided by batch wall time,
so the one-request column includes TTFT. Request-local decode after the first
visible token was approximately 96--103 tokens/second for the completed
single-request samples.

| Served context | Output cap | One in flight, tok/s | Two in flight, tok/s | Completion observation |
| ---: | ---: | ---: | ---: | --- |
| 8,192 | 512 | 90.12 | 168.25 | all reached cap |
| 8,192 | 2,048 | 99.53 | 172.85 | all reached cap |
| 16,384 | 512 | 77.78 | 163.29 | all reached cap |
| 16,384 | 2,048 | 80.53 | 167.33 | model stopped at 578 tokens for one single sample and 2,001 tokens for one concurrent sample |
| 16,384 | 8,192 | 99.54 | 168.64 | all reached cap |
| 32,768 | 512 | 61.16 | 152.80 | all reached cap |
| 32,768 | 2,048 | 86.91 | 159.19 | all reached cap |
| 32,768 | 8,192 | 95.10 | 146.91 | single reached cap; concurrent samples stopped at 5,620 and 4,586 tokens |

The 8K report structurally skipped the 8,192-token output cap because it would
leave fewer than 128 prompt tokens. At every context, the disconnect probe
observed a client cancellation, a drained server, and a subsequent healthy
endpoint. The vLLM abort counter did not increment, so each report explicitly
retains that counter-evidence gap rather than claiming counter confirmation.
Malformed-history recovery produced exactly one corrected
`lookup_incident(INC-42)` call at every context.

The initial 32K native compatibility gate exposed an overly strict verifier:
both frameworks completed the tool loop but returned boundary newlines around
`multi-turn complete`. PR #48 normalizes only surrounding whitespace and
retains the unmodified text as `raw_value`. The exact-profile live rerun then
passed with `errors: []` for both MAF and LangChain across plain, streaming,
strict structured output, sequential and parallel tool calls, multi-turn tool
execution, and every actor header probe. The benchmark's separate malformed
history fixture remains the positive recovery proof because a live model is not
expected to emit intentionally invalid JSON on demand.

Local evidence is intentionally ignored by Git and remains mode 0600. These
hashes identify the pre-restart reports and the accepted compatibility rerun:

| Evidence | SHA-256 |
| --- | --- |
| `qwen35-9b-8192-before.json` | `0a41d85115ca850d793615385945ea80081c02a72940b1a3d07dc4c52e9a76b9` |
| `qwen35-9b-16384-before.json` | `542b3fbf9185373477c261cb098bbfc7c1275501c97468465f072e456f1ba361` |
| `qwen35-9b-32768-before.json` | `ec1d1cd22501dc8d16e661ae506e53ea9713cdc9d24c22fbd58ef3436b910275` |
| `qwen35-9b-32768-before-compat-after-whitespace-fix.json` | `abd5f5f957700fd25ffea9d537f931065c8f12ab5db9ae06ab2f845ed09c4375` |

The corresponding server logs have SHA-256 values
`e963f5cb611a452201f44a86b587c091eb47a9b3e009941c8787b3ffef07d623`,
`4b55ba6c74863c6b1f230cdb6f20cecd20bf0c846250c6c8c3ef65d30b3b54f8`,
`850b5a348c1ae7a574ba1b098a68f2a3fa4a15dc40888e22f350fe2a384bfbb7`,
and `f64acccd3b1c6c1b525991949dfca4b94a4f178276142ad4f85abd38626884ca`
in the same table order.

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
slice, writes mode-0600 evidence without overwriting prior files, preserves the
server log, and stops and removes only its named container:

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

The script and preflight require official safetensors dynamically loaded with
BitsAndBytes 4-bit weights and BF16 compute, one tensor-parallel worker, two
maximum sequences, a 0.75 GPU-memory reservation, an 8,192-token
chunked-prefill ceiling, explicit context ceilings, generation
defaults disabled (`--generation-config vllm`), Qwen `qwen3` reasoning and
`qwen3_coder` tool parsers, text-only serving, chunked prefill, automatic tool
choice, prefix caching, usage details, per-request timing metrics, tokenizer
identity exposure, and the requested seed. Docker publishes the server only as
`127.0.0.1:8001`; the container's internal wildcard bind is accepted only when
Docker inspection proves that exact loopback publication.
