# ADR 0012: Promote Qwen3.5-27B Q4_K_M on llama.cpp to measured pilots

Status: Accepted

Date: 2026-08-03

Supersedes: ADR 0008 for measured-pilot runtime selection

## Context

Qwen3.5-9B on vLLM passed hardware and client compatibility but did not produce
two successful lanes in either corrected paired task-quality gate. The larger
Qwen3.5-35B-A3B GPTQ-Int4 profile loaded with CPU offload but could not
initialize the pinned vLLM engine, while a no-offload point could not preserve
useful KV-cache headroom on the 24 GB RTX 4090.

A pinned third-party Q4_K_M GGUF conversion of official Qwen3.5-27B was then
tested on a pinned local llama.cpp build. The frozen operating point uses two
16,384-token request slots, all 99 model layers on the GPU, Q8 K/V caches,
Flash Attention, thinking/reasoning extraction, and one identical loopback
OpenAI-compatible endpoint for both framework lanes.

At source `6989bb0`, two restart-separated characterizations completed every
cell without a skip or deferral, the full MAF and LangChain client suite passed,
and both lanes of the retry-storm seed-42 task-quality pair succeeded. The
LangGraph score was 0.916667 and the MAF Workflow score was 1.0. The observed
whole-device peaks were 23,221 and 23,229 MiB.

## Decision

Promote only the exact Qwen3.5-27B artifact and llama.cpp operating point
recorded in `../research/qwen35-27b-llamacpp-gate.md` to the five-seed pilot and
harness-ablation profiles. Freeze the model and server binary SHA-256 values,
server flags, request settings, and runtime attestation in every launch.

Keep Qwen3.5-9B/vLLM as historical fit-check evidence and Qwen3-Next 80B A3B as
historical development-smoke evidence. Do not pool either cohort with the 27B
pilot results or infer that a different Qwen3.5-27B quantization/runtime is
equivalent.

## Consequences

- The model-selection blocker for the five-seed pilots is closed.
- The measured-study protocol remains blocked until the explicit and harness
  pilots support seed-cluster sizing and component discrimination, and an
  independent reviewer approves the frozen plan.
- The approximately 1.3 GiB whole-device margin is credible for the validated
  sequential launch path but narrow. Concurrent non-study GPU workloads are
  prohibited during measurement.
- Any artifact, llama.cpp revision, context/slot topology, cache precision, or
  serving-flag change requires a new fail-closed gate rather than inheriting
  this acceptance.
