#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly MODEL_ID="bartowski/Qwen_Qwen3.5-27B-GGUF:Q4_K_M"
readonly MODEL_REVISION="d7b113c40283f4d99f4eb0ec20d126ad653cc736"
readonly MODEL_SHA256="81657841d62f1821c748d0fea6c260b7d3508844fe4e9250253ef81c4e4d9edf"
readonly LLAMA_SERVER_SHA256="afcd673634be6b8c46bce6b8326d65e9953cd541271649d5464ed7cea88dab25"
readonly PORT="8002"
readonly CONTEXT_TOKENS="16384"

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <before_restart|after_restart> <evidence-directory> <runs-directory>" >&2
  exit 2
fi

readonly RESTART_PHASE="$1"
readonly EVIDENCE_DIR="$2"
readonly RUNS_DIR="$3"
readonly CHARACTERIZATION_OUTPUT="${EVIDENCE_DIR}/characterization.json"
readonly COMPAT_OUTPUT="${EVIDENCE_DIR}/model-compatibility.json"
readonly SERVER_LOG="${EVIDENCE_DIR}/server.log"

case "$RESTART_PHASE" in
  before_restart|after_restart) ;;
  *) echo "restart phase must be before_restart or after_restart" >&2; exit 2 ;;
esac

if [[ -z "${AGENT_LAB_GGUF_MODEL:-}" || ! -f "$AGENT_LAB_GGUF_MODEL" ]]; then
  echo "AGENT_LAB_GGUF_MODEL must name the pinned GGUF file" >&2
  exit 2
fi
if [[ -z "${AGENT_LAB_LLAMA_SERVER:-}" || ! -x "$AGENT_LAB_LLAMA_SERVER" ]]; then
  echo "AGENT_LAB_LLAMA_SERVER must name the pinned llama-server executable" >&2
  exit 2
fi
if [[ "$(sha256sum "$AGENT_LAB_GGUF_MODEL" | cut -d ' ' -f 1)" != "$MODEL_SHA256" ]]; then
  echo "GGUF SHA-256 differs from the pinned ${MODEL_ID} artifact at ${MODEL_REVISION}" >&2
  exit 1
fi
if [[ "$(sha256sum "$AGENT_LAB_LLAMA_SERVER" | cut -d ' ' -f 1)" != "$LLAMA_SERVER_SHA256" ]]; then
  echo "llama-server SHA-256 differs from the pinned local build" >&2
  exit 1
fi
if [[ "$(docker container inspect --format '{{.State.Running}}' agent-lab-qwen35-9b-vllm 2>/dev/null || true)" == "true" ]]; then
  echo "the Qwen3.5-9B vLLM container must be stopped before the 27B gate" >&2
  exit 2
fi
if pgrep -x llama-server >/dev/null 2>&1; then
  echo "a llama-server process is already running; refusing an ambiguous runtime" >&2
  exit 2
fi
if [[ -e "$EVIDENCE_DIR" ]]; then
  echo "evidence directory already exists; refusing to overwrite it" >&2
  exit 2
fi

mkdir -p "$EVIDENCE_DIR"

"$AGENT_LAB_LLAMA_SERVER" \
  --model "$AGENT_LAB_GGUF_MODEL" \
  --alias "$MODEL_ID" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --ctx-size "$CONTEXT_TOKENS" \
  --parallel 2 \
  --n-gpu-layers 99 \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --batch-size 2048 \
  --ubatch-size 512 \
  --threads 16 \
  --threads-batch 16 \
  --seed 1729 \
  --jinja \
  --chat-template-kwargs '{"enable_thinking":true}' \
  --reasoning on \
  --reasoning-format deepseek \
  --cont-batching \
  --metrics \
  --slots \
  --no-context-shift \
  --no-ui >"$SERVER_LOG" 2>&1 &
readonly SERVER_PID=$!

cleanup() {
  local exit_status=$?
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill -TERM "$SERVER_PID" >/dev/null 2>&1 || true
    for _attempt in $(seq 1 30); do
      if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      kill -KILL "$SERVER_PID" >/dev/null 2>&1 || true
    fi
  fi
  wait "$SERVER_PID" >/dev/null 2>&1 || true
  return "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for _attempt in $(seq 1 180); do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
    break
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "llama-server exited during startup" >&2
    tail -200 "$SERVER_LOG" >&2 || true
    exit 1
  fi
  sleep 5
done

if ! curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null; then
  echo "llama-server did not become healthy within 15 minutes" >&2
  tail -200 "$SERVER_LOG" >&2 || true
  exit 1
fi

uv run --frozen python -m agent_lab.local_inference_benchmark \
  --model-profile configs/models/qwen35_27b_llamacpp_q4km_experimental.toml \
  --context-tokens "$CONTEXT_TOKENS" \
  --output-caps 512 2048 \
  --concurrencies 1 2 \
  --server-kind llama_cpp \
  --process-name llama-server \
  --server-pid "$SERVER_PID" \
  --restart-phase "$RESTART_PHASE" \
  --output "$CHARACTERIZATION_OUTPUT"

if [[ "$RESTART_PHASE" == "after_restart" ]]; then
  uv run --frozen python -m agent_lab.model_compat_suite \
    --model-profile configs/models/qwen35_27b_llamacpp_q4km_experimental.toml \
    --server-kind llama_cpp \
    --output "$COMPAT_OUTPUT"

  uv run --frozen agent-lab experiment run \
    --experiment configs/experiments/qwen35-27b-task-quality-gate.toml \
    --runs-dir "$RUNS_DIR" \
    --order-seed 1729
fi
