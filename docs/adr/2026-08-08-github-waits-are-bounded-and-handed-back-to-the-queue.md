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

The client waits in-process only while the wait is at most `MAX_WAIT_SECONDS` (60) and there are
attempts left, which covers the secondary limit and transient `5xx`. Beyond that bound it raises
`GitHubRateLimited` carrying `retry_after` — the number of seconds GitHub asked for, or `None` when
it did not say — and the queue schedules the next attempt.

Anything else in the `4xx` range raises immediately without a retry, as
`docs/06-event-pipeline.md#reliability-policy` requires.

## Alternatives considered

| Option | Why not |
|---|---|
| Sleep until `x-ratelimit-reset`, however far away | Exceeds the job lease, so the job is reclaimed and the side effect — a comment, a label — happens twice. A worker asleep for an hour is also indistinguishable from a hung one |
| No waiting at all; every rate limit fails the job | Turns the common case, a secondary limit clearing in ten seconds, into a job attempt spent and a minute of queue backoff. Escalating a remediation because two labels were added in quick succession is a false failure |
| Retry forever inside the client | Removes the attempt ceiling the queue exists to enforce, and hides the rate limiting from `remediation_event`, where the cost of it should be visible |

## Consequences

A short limit is invisible to the pipeline, and a long one becomes a scheduled retry with a
`retry_after` the queue can honour, rather than a stalled worker. Rate limiting stays legible: the
long case is recorded as a job attempt with the reason on the event, so "we are being throttled by
GitHub" is answerable from the data.

The cost is two retry layers, so a genuinely unreachable GitHub is attempted `RETRY_ATTEMPTS ×
MAX_JOB_ATTEMPTS` times before the remediation fails. That is acceptable because the inner layer is
bounded in wall-clock time by the same 60 seconds.

**What would tell us this was wrong:** primary-limit rejections appearing at all on a workload of
tens of remediations a day. The token's budget is 5,000 requests an hour and one remediation costs
a handful, so hitting it would mean something is polling far more than the design says, and the fix
would be there rather than in the waiting policy.
