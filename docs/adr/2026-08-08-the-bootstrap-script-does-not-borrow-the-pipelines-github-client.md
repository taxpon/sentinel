---
title: The bootstrap script talks to GitHub itself rather than through the pipeline's client
status: accepted
date: 2026-08-08
type: process
areas: [github, ops]
tasks: [T41]
files: [scripts/bootstrap_github.py, src/sentinel/github/client.py]
specs: [docs/09-operations.md]
supersedes:
---

# The bootstrap script talks to GitHub itself rather than through the pipeline's client

## Context

`scripts/bootstrap_github.py` has to enable the issue tracker on `taxpon/superset`, create eleven
labels and register a webhook — `PATCH /repos/{repo}`, the `/labels` collection and the `/hooks`
collection.

`sentinel.github.client.GitHubClient` already speaks to the same API with retries and redaction, and
none of those three endpoints is in it. Its `ROUTES` table is not an implementation detail: it is
the statement of everything the running process can do to the repository, asserted by
`tests/test_github_client.py`, and it exists so that "Sentinel cannot merge a pull request" can be
read off one table instead of reconstructed from ten methods
(`docs/adr/2026-08-07-humans-approve-every-merge.md`).

The two callers also want opposite things from a failure. The pipeline is unattended, so it retries
and hands the rest back to the queue. The bootstrap is a command an operator is watching, run at
most a few times, where an immediate error and a re-run are better than a wait.

## Decision

The script builds its own `httpx.Client` and its own small `GitHub` wrapper. `GitHubClient` is not
extended, and `ROUTES` gains no entries.

Two things are imported rather than restated: `GITHUB_API_BASE` and `API_VERSION`, so that the
pinned API version cannot drift between the bootstrap and the pipeline.

## Alternatives considered

| Option | Why not |
|---|---|
| Add the routes to `ROUTES` | It would give the worker and the poller the ability to rewrite repository settings, labels and webhooks for the rest of the project's life, to save one file a `httpx.Client`. The table stops being a bound on the running system |
| A second routes table inside the client, for administration | The property is "every path this client can build comes from `ROUTES`". A second table is the same permission with an extra step, and the test that enforces the first would not see it |
| Shell out to `gh`, as `scripts/seed_issues.py` does | `gh` cannot be intercepted by `respx`. The request bodies — the event list above all — would then only be assertable by running against a real repository, which is the one thing this task must not do |

## Consequences

The script has no retry policy: a `5xx` fails the run. That is acceptable because every step is
reconciling, so re-running completes whatever did not finish, and because an operator is present to
re-run it. It is not acceptable for anything unattended, which is the line between the two clients.

Redaction is duplicated in miniature: the script scrubs the configured credentials out of its own
error text, because `sentinel.observability.logging` redacts structured log lines and this program
prints. The set of secrets comes from `secret_values(settings)` rather than a hand-kept list, so a
new `SecretStr` field is covered in both places at once.

**What would tell us this was wrong:** a third caller needing repository administration — say a
teardown command, or the pipeline itself having to create a label at runtime. At that point the
right move is a separate admin client in `src/`, with its own routes table and its own tests, not
routes added to the one that must not grow.
