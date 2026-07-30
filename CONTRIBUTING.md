# Contributing

This repository treats experiment validity as a product requirement.

## Development setup

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The MAF runner, LangGraph runner, and incident MCP service are independent
projects. Update and commit their own `uv.lock` files when their dependencies
change.

## Change rules

- Change shared prompts, tools, budgets, model profiles, scenario generators,
  or scorers in both matched lanes and version the affected contract.
- Never modify a completed run bundle.
- Never drop failed runs because they are inconvenient.
- Keep framework-native types and dependencies inside the relevant runner.
- Do not commit secrets, model weights, private datasets, generated runs,
  Phoenix data, or raw model reasoning.
- Add or supersede an ADR when reversing an accepted architectural decision.
- Include a deterministic test for scenario/scorer changes.
- Include a paired smoke run for framework adapter changes once those runners
  exist.

Generated benchmark reports must name the source commit, model revision,
configuration hash, scorer version, seed set, and exclusions.
