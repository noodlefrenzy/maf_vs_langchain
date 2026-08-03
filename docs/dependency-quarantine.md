# Dependency release-age quarantine

Every uv project enforces a rolling `exclude-newer = "72 hours"` policy. uv
applies the cutoff to direct and transitive registry resolution, treats missing
upload times as unavailable, and records the resolved cutoff in each lockfile.
Repository verification also reads every locked registry artifact's
`upload-time` and requires it to be strictly older than the current UTC cutoff.
These are the documented semantics of uv's
[dependency cooldown](https://docs.astral.sh/uv/concepts/resolution/#dependency-cooldowns).

The four governed projects are the root package, both isolated runner
packages, and the incident MCP service. Package- or index-specific bypasses are
forbidden. Non-registry dependency releases fail closed because their
authoritative publication time cannot be established by this workflow.

## Approved update workflow

Edit the relevant dependency constraint, then run:

```bash
python scripts/dependency_quarantine.py lock
```

The command refreshes all four lockfiles under their project-owned uv policy,
checks every recorded registry artifact timestamp against one current UTC
72-hour cutoff, and runs `uv lock --check` for every project. Do not commit a
manifest or lockfile if this command fails. Do not pass `--exclude-newer false`,
add a package/index exclusion, or run a different lock update path to bypass a
failure.

CI runs the same verifier as a prerequisite job. No project dependency is
installed until the quarantine policy, artifact timestamps, and all four locks
pass.

An emergency exception requires explicit approval for a named package and
version after the supply-chain risk is explained. The repository has no
mechanical bypass for that exception; an approved policy change must be
reviewed and recorded explicitly.
