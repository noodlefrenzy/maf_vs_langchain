# Security policy

## What this project is

This repository is a **research harness**, not production software. It exists to
run controlled, paired comparisons of agent frameworks on a seeded simulator. It
is not hardened for multi-tenant or internet-facing deployment, and no part of it
should be exposed beyond the local host.

Design constraints that follow from that scope:

- The incident MCP service, the observer UI, and the trace collector are all
  **loopback-only by design**. Binding them to a routable interface is
  unsupported and rejected where the code can detect it.
- Run-scoped bearer credentials are HMAC-signed and short-lived. They authorize
  actions inside a simulated incident world and carry no external authority.
- The approval secrets in `runs/<id>/world/world-checkpoint.json` are the
  simulator's own, generated per run. They are written owner-only (`0600`) and
  the entire `runs/` tree is excluded from version control.
- Report bundles contain a `private-runs/` subtree that is **owner-only and must
  not be published**. See [evidence retention](docs/evidence-retention.md).

## Supported versions

The project is pre-1.0 and moves with `main`. Fixes land on `main`; there are no
backported release branches.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub's
[private vulnerability reporting](https://github.com/noodlefrenzy/maf_vs_langchain/security/advisories/new)
rather than opening a public issue.

Useful detail: affected file and line, the trust boundary you believe is
crossed, and whether the issue is reachable without local host access.

Expect an acknowledgement within about two weeks. This is a personal research
project maintained on a best-effort basis — there is no SLA, and no bounty.

## Out of scope

The following are known and accepted properties of a local research tool, not
vulnerabilities:

- Fixed, non-secret signing keys in the hermetic `*_compat.py` probes. These are
  labelled non-production in-source; the probes need reproducibility, not
  secrecy.
- Placeholder API keys (`agent-lab-local`, `compat-placeholder`,
  `model-wire-test`) sent to a local OpenAI-compatible inference server, which
  requires a non-empty string but does not authenticate.
- Cleartext simulator approval tokens inside owner-only, git-ignored run
  checkpoints. Persisting them is what makes approval state survive the
  framework-restart scenario under test.
- Anything requiring an attacker to already have local access to the run
  directory.

## Reporting a comparison-validity problem instead

If you have found a way the harness treats the two lanes **unequally** — an
asymmetric budget, prompt, retry policy, or scorer path — that is a correctness
issue for this project, not a security issue. Open a public issue using the
comparison-validity template so it can be discussed in the open.
