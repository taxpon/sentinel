---
title: One statement claims, un-defers and reclaims, and the lease it grants fences the release
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline, data]
tasks: [T13]
files: [src/sentinel/queue.py]
specs: [docs/06-event-pipeline.md, docs/03-data-model.md]
supersedes:
---

# One statement claims, un-defers and reclaims, and the lease it grants fences the release

## Context

[06](../06-event-pipeline.md#jobs-and-claiming) writes the claiming statement out in full, and its
subquery selects `status = 'pending' AND run_after <= now()`. The prose and the policy table around
it describe two more rows that must eventually run:

- a job held back by the concurrency cap rests in `status = 'deferred'` with
  `run_after = now() + 60 s`, and [03](../03-data-model.md) lists `deferred` as a status of its own,
  which [09](../09-operations.md) tells an operator to count;
- a lease older than `JOB_LEASE_TIMEOUT` belongs to a worker that is not coming back, and its job
  must not be stranded in `running` for ever.

Neither is matched by the statement as written. Separately, once expired leases are reclaimable, two
workers can hold the same job in sequence: the reclaimer, and the original worker if it wakes up.
Both would otherwise be able to write the row's outcome.

## Decision

One statement, keeping the spec's `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`
shape, with the subquery predicate widened to all three cases:

```sql
WHERE (status IN ('pending', 'deferred') AND run_after <= now())
   OR (status = 'running' AND locked_at <= now() - make_interval(secs => :lease_seconds))
```

Reclaiming does not change `attempts`. `attempts` counts failures a worker reported, because it is
what the backoff schedule is computed from; a lost lease is recovery, not an attempt.

`claim()` returns a `ClaimedJob`, and `complete()`, `fail()` and `defer()` take that object rather
than an id. Each matches `locked_by = :worker_id` and raises `LeaseLost` when it matches no row, so
only the worker that currently holds the lease can decide the job's outcome.

## Alternatives considered

| Option | Why not |
|---|---|
| Deferral writes `status = 'pending'` with a future `run_after` | The claim statement then needs no change at all, but `deferred` never appears in the table and the runbook's `select status, count(*) from job` can no longer tell a queue held back by the concurrency cap from one backing off after failures |
| A second statement, or a sweeper process, to reclaim expired leases | Two statements race each other in a way one does not, and a sweeper is a third process to deploy for something a widened predicate does for free |
| Reclaiming counts as an attempt | It bounds a job that repeatedly kills its worker, but it also conflates two different failures: the backoff delay would then be computed from a number that includes crashes the remote API never saw, and a job could exhaust its retry budget without a single request having failed |
| Release by id, unfenced | Smaller signature, but a worker that stalled past the lease and then finished would retire, retry or defer a job another worker is running at that moment |
| A fence that also prevented the second execution — an idempotency token sent to Devin, or a lock held for the length of the call | Nothing in the v3 API takes such a token, and a lock held across an outbound call is this lease again with a worse failure mode: a worker that dies mid-call holds it until it expires anyway |

## Consequences

The spec's statement stays recognisable — the shape, the ordering and the locking clause are
unchanged — and there is still exactly one round trip to take a job. A deferred job needs no special
handling anywhere else: `run_after` is the only thing that decides when work becomes due, whoever
wrote it and for whatever reason. `id` was added to the `ORDER BY`, because `run_after` defaults to
`transaction_timestamp()` and two jobs enqueued in one transaction otherwise sort arbitrarily — the
same hazard [the live panels](./2026-08-08-live-panels-sort-client-side-with-an-id-tiebreak.md)
address for `remediation_event`.

The cost is that the predicate is an `OR` over two clauses, so the planner cannot always use
`ix_job_status_run_after` for the whole of it. At tens of jobs a day that is not worth a second
index.

**The fence protects the row, not the work: handlers have to be idempotent.** A lease can expire on
a worker that is merely slow rather than dead, and by the time it is reclaimed that worker has
already made its outbound call. The reclaiming worker then makes it again. `LeaseLost` tells the
first worker its result is unwanted; it cannot tell Devin to forget the session it just created. For
`create_session` that is two `POST /v3/…/sessions` for one issue — two sessions, two `max_acu_limit`
budgets, and one of them orphaned. Devin has [no outbound webhook](./2026-08-07-poller-drives-state-machine.md),
so the poller only ever sees the session id the winner wrote, and the orphan is never observed and
never cleaned up. Every handler must therefore check for its own effect before repeating it:
`create_session` reads `remediation.devin_session_id` and does not post if one is already there.
Raising `JOB_LEASE_TIMEOUT_SECONDS` narrows the window but cannot close it.

Not incrementing `attempts` on reclaim leaves one case unbounded: a job whose handler reliably kills
its worker is reclaimed for ever, once per `JOB_LEASE_TIMEOUT`. It is visible rather than silent —
the row sits in `running` with an ageing `locked_at`.

**What would tell us this was wrong:** a job reclaimed many times over, which would mean the crash
loop is real and `attempts` has to cover it; two Devin sessions carrying the same `run:<delivery_id>`
tag, which would mean a handler is not idempotent and the window is being hit in practice; or
`deferred` rows accumulating faster than they are claimed, which would mean the 60-second retry is
the wrong shape for the concurrency cap and it wants a queue of its own.
