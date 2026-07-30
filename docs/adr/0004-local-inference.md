# ADR 0004: Use one local vLLM Chat Completions endpoint

Status: Proposed

Date: 2026-07-31

## Context

The host has an RTX 4090 with about 24 GB VRAM and 128 GB system RAM. Both
frameworks must use the same local reasoning model. MAF and LangChain support
custom OpenAI-compatible endpoints; current Qwen3.5 guidance includes vLLM
reasoning and tool parsers.

## Decision

Use one direct vLLM OpenAI-compatible Chat Completions endpoint for every lane.
Begin hardware/tool compatibility with `Qwen/Qwen3.5-9B` in text-only mode and
a 32K context profile. Treat
`Qwen/Qwen3.5-35B-A3B-GPTQ-Int4` with bounded CPU offload as a separate
quality-tier experiment.

Finalize the model only after measuring fit, throughput, concurrency,
multi-step tool correctness, reasoning/tool parsing, and restart
reproducibility. Pin the model revision and server artifact/flags afterward.

## Consequences

- Model access is framework-neutral.
- One server makes model identity and GPU telemetry easy to audit.
- Chat Completions maximizes present local tool-call compatibility.
- The 9B model may be too weak; the 35B quantized model may be too slow or fail
  to leave useful KV cache.
- Model characterization becomes a gate before framework implementation.
- Responses-API-specific framework capabilities are outside the first study.
