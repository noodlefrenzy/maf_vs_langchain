# Initial review guide

This baseline is ready for design review, not benchmark-result review.

Use
[issue #10](https://github.com/noodlefrenzy/maf_vs_langchain/issues/10)
as the review checklist. Accepted follow-up work is organized on the
[Agent Framework Field Lab project](https://github.com/users/noodlefrenzy/projects/21).

Recommended reading order:

1. [Specification](SPEC.md)
2. [Architecture](architecture.md)
3. [Framework research](research/framework-landscape.md)
4. [Task options](research/task-options.md)
5. [Comparison protocol](comparison-protocol.md)
6. [ADRs](adr/README.md)

Please focus on these decisions:

- Primary scenario: keep seeded incident response, or promote another task?
- Comparison shape: approve explicit-workflow and opinionated-harness tracks?
- Capability separation: approve role-scoped MCP tools that force coordination?
- Local model: approve Qwen3.5-9B for fit testing and the 35B-A3B quantized model
  as a separate quality-tier spike?
- Live experience: start with a browser-based paired observer (recommended) or
  a Textual terminal UI?
- Scale: how much overnight GPU time is acceptable for the measured study?

Items intentionally deferred until those decisions are accepted:

- final prompt wording;
- final MCP tool schemas and error messages;
- a model download;
- executable framework adapters;
- the observer UI;
- benchmark claims.
