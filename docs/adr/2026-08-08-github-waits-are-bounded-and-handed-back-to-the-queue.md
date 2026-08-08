---
title: A GitHub rate-limit wait is bounded, and the remainder is handed back to the queue
status: accepted
date: 2026-08-08
type: architecture
areas: [github, pipeline]
tasks: [T12, T23]
files: [src/sentinel/github/client.py]
specs: [docs/06-event-pipeline.md]
supersedes:
---

# A GitHub rate-limit wait is bounded, and the remainder is handed back to the queue

## Context

GitHub rate-limits in two ways, and a bot that comments and labels meets both. The primary limit
answers `403` with `x-ratelimit-remaining: 0` and an `x-ratelimit-reset` that can be up to an hour
away. The secondary limit — the one that fires on writes in quick succession — answers `403` or
`429`, usually with `retry-after` in the tens of seconds.

Every GitHub call Sentinel makes happens inside a claimed job. A worker holds its lease for
`JOB_LEASE_TIMEOUT` (15 minutes by default), after which another worker may reclaim the job and do
the work a second time (`docs/06-event-pipeline.md#jobs-and-claiming`). So "wait until the limit
resets" and "hold the job" are not compatible for an arbitrary reset time: sleeping through a
primary limit would produce a duplicate comment rather than a delayed one.

The queue already has a retry policy — `2^attempts × 5 s` with jitter, capped at 10 minutes, and
`MAX_JOB_ATTEMPTS` after which the remediation fails.

## Decision

The client waits in-process while the **total** it has waited in this call stays within
`MAX_WAIT_SECONDS` (60) and attempts remain, which covers the secondary limit and a transient
`5xx`. The budget is cumulative rather than per-wait: two waits of 60 seconds are two minutes of
the lease, which is not what a 60-second bound means.

Past the budget it raises `GitHubRateLimited` carrying `retry_after` — **the seconds GitHub asked
for, and `None` when it did not say**. This client's own backoff is not substituted: a fabricated
delay wearing GitHub's authority leaves the queue unable to tell an answer from a guess, and four
seconds is a poor guess at a limit that resets in an hour.

A rate limit is retried whatever the method, because the request was rejected before it was
processed and repeating it cannot repeat a side effect. A `5xx` is retried only for an idempotent
method: GitHub can answer `502` after a write has landed, so retrying a `POST` is how one
escalation becomes two comments on the issue. A `5xx` on `POST` or `PATCH` is raised as
`GitHubUnavailable`, still the queue's to retry — under a job attempt that is recorded on the
remediation rather than invisible inside one call.

Anything else in the `4xx` range raises immediately without a retry, as
`docs/06-event-pipeline.md#reliability-policy` requires.

## Alternatives considered

| Option | Why not |
|---|---|
| Sleep until `x-ratelimit-reset`, however far away | Exceeds the job lease, so the job is reclaimed and the side effect — a comment, a label — happens twice. A worker asleep for an hour is also indistinguishable from a hung one |
| No waiting at all; every rate limit fails the job | Turns the common case, a secondary limit clearing in ten seconds, into a job attempt spent and a minute of queue backoff. Escalating a remediation because two labels were added in quick succession is a false failure |
| Retry forever inside the client | Removes the attempt ceiling the queue exists to enforce, and hides the rate limiting from `remediation_event`, where the cost of it should be visible |
| Bound each wait rather than their sum | Reads as a 60-second bound and is a 120-second one at `RETRY_ATTEMPTS = 3`. It is still inside the lease, but a bound this decision asks to be judged on should be the number it states |
| Retry `5xx` on every method, as on the rate limits | Contradicts the duplicate-comment argument this record rests on, in the one case where the duplicate is invisible: nothing distinguishes a `502` before the write from a `502` after it |
| Fill `retry_after` with the local backoff when GitHub named none | Cheaper for the queue, which then always has a number, but the number is invented and indistinguishable from GitHub's. The queue has its own backoff for exactly the case where there is no answer |

## Consequences

A short limit is invisible to the pipeline, and a long one becomes a scheduled retry with a
`retry_after` the queue can honour, rather than a stalled worker. Rate limiting stays legible: the
long case is recorded as a job attempt with the reason on the event, so "we are being throttled by
GitHub" is answerable from the data.

The cost is two retry layers, so a genuinely unreachable GitHub is attempted `RETRY_ATTEMPTS ×
MAX_JOB_ATTEMPTS` times before the remediation fails. That is acceptable because the inner layer is
bounded in wall-clock time by 60 seconds in total, whatever the mix of waits that fills it.

A second cost is that a `5xx` on a write reaches the queue immediately rather than being absorbed,
so a flapping GitHub spends job attempts on writes where reads would have recovered in place. That
is the intended trade: a spent attempt is visible on the remediation, a duplicated escalation
comment is not.

**What would tell us this was wrong:** primary-limit rejections appearing at all on a workload of
tens of remediations a day. The token's budget is 5,000 requests an hour and one remediation costs
a handful, so hitting it would mean something is polling far more than the design says, and the fix
would be there rather than in the waiting policy.
