# Qwen3.5-35B-A3B quality-tier gate

Status: staged experimental candidate

This gate tests the quality-tier candidate only after Qwen3.5-9B has passed
hardware/client compatibility but failed the one-seed paired task-quality
operating point. It does not promote the 35B model merely because it starts.

## Frozen candidate

- model: `Qwen/Qwen3.5-35B-A3B-GPTQ-Int4`;
- revision: `3af5ca2972faf6de1fd6f4efc4d8d319ca751e8b`;
- repository size: 24,447,882,201 bytes at acquisition;
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

## Gate sequence

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

The script refuses to overwrite evidence, starts only the pinned offline
snapshot, waits up to 30 minutes for health, and runs the full two-client model
compatibility suite before the paired MAF Workflow/LangGraph task-quality gate.
It always captures the server log and removes only its named 35B container.

## Promotion rule

The model advances to characterization and five-seed profiles only if:

1. the server starts without exceeding the pinned offload and context controls;
2. every native-client compatibility fixture passes;
3. both paired task lanes complete successfully and pass task scoring; and
4. the pair remains measurement-eligible under identical source/runtime
   provenance.

Startup, throughput, or success in only one framework is not sufficient.
