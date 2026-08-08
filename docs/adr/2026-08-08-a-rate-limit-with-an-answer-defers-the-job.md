---
title: A rate limit GitHub named a delay for defers the job; one it did not fails it
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline, github]
tasks: [T23]
files: [src/sentinel/pipeline/worker.py]
specs: [docs/06-event-pipeline.md]
supersedes:
---

# A rate limit GitHub named a delay for defers the job; one it did not fails it

## Context

The GitHub client waits out a short rate limit in place and hands back anything longer as
`GitHubRateLimited`, carrying `retry_after` — the seconds GitHub asked for, and `None` when it did
not say ([ADR](./2026-08-08-github-waits-are-bounded-and-handed-back-to-the-queue.md)). That record
leaves the delay "for the queue to schedule" without saying how.

The queue offers two endings, and they differ in more than the delay
([06](../06-event-pipeline.md#reliability-policy)):

- `fail()` increments `attempts`, computes `2^attempts × 5 s` with jitter — ten to eighty seconds at
  `MAX_JOB_ATTEMPTS = 5` — and gives up when the budget is spent, taking the remediation to `FAILED`;
- `defer()` sets `run_after` to whatever it is told and leaves `attempts` alone, which the spec
  reserves for work that "is still valid and still wanted" and nothing about which has failed.

GitHub's two limits are very different in size. The secondary one clears in tens of seconds, and the
client has usually absorbed it before the worker hears about it at all. The primary one resets up to
an hour later, and its `x-ratelimit-reset` says when.

`fail()` on a primary limit spends all five attempts inside two and a half minutes, every one of
them rejected before it is processed, and then escalates a remediation to a human over a quota that
had not reset yet.

## Decision

A `GitHubRateLimited` carrying a `retry_after` **defers** the job for exactly that long. Everything
else, including a rate limit with no delay named, goes through `fail()` with the reliability
policy's own backoff and attempt ceiling.

The split is on whether there is an answer to honour, not on which limit was hit: a rejected request
had no side effect, so nothing about the job has failed either way, and the only question is whether
Sentinel has a schedule or has to guess. Where it has to guess, the queue's backoff is the better
guess and its ceiling is the safety net.

## Alternatives considered

| Option | Why not |
|---|---|
| Always `fail()`, discarding `retry_after` | The delay the previous record went to some trouble to preserve would then reach nothing that could act on it, and a primary limit would fail a remediation in two and a half minutes of retries against a closed window |
| Always `defer()`, whether or not a delay was named | With no answer there is nothing to defer *for*, so the deferral length would be invented — and with `attempts` untouched, a genuine `403` misread as a rate limit would be retried forever with no ceiling |
| `fail()`, but pass `retry_after` through as the backoff | Closest to the spec's letter, and it spends an attempt per rate limit anyway: five throttles over the life of a remediation would fail it without a single request having been processed. `attempts` is what the backoff schedule is computed from, and honouring GitHub's delay means not computing one |
| Cap the deferral, so a job cannot be held for an hour | The cap would only make the job claimable while the window is still closed, producing another rejection and another deferral. An hour is what GitHub said |

## Consequences

Throttling costs latency and nothing else: the job keeps its retry budget for the failures that are
really failures, and a remediation is not escalated to a human over a quota. The queue stays legible
to an operator — `select status, count(*) from job` shows the throttled work as `deferred`, next to
whatever the concurrency cap is holding, and `run_after` says when it comes back.

Two costs. A rate limit no longer appears as a job attempt, so "we are being throttled" is answered
from the logs and the `deferred` count rather than from `attempts` on the remediation — narrower
than the previous record assumed when it said the long case would be "recorded as a job attempt". And
a deferral loop is unbounded by construction: a token permanently over its quota would defer for
ever rather than failing. That is visible rather than silent, and the alternative is failing
remediations because of a limit that would have cleared.

**What would tell us this was wrong:** `deferred` jobs accumulating with rate-limit reasons, which
would mean the token's budget is genuinely too small rather than momentarily spent and the fix is in
how much Sentinel calls GitHub; or a `403` that is not a rate limit being classified as one, which
would show up as a job deferring repeatedly and never succeeding.
