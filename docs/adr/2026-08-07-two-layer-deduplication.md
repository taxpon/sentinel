---
title: Deduplicate at both the delivery and the remediation layer
status: accepted
date: 2026-08-07
type: architecture
areas: [pipeline, data]
tasks: [T21]
files: [src/sentinel/policy/dedup.py]
specs: [docs/06-event-pipeline.md, docs/03-data-model.md]
supersedes:
---

# Deduplicate at both the delivery and the remediation layer

## Context

Two different things can cause duplicate work. GitHub redelivers a webhook after a timeout or
failure, producing the *same* event twice. Separately, several *different* events can describe the
same underlying issue — a label removed and re-added, an issue reopened, a comment — each of which
would independently look like a reason to start work.

## Decision

Enforce both in the database rather than in application logic:

- `webhook_delivery.delivery_id` is `UNIQUE` — a redelivered event is recognised and answered `200`.
- `remediation (repo, issue_number)` is `UNIQUE`, and the worker creates rows with
  `INSERT … ON CONFLICT DO NOTHING RETURNING id`, so at most one Devin session exists per issue.

## Alternatives considered

| Option | Why not |
|---|---|
| Only deduplicate on delivery id | Stops replays but not two genuinely different events about one issue — the more expensive failure, since it burns ACUs on a duplicate session |
| Only deduplicate on the issue | Lets a redelivered event pass through and do work twice before the domain check settles |
| Application-level checks (read, then write) | Racy under concurrent workers; the window between check and insert is exactly where duplicates appear |

## Consequences

Duplicate protection holds under concurrency without any lock service, and correctness does not
depend on remembering to call a helper. It does mean a duplicate delivery must be answered `200`
rather than an error status, since an error would make GitHub retry it indefinitely.

**What would tell us this was wrong:** legitimately wanting a second session for the same issue —
for example a deliberate retry after a human fix. That would need an explicit new state rather than
a relaxed constraint.
