---
title: Record every state transition as an append-only event
status: accepted
date: 2026-08-07
type: architecture
areas: [data, analytics]
tasks: [T03, T16]
files: [src/sentinel/models.py, src/sentinel/analytics/metrics.py]
specs: [docs/03-data-model.md, docs/07-observability.md]
supersedes:
---

# Record every state transition as an append-only event

## Context

The observability requirement is not "how many sessions are there" but MTTR, funnel drop-off,
throughput over time, and how many self-correction cycles each fix needed. A mutable `state` column
answers none of those: it knows where a remediation is, not how it got there or how long each step
took.

## Decision

Every transition writes one row to `remediation_event` (`from_state`, `to_state`, `kind`, `detail`,
`created_at`) in the same transaction as the state column update. The table is never updated or
deleted from. Headline timestamps are additionally denormalised onto `remediation` so the common
durations are a subtraction rather than a window function.

## Alternatives considered

| Option | Why not |
|---|---|
| Mutable status column only | Cannot answer any duration or cycle-count question |
| Reconstruct history from application logs | Logs are not queryable, not transactional with the state change, and are routinely rotated away |
| Event log only, no denormalised timestamps | Every dashboard query becomes a window function over the log; correct but needlessly slow and hard to read |

## Consequences

Every metric in the observability spec is derivable, and the table doubles as the audit trail —
stalled sessions and abandoned attempts stay visible instead of being tidied away, which the project
treats as a deliverable rather than an embarrassment. The cost is unbounded growth and the
denormalised timestamps needing to stay consistent with the log; both are covered by tests.

**What would tell us this was wrong:** the two representations disagreeing in practice, which would
mean the "same transaction" rule is being violated somewhere.
