---
title: The sweep resolves both ecosystems against OSV, from inside Sentinel
status: accepted
date: 2026-08-08
type: process
areas: [remediation, devin]
tasks: [T43]
files: [src/sentinel/scanner/audit.py]
specs: [docs/05-devin-integration.md, docs/remediation-candidates.md]
supersedes:
---

# The sweep resolves both ecosystems against OSV, from inside Sentinel

## Context

[05](../05-devin-integration.md#scheduled-sweep) describes the nightly sweep as a recurring **Devin
session** whose prompt is "run `pip-audit` and `npm audit` on the target repo; for each new finding
not already tracked, open a GitHub issue." Two things about that description had to be settled
before any of it could be built.

**Where it runs.** [`docs/tasks.yaml`](../tasks.yaml) gives T43 `src/sentinel/scanner/audit.py`, so
the project's own task graph puts the sweep in Sentinel rather than in a prompt. That is also the
only version of it that can be tested: a prompt's judgement about which findings are worth filing
cannot be asserted, and the judgement is the entire substance of this task
([the filter](./2026-08-08-the-sweep-files-only-what-forces-a-decision.md)). The `POST
/v3/organizations/{org}/schedules` row is unaffected — a schedule can invoke this sweep as easily
as it can prompt for one — but the prompt text in [05](../05-devin-integration.md#scheduled-sweep)
now describes something Sentinel does itself, and should be corrected by whoever owns that
document.

**What it asks.** `taxpon/superset` has two dependency trees: ~285 pinned Python packages across
`requirements/*.txt` and ~2,400 resolved npm packages in `superset-frontend/package-lock.json`.
[`docs/remediation-candidates.md`](../remediation-candidates.md) used the OSV API for the first and
`npm audit` for the second, and then corroborated the `npm audit` result by scanning every unique
`package@version` pair in the lockfile against OSV — which returned exactly the `image-size` pair
`npm audit` had flagged.

## Decision

Both ecosystems are resolved against the [OSV](https://osv.dev) API: `POST /v1/querybatch` for the
whole tree in batches, then `GET /v1/vulns/{id}` for each record. `pip-audit` and `npm audit` are
not run.

The manifests are read over the GitHub contents API. Nothing is checked out and no package manager
is invoked, so the sweep needs neither a Python environment matching the target's nor a Node
toolchain in Sentinel's image.

Three properties decided it:

- **Neither auditor can run where the sweep runs.** `npm audit` needs Node and a lockfile it is
  willing to resolve, and it reports dependency *paths* rather than stable advisory identifiers.
  `pip-audit` needs the packages installed, or a resolver run against a manifest, to say what is
  affected. Both would put a second package ecosystem inside Sentinel's container to answer a
  question one HTTP call answers.
- **One identifier space.** Every finding, from either tree, is keyed on an OSV record and its
  aliases. That is what makes the duplicate fingerprint uniform across ecosystems
  ([the sweep's memory](./2026-08-08-the-filed-issue-is-the-sweeps-memory.md)); two sources would
  mean two vocabularies to reconcile before anything could be deduplicated.
- **Reproducible without credentials.** OSV is unauthenticated, so every issue the sweep files can
  carry the exact `curl` that produced it, and a reviewer can check the claim without holding any
  of Sentinel's tokens. That matters more than it sounds: the sweep is a bot asserting that
  something is wrong with the repository, and the assertion should be checkable.

## Alternatives considered

| Option | Why not |
|---|---|
| `pip-audit` + `npm audit`, as [05](../05-devin-integration.md) describes | Two toolchains in the image, two output formats, two identifier vocabularies, and `npm audit` needs a registry round trip on every run. Its findings were already shown to be a subset of what OSV returns for the same lockfile ([remediation candidates](../remediation-candidates.md), C4) |
| GitHub's Dependabot alerts API | Richer — it already resolves direct versus transitive and deduplicates. But it reports only what Dependabot has scanned, requires the feature enabled on the fork and a token carrying `security_events`, and cannot be reproduced by a reader. It also makes the sweep's findings a function of another bot's schedule rather than of the tree |
| OSV for Python, `npm audit` for the frontend — what T50 did by hand | Defensible for a one-off triage, where a human reconciles the two. As a nightly job it means the npm half has a different notion of "the same finding" from the Python half, and the deduplication is where this fails dangerously |
| Query OSV only for the packages a manifest declares | Two thirds of the frontend advisories are on transitive packages — `image-size` is four levels down. Scanning only declared packages would have found none of them |

## Consequences

The sweep is a pure function of five files and one public API, which is why it can be tested end to
end against recorded responses and why `tests/test_scanner.py` can assert what it files for a tree
cut down from the real one.

The cost is that OSV knows nothing about how npm resolves. For a transitive finding the sweep can
say which declared dependency reaches the vulnerable package and by what path, but not which
version of that dependency pulls in a fixed one — so the issue names the chain and states the
question rather than answering it. `npm audit` would have answered it, and this is the one thing it
would have been better at.

**What would tell us this was wrong:** an advisory that `npm audit` reports on this lockfile and
OSV does not. The two were equal on the tree as triaged; a divergence would mean the npm registry's
advisory data has content the OSV mirror lacks, and the frontend half would have to move back.
