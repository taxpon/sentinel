---
title: Use Postgres as the job queue instead of adding Redis
status: accepted
date: 2026-08-07
type: architecture
areas: [pipeline, data]
tasks: [T13]
files: [src/sentinel/queue.py]
specs: [docs/06-event-pipeline.md]
supersedes:
---

# Use Postgres as the job queue instead of adding Redis

## Context

Webhook handling is asynchronous: the API persists an event and enqueues work, and workers pick it
up later. Multiple workers must claim jobs without processing the same one twice. Expected volume is
tens of jobs per day, not thousands per second. Postgres is already a hard dependency for the
domain data.

## Decision

Keep jobs in a `job` table and claim them with a single
`UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *` statement. Retries and
deferrals are expressed as `attempts` and `run_after` on the same row.

## Alternatives considered

| Option | Why not |
|---|---|
| Redis with `arq` or `RQ` | A second stateful service to run, back up and reason about, for a workload that does not need it. It also splits the transaction: enqueueing and updating the remediation could no longer be atomic |
| Celery with a broker | Heavier still, and its operational surface dwarfs the problem |
| In-process background tasks | Lost on restart, and no way to run more than one worker safely |

## Consequences

Compose stays at three services, and enqueue-on-state-change is atomic with the state change itself,
which removes a class of "state advanced but no job was scheduled" bugs. The cost is that throughput
is bounded by Postgres, and the claiming query must be written carefully — it is covered by a
concurrency test with two real workers.

**What would tell us this was wrong:** sustained queue depth, claim contention visible in
`pg_stat_activity`, or a need for delayed fan-out patterns that a table does not express well.
