# Qwen3.5-27B llama.cpp quality-tier gate

Status: Accepted on 2026-08-03 at source `6989bb0`

Qwen3.5-9B fit the RTX 4090 but failed to produce two successful lanes in
either clean task-quality pair. Qwen3.5-35B-A3B GPTQ Int4 then failed inside
vLLM because its Triton path could not operate on CPU-offloaded tensors. This
gate changes both candidate and serving topology without changing the paired
task contract.

## Candidate decision

The candidate is the text portion of official `Qwen/Qwen3.5-27B`, served from
an immutable `Q4_K_M` GGUF conversion by Bartowski. Qwen's model card reports
that the dense 27B model exceeds the 35B-A3B model on most listed instruction,
reasoning, coding, and general-agent benchmarks, while llama.cpp supports the
model's chat template and local OpenAI-compatible serving:

- official model and benchmark table:
  <https://huggingface.co/Qwen/Qwen3.5-27B>;
- quantization repository:
  <https://huggingface.co/bartowski/Qwen_Qwen3.5-27B-GGUF>;
- original unquantized model named by the converter:
  <https://huggingface.co/Qwen/Qwen3.5-27B>.

This is a third-party imatrix quantization, not an official Qwen weight file.
That provenance and the quantization itself are part of the candidate under
test. They must not be generalized to BF16 or another quant. The artifact is
17,984,872,928 bytes, leaving materially more 4090 headroom than the 22.29 GB
35B-A3B Q4_K_M conversion. The repository and selected file were already more
than 72 hours old when selected on 2026-08-03.

Frozen identities:

- repository revision: `d7b113c40283f4d99f4eb0ec20d126ad653cc736`;
- selected file commit time: 2026-05-19 23:29:18 UTC;
- file: `Qwen_Qwen3.5-27B-Q4_K_M.gguf`;
- file/LFS SHA-256:
  `81657841d62f1821c748d0fea6c260b7d3508844fe4e9250253ef81c4e4d9edf`;
- local llama.cpp revision: `35c9b1f39ebe5a7bb83986d64415a079218be78d`;
- `llama-server` SHA-256:
  `afcd673634be6b8c46bce6b8326d65e9953cd541271649d5464ed7cea88dab25`.

The Hugging Face file record reported VirusTotal 0/74 while its other hosted
scanner jobs were queued. The gate therefore relies on the content digest,
GGUF-only loader surface, loopback binding, and an unprivileged local server;
it does not claim that every hosted scanner had completed.

## Frozen first operating point

- one RTX 4090 under WSL, CUDA llama.cpp build b9333;
- 32,768 total server tokens split into two 16,384-token request slots;
- all 99 available layers requested on GPU;
- auto-fit disabled so the server cannot silently reduce the frozen point;
- Q8 K/V caches, Flash Attention, continuous batching, and no context shift;
- the default 8,192 MiB host prompt cache pinned explicitly;
- Jinja tool parsing with thinking enabled and reasoning extracted into the
  OpenAI-compatible reasoning field;
- loopback port 8002, metrics and read-only slot inspection enabled, UI off;
- Qwen's documented thinking defaults: temperature 1.0, top-p 0.95, top-k 20,
  min-p 0, and presence penalty 1.5;
- two restart-separated characterization launches before any promotion.

The generic characterization command now understands both vLLM and llama.cpp.
For llama.cpp it applies the served chat template before token counting,
normalizes the server's Prometheus counters, verifies cancellation drain, and
retains the same resource, malformed-tool recovery, concurrency, and output-cap
matrix used for vLLM.

The first launch used `--ctx-size 16384` with two slots. The model loaded all
layers and reached health in about 5.8 seconds, but llama.cpp divided that
server-wide context into two 8,192-token slots. Runtime attestation rejected
the mismatch before any characterization request. The retained failure log is
not acceptance evidence; the corrected preregistered launch uses 32,768 total
tokens to preserve the intended 16,384-token per-request contract.

## Accepted result

Both corrected launches started with zero server request counters, produced
complete source/runtime provenance, ran every requested characterization cell,
and were marked measurement-eligible with no skips. The before/after launches
peaked at 23,221/23,229 MiB of whole-device GPU memory. Each completed the 512-
and 2,048-token output-cap cells at concurrency one and two without a deferred
request. Cancellation disconnected after the first visible token, drained the
server, and left it healthy; malformed-tool correction passed with a separate
reasoning channel on both launches.

The restart launch then passed every MAF and LangChain actor, plain, streaming,
structured-output, one-tool, multi-tool, multi-turn, and malformed-tool client
probe. The final randomized pair used retry-storm seed 42, ran LangGraph first,
and validated both bundles under pair ID
`qwen35_27b_task_quality_gate_v1:explicit_workflow:seed-42:rep-0`:

- LangGraph run `b3800985-1983-4e49-9c59-ae6c9e7316d8`: task success,
  score 0.916667, expected 0.952381 trace completeness;
- MAF Workflow run `767160e6-9390-4f69-b8da-d06523718156`: task success,
  score 1.0, expected 0.952381 trace completeness.

Both selected `rollback_checkout_retry_policy`, passed approval, applied the
mitigation, and verified recovery. The offline v3 report revalidated the pair,
reported zero operational failures and one success for each lane, and retained
the single observed root-cause component difference without treating one seed
as a powered comparison.

Retained evidence SHA-256 values:

| Evidence | SHA-256 |
| --- | --- |
| Before-restart characterization | `3257d2b8162dda4432e817ef5f1fd8dcc812fbcfb24f237e1a8dca54387051db` |
| After-restart characterization | `e0838d99de16c7ef04b9ed85251c4d1b8434aae8b8fb78152ec98d568dceaf68` |
| Live client compatibility | `d020bc7fc29dd148f4547eb93b614c8c37df7f9bc50af7b95b21f718adf2e445` |
| Frozen execution plan | `cec38434ef14326f89f695c572cad1563c8cd39aa9dda043b2649b58988808c1` |
| Validated experiment index | `f646eb41e03fbd768559e5f8089fd71ec414c1707443d4ebaf99d1c2ea6406ae` |
| Revalidated paired report JSON | `7952b7829ca590af53943d9ed36c83f14e3ac15afb0108f1985326fcc6f6e718` |
| Self-contained paired report HTML | `5e1eaa5a5de150722e7ffa371f00a31c05678a6c8e99e96a6e0b6e5cf8d6fbff` |

This accepts only the pinned Q4_K_M artifact and exact llama.cpp operating
point. The roughly 1.3 GiB whole-device margin is adequate for the sequential
pilot path demonstrated here but is narrow; concurrent non-study GPU workloads
remain prohibited during measurement.

## Acquisition and execution

Download the exact revision to a path containing the immutable snapshot ID:

```bash
mkdir -p \
  $HOME/models/hf/hub/models--bartowski--Qwen_Qwen3.5-27B-GGUF/snapshots/d7b113c40283f4d99f4eb0ec20d126ad653cc736
curl -fL --retry 5 --continue-at - \
  --output $HOME/models/hf/hub/models--bartowski--Qwen_Qwen3.5-27B-GGUF/snapshots/d7b113c40283f4d99f4eb0ec20d126ad653cc736/Qwen_Qwen3.5-27B-Q4_K_M.gguf \
  https://huggingface.co/bartowski/Qwen_Qwen3.5-27B-GGUF/resolve/d7b113c40283f4d99f4eb0ec20d126ad653cc736/Qwen_Qwen3.5-27B-Q4_K_M.gguf
```

After committing the gate source so measured execution sees a clean tree, set
the two explicit local paths and run fresh launches:

```bash
export AGENT_LAB_GGUF_MODEL=$HOME/models/hf/hub/models--bartowski--Qwen_Qwen3.5-27B-GGUF/snapshots/d7b113c40283f4d99f4eb0ec20d126ad653cc736/Qwen_Qwen3.5-27B-Q4_K_M.gguf
export AGENT_LAB_LLAMA_SERVER=$HOME/dev/tools/llama.cpp/build/bin/llama-server

scripts/run-qwen35-27b-llamacpp-gate.sh \
  before_restart runs/model-characterization/qwen35-27b-before runs
scripts/run-qwen35-27b-llamacpp-gate.sh \
  after_restart runs/model-characterization/qwen35-27b-after runs
```

The launcher verifies both large-file and server-binary digests before every
launch, refuses an existing output directory or ambiguous serving process,
starts a fresh server, captures its log, and reaps it on every exit path. The
runtime attestation also rejects drift in the model path, loopback binding,
context/slot topology, GPU layers, caches, parser, batch/thread settings, and
telemetry/safety flags. This llama.cpp build does not expose a quantization
field from `/props`, so provenance records `Q4_K_M` from the frozen served
alias while the launcher independently enforces the complete GGUF SHA-256.
The second launch runs the complete client
compatibility suite and only then runs the one-seed paired explicit-workflow
task gate.

## Acceptance rule

Promotion was fail closed. Both restart characterizations are eligible,
the compatibility suite must pass, source and runtime attestation must be
complete, both paired task lanes must validate and achieve `task_success`, and
runtime/VRAM must leave a credible path to the repeated pilot. The retained
evidence satisfies those conditions, so this exact profile is promoted to the
five-seed pilot. The rule remains unchanged for any future artifact, server,
context, or quantization change.
