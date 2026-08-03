# Qwen3.5-35B-A3B quality-tier gate

Status: rejected on the current single-RTX-4090/pinned-vLLM topology

This record preserves the quality-tier candidate tested after Qwen3.5-9B
passed hardware/client compatibility but failed the one-seed paired
task-quality operating point. The 35B model did not reach server health, so no
native-client compatibility or task-quality run was started.

## Frozen candidate

- model: `Qwen/Qwen3.5-35B-A3B-GPTQ-Int4`;
- revision: `3af5ca2972faf6de1fd6f4efc4d8d319ca751e8b`;
- repository size: 24,447,882,201 bytes at acquisition;
- cached snapshot aggregate SHA-256:
  `f8335ad5d64105f3d53f2952b6db133c93b6811ebccd507cf2d21dbf97ecc97c`;
- server image: `vllm/vllm-openai@sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9`;
- text-only Chat Completions on loopback port 8002;
- 16,384-token served context, two sequences, and 8,192 batched tokens;
- BF16 compute over official GPTQ Int4 weights with `moe_wna16`;
- 75% GPU-memory target and exactly 8 GiB of bounded CPU offload, leaving room
  for the host's observed WSL/display reservation;
- Qwen3 reasoning and `qwen3_coder` tool-call parsers.

The [official model card](https://huggingface.co/Qwen/Qwen3.5-35B-A3B-GPTQ-Int4)
documents 35B total/3B activated parameters, the GPTQ artifact, vLLM
`moe_wna16`, the parser flags, and text-only serving. The host-specific offload,
context, and concurrency values above are experiment controls, not vendor
recommendations.

## Acquisition

Artifact acquisition is separate from measurement and pins an immutable model
revision:

```bash
export AGENT_LAB_HF_CACHE=/home/noodle/.cache/huggingface

docker run --rm --pull never \
  --entrypoint /usr/bin/python3 \
  --volume "$AGENT_LAB_HF_CACHE:/root/.cache/huggingface" \
  vllm/vllm-openai@sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9 \
  -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3.5-35B-A3B-GPTQ-Int4', revision='3af5ca2972faf6de1fd6f4efc4d8d319ca751e8b')"
```

## Executed gate sequence

The 35B server cannot coexist with the 9B GPU server. Stop and remove only the
named 9B container, then run the fail-closed gate from a clean committed tree:

```bash
docker stop --time 30 agent-lab-qwen35-9b-vllm
docker rm agent-lab-qwen35-9b-vllm

export AGENT_LAB_HF_CACHE=/home/noodle/.cache/huggingface
scripts/run-qwen35-35b-a3b-vllm-gate.sh \
  runs/model-compatibility/qwen35-35b-a3b-gate \
  runs/qwen35-35b-a3b-gate
```

The script refused to overwrite evidence, started only the pinned offline
snapshot, and failed closed while waiting for server health. It captured the
server log and removed only its named 35B container. The ignored local log is
`runs/model-compatibility/qwen35-35b-a3b-gate/server.log`, with SHA-256
`aa2a15cbbb504df8121ed925f74ee67c9cced27c4688096da6175fa31aa9f07e`.

All 14 shards loaded in about 46 seconds, and vLLM reported 12.12 GiB of GPU
model memory after offloading 8.03 GiB. Engine initialization then failed while
profiling the cache in the Qwen GDN prefill path:

```text
ValueError: Pointer argument cannot be accessed from Triton (cpu tensor?)
```

This happened after weight loading and before the health endpoint became
available; it was not a task or client failure.

## Minimal-fit diagnostic

A second diagnostic tested the smallest potentially useful variant without
changing the frozen gate:

- 8,192-token context;
- one sequence and 4,096 batched tokens;
- 75% GPU-memory target and 2 GiB CPU offload;
- eager execution, disabling `torch.compile` and CUDA graphs;
- every other model, image, revision, quantization, and parser identity held
  equal.

Again all 14 shards loaded. vLLM reported 18.02 GiB of GPU model memory after
offloading 2.13 GiB. Initialization failed with the same pointer error, this
time in the Triton WNA16 MoE kernel. The ignored diagnostic log is
`runs/model-compatibility/qwen35-35b-a3b-fit/server.log`, with SHA-256
`44089c51a21ed7e4021b539cc373b1171a32d7796a450febc790c82ca603d637`.

The two failures show that generic CPU offload is not compatible with the
required Qwen GDN and WNA16 Triton kernels in this pinned stack. Removing the
last 2.13 GiB of offload would bring model allocation to roughly 20.15 GiB,
essentially consuming the VRAM available to WSL before KV cache and runtime
headroom. A still smaller context would not be a useful experiment operating
point.

## Gate disposition

The model would have advanced to characterization and five-seed profiles only
if:

1. the server starts without exceeding the pinned offload and context controls;
2. every native-client compatibility fixture passes;
3. both paired task lanes complete successfully and pass task scoring; and
4. the pair remains measurement-eligible under identical source/runtime
   provenance.

Startup, throughput, or success in only one framework is not sufficient. The
first condition failed, so the candidate is rejected for this host and server
stack. The staged script and configs remain solely as an exact, fail-closed
reproduction of the attempted gate; they are not an approved experiment
profile. No 35B compatibility or task result exists and no larger profile is
promoted.
