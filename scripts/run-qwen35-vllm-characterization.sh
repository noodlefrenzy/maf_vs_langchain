#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly CONTAINER_NAME="agent-lab-qwen35-9b-vllm"
readonly IMAGE="vllm/vllm-openai@sha256:674c5aa666d38c07a0dc779f8c77a05c2b859617410d16dcdd8a8776166c92b9"
readonly MODEL="Qwen/Qwen3.5-9B"
readonly REVISION="c202236235762e1c871ad0ccb60c8ee5ba337b9a"
readonly PORT="8001"
readonly MAX_BATCHED_TOKENS="8192"

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <8192|16384|32768> <before_restart|after_restart> <output.json>" >&2
  exit 2
fi

readonly CONTEXT_TOKENS="$1"
readonly RESTART_PHASE="$2"
readonly OUTPUT="$3"
readonly COMPAT_OUTPUT="${OUTPUT%.json}-compat.json"
readonly SERVER_LOG="${OUTPUT%.json}-server.log"

case "$CONTEXT_TOKENS" in
  8192|16384|32768) ;;
  *) echo "context must be 8192, 16384, or 32768" >&2; exit 2 ;;
esac
case "$RESTART_PHASE" in
  before_restart|after_restart) ;;
  *) echo "restart phase must be before_restart or after_restart" >&2; exit 2 ;;
esac

if [[ -z "${AGENT_LAB_HF_CACHE:-}" || ! -d "$AGENT_LAB_HF_CACHE" ]]; then
  echo "AGENT_LAB_HF_CACHE must name the existing Hugging Face cache directory" >&2
  exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "pinned vLLM image is not local; follow docs/research/qwen35-9b-characterization.md" >&2
  exit 2
fi
if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  echo "container $CONTAINER_NAME already exists; refusing to replace it" >&2
  exit 2
fi
if [[ -e "$OUTPUT" || -e "$SERVER_LOG" || ( "$CONTEXT_TOKENS" == "32768" && -e "$COMPAT_OUTPUT" ) ]]; then
  echo "an output report or server log already exists; refusing to overwrite evidence" >&2
  exit 2
fi

cleanup() {
  local exit_status=$?
  if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    docker stop --time 30 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    mkdir -p "$(dirname "$SERVER_LOG")"
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
  --load-format bitsandbytes \
  --quantization bitsandbytes \
  --tensor-parallel-size 1 \
  --max-model-len "$CONTEXT_TOKENS" \
  --max-num-batched-tokens "$MAX_BATCHED_TOKENS" \
  --max-num-seqs 2 \
  --gpu-memory-utilization 0.75 \
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

for _attempt in $(seq 1 240); do
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
  echo "vLLM did not become healthy within 20 minutes" >&2
  docker logs "$CONTAINER_NAME" >&2 || true
  exit 1
fi

uv run --frozen python -m agent_lab.local_inference_benchmark \
  --context-tokens "$CONTEXT_TOKENS" \
  --restart-phase "$RESTART_PHASE" \
  --container-name "$CONTAINER_NAME" \
  --container-image-digest "${IMAGE##*@}" \
  --output "$OUTPUT"

if [[ "$CONTEXT_TOKENS" == "32768" ]]; then
  uv run --frozen python -m agent_lab.model_compat_suite \
    --model-profile configs/models/qwen35_9b_vllm.toml \
    --server-kind vllm \
    --output "$COMPAT_OUTPUT"
fi
