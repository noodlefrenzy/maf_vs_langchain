#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly CONTAINER_NAME="agent-lab-qwen35-35b-a3b-vllm"
readonly IMAGE="vllm/vllm-openai@sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9"
readonly MODEL="Qwen/Qwen3.5-35B-A3B-GPTQ-Int4"
readonly REVISION="3af5ca2972faf6de1fd6f4efc4d8d319ca751e8b"
readonly PORT="8002"
readonly CONTEXT_TOKENS="16384"
readonly MAX_BATCHED_TOKENS="8192"
readonly CPU_OFFLOAD_GB="8"

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <evidence-directory> <runs-directory>" >&2
  exit 2
fi

readonly EVIDENCE_DIR="$1"
readonly RUNS_DIR="$2"
readonly COMPAT_OUTPUT="${EVIDENCE_DIR}/model-compatibility.json"
readonly SERVER_LOG="${EVIDENCE_DIR}/server.log"

if [[ -z "${AGENT_LAB_HF_CACHE:-}" || ! -d "$AGENT_LAB_HF_CACHE" ]]; then
  echo "AGENT_LAB_HF_CACHE must name the existing Hugging Face cache directory" >&2
  exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "pinned vLLM image is not local" >&2
  exit 2
fi
if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "container $CONTAINER_NAME already exists; refusing to replace it" >&2
  exit 2
fi
if docker container inspect agent-lab-qwen35-9b-vllm >/dev/null 2>&1; then
  echo "the 9B vLLM container must be stopped and removed before the 35B gate" >&2
  exit 2
fi
if [[ -e "$EVIDENCE_DIR" ]]; then
  echo "evidence directory already exists; refusing to overwrite it" >&2
  exit 2
fi

mkdir -p "$EVIDENCE_DIR"

cleanup() {
  local exit_status=$?
  if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    docker stop --time 30 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker logs "$CONTAINER_NAME" >"$SERVER_LOG" 2>&1 || true
    docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
  fi
  return "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker run --detach --pull never \
  --name "$CONTAINER_NAME" \
  --gpus all \
  --ipc host \
  --publish "127.0.0.1:${PORT}:${PORT}" \
  --volume "${AGENT_LAB_HF_CACHE}:/root/.cache/huggingface" \
  --env HF_HUB_OFFLINE=1 \
  --env TRANSFORMERS_OFFLINE=1 \
  "$IMAGE" \
  "$MODEL" \
  --revision "$REVISION" \
  --tokenizer "$MODEL" \
  --tokenizer-revision "$REVISION" \
  --served-model-name "$MODEL" \
  --host 0.0.0.0 \
  --port "$PORT" \
  --dtype bfloat16 \
  --load-format safetensors \
  --quantization moe_wna16 \
  --tensor-parallel-size 1 \
  --max-model-len "$CONTEXT_TOKENS" \
  --max-num-batched-tokens "$MAX_BATCHED_TOKENS" \
  --max-num-seqs 2 \
  --gpu-memory-utilization 0.75 \
  --cpu-offload-gb "$CPU_OFFLOAD_GB" \
  --seed 1729 \
  --generation-config vllm \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --language-model-only \
  --enable-chunked-prefill \
  --enable-prefix-caching \
  --enable-tokenizer-info-endpoint \
  --enable-force-include-usage \
  --enable-prompt-tokens-details \
  --enable-per-request-metrics >/dev/null

for _attempt in $(seq 1 360); do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
    break
  fi
  if [[ "$(docker container inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" != "true" ]]; then
    echo "vLLM container exited during startup" >&2
    docker logs "$CONTAINER_NAME" >&2 || true
    exit 1
  fi
  sleep 5
done

if ! curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "vLLM did not become healthy within 30 minutes" >&2
  docker logs "$CONTAINER_NAME" >&2 || true
  exit 1
fi

uv run --frozen python -m agent_lab.model_compat_suite \
  --model-profile configs/models/qwen35_35b_a3b_gptq_int4_experimental.toml \
  --server-kind vllm \
  --output "$COMPAT_OUTPUT"

uv run --frozen agent-lab experiment run \
  --experiment configs/experiments/qwen35-35b-a3b-task-quality-gate.toml \
  --runs-dir "$RUNS_DIR" \
  --order-seed 1729
