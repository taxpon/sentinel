"""The job queue: enqueueing, claiming, completion, failure, retry and deferral over `job`.

`docs/06-event-pipeline.md#jobs-and-claiming` gives the claiming statement, and
`docs/adr/2026-08-07-postgres-as-job-queue.md` records why it is a table rather than Redis. The
whole module is that statement plus the four ways a claim can end.

**Nothing here commits.** Every function runs inside the caller's transaction, because that is the
property the queue exists for: the webhook path writes the state transition, the `remediation_event`
and the enqueued job together or not at all (`docs/06-event-pipeline.md#ingress-path`). A worker
therefore commits its claim before running the handler — otherwise the row stays locked for the
length of the call it is supposed to be tracking.

Four things a caller has to know:

- **A claim is a lease, and the lease is the capability.** `claim()` returns a `ClaimedJob`, and
  `complete()`, `fail()` and `defer()` take it rather than an id: each matches on `locked_by` and
  raises `LeaseLost` if another worker has since reclaimed the row. Without that, a worker that
  stalled past `JOB_LEASE_TIMEOUT_SECONDS` and woke up could retire a job that is running elsewhere.
- **The lease protects the row, not the work. Handlers must be idempotent.** A worker that is merely
  slow, rather than dead, is still holding the job when its lease expires and another worker takes
  it, so the handler runs twice — and for `create_session` that is two Devin sessions and two ACU
  budgets for one issue. Check for your own effect before repeating it.
- **Deferral is not failure.** `defer()` leaves `attempts` alone, so a job held back by the
  concurrency cap does not spend the retry budget it will need if the API later starts failing —
  `docs/06-event-pipeline.md#reliability-policy` is explicit, and T21 depends on it.
- **Reclaiming an expired lease does not spend the budget either.** `attempts` counts failures the
  worker reported, which is what the backoff schedule is computed from; a lost lease is recovery,
  not an attempt. See `docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md`.
"""

from __future__ import annotations

import datetime
import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from sentinel.config import Settings, get_settings
from sentinel.models import Job


class JobStatus(StrEnum):
    """The `job.status` vocabulary of `docs/03-data-model.md`.

    `PENDING` and `DEFERRED` are both resting states that become claimable when `run_after` passes;
    they are distinct so that `select status, count(*) from job` tells an operator whether the queue
    is waiting on a backoff or on the concurrency cap (`docs/09-operations.md`).
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEFERRED = "deferred"


class JobKind(StrEnum):
    """The four kinds of work in `docs/06-event-pipeline.md#jobs-and-claiming`."""

    CREATE_SESSION = "create_session"
    RESUME_SESSION = "resume_session"
    ESCALATE = "escalate"
    SYNC_ACU = "sync_acu"


BACKOFF_BASE: Final = datetime.timedelta(seconds=5)
"""The `5 s` of `run_after = now() + 2^attempts * 5 s`."""

BACKOFF_CAP: Final = datetime.timedelta(minutes=10)
"""The ceiling the same row of the spec puts on the doubling."""

DEFER_DELAY: Final = datetime.timedelta(seconds=60)
"""How long a job deferred by the concurrency cap waits before it is claimable again."""


class LeaseLost(RuntimeError):
    """The job was no longer held by this worker when it tried to release it.

    Raised rather than returned: a worker that has lost its lease has been running a job somebody
    else now owns, and the two must not both decide what happens to the row. Callers that expect it
    — a handler slower than `JOB_LEASE_TIMEOUT_SECONDS` — catch it and abandon the result.

    It says nothing about what the handler already did. By the time this is raised the outbound call
    has been made and its ACUs are spent, and the reclaiming worker will make it again. The queue
    cannot prevent that; only the handler can, by checking for its own effect before repeating it.
    """

    def __init__(self, job_id: int, worker_id: str) -> None:
        super().__init__(f"job {job_id} is no longer held by {worker_id!r}")
        self.job_id = job_id
        self.worker_id = worker_id


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One job, held by the worker that claimed it, until it is released or the lease expires.

    `kind` is `str` and not `JobKind` deliberately: the column is `text`, and a row written by a
    future version of another process must not make `claim()` raise in this one. Comparing it
    against `JobKind` works — `StrEnum` members are strings.
    """

    id: int
    remediation_id: int | None
    kind: str
    payload: dict[str, Any]
    attempts: int
    run_after: datetime.datetime
    locked_by: str
    locked_at: datetime.datetime
    last_error: str | None


# The statement of `docs/06-event-pipeline.md#jobs-and-claiming`, with the predicate widened to the
# two cases the prose beneath it adds: a `deferred` row is claimable once `run_after` passes, and a
# lease older than `JOB_LEASE_TIMEOUT_SECONDS` belongs to a worker that is not coming back. Both are
# selected by the same `FOR UPDATE SKIP LOCKED` subquery, so a worker still takes exactly one row
# and never waits for another worker's transaction.
#
# `id` tie-breaks the ordering, which the spec's statement leaves open. `run_after` defaults to
# `now()` — `transaction_timestamp()` — so two jobs enqueued in one transaction carry the same value
# to the microsecond and would otherwise be claimed in whatever order the plan happened to produce.
# The ingress writes one job per transaction today, so this decides nothing yet; it costs a column
# in an ORDER BY and stops the queue being a queue only by coincidence.
CLAIM = text("""
    UPDATE job SET status = 'running', locked_by = :worker_id, locked_at = now()
    WHERE id = (
        SELECT id FROM job
        WHERE (status IN ('pending', 'deferred') AND run_after <= now())
           OR (status = 'running' AND locked_at <= now() - make_interval(secs => :lease_seconds))
        ORDER BY run_after, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
""")

# `locked_by = :worker_id` is the fence on all three releases: it is what stops a worker whose lease
# expired from retiring, retrying or deferring a job another worker has since claimed. Each returns
# the id it wrote, so "matched nothing" is a missing row rather than a driver-reported count.
COMPLETE = text("""
    UPDATE job SET status = 'done', locked_by = NULL, locked_at = NULL
    WHERE id = :job_id AND locked_by = :worker_id
    RETURNING id
""")

RETRY = text("""
    UPDATE job
    SET status = 'pending',
        attempts = attempts + 1,
        run_after = now() + make_interval(secs => :delay_seconds),
        last_error = :error,
        locked_by = NULL,
        locked_at = NULL
    WHERE id = :job_id AND locked_by = :worker_id
    RETURNING id
""")

GIVE_UP = text("""
    UPDATE job
    SET status = 'failed',
        attempts = attempts + 1,
        last_error = :error,
        locked_by = NULL,
        locked_at = NULL
    WHERE id = :job_id AND locked_by = :worker_id
    RETURNING id
""")

# `attempts` is untouched, which is the whole point of the row: the job is being held back, not
# retried, and the retry budget is still intact when the work eventually runs.
DEFER = text("""
    UPDATE job
    SET status = 'deferred',
        run_after = now() + make_interval(secs => :delay_seconds),
        locked_by = NULL,
        locked_at = NULL
    WHERE id = :job_id AND locked_by = :worker_id
    RETURNING id
""")


def backoff_delay(
    attempts: int, *, jitter: Callable[[], float] = random.random
) -> datetime.timedelta:
    """`2^attempts * 5 s` with jitter, capped at ten minutes — the retry row of the spec.

    `attempts` is the count *after* the failure, so the first retry waits ten seconds and the fourth
    eighty. Jitter is equal jitter: half the schedule, plus a random half. `jitter` returns a value
    in `[0, 1)`, so the result lies in `[base/2, base]` — never so short that a fleet of workers
    retries a rate-limited API in lockstep, and never longer than the schedule says, which keeps the
    cap a real ceiling. Injectable so that a test can assert the schedule exactly.

    At `MAX_JOB_ATTEMPTS = 5` the cap is unreachable: `fail()` gives up rather than compute a delay
    for the fifth, so nothing waits longer than eighty seconds unless the limit is raised.
    """
    # `1 << attempts` rather than `2**attempts`: identical, and typed `int`. `int.__pow__` is
    # `Any`, which would make the whole expression untyped and the return value unchecked.
    base = min(BACKOFF_BASE * (1 << attempts), BACKOFF_CAP)
    return base * (0.5 + 0.5 * jitter())


async def enqueue(
    session: AsyncSession,
    *,
    kind: JobKind,
    payload: dict[str, Any],
    remediation_id: int | None = None,
    run_after: datetime.datetime | None = None,
) -> int:
    """Add a job and return its id, in the caller's transaction.

    `run_after` is left to the column's `now()` default unless given, which is what makes an
    ordinary job claimable the moment the enqueueing transaction commits.
    """
    values: dict[str, Any] = {
        "remediation_id": remediation_id,
        "kind": kind,
        "payload": payload,
        "status": JobStatus.PENDING,
    }
    if run_after is not None:
        values["run_after"] = run_after
    result = await session.execute(insert(Job).values(**values).returning(Job.id))
    return result.scalar_one()


async def claim(
    session: AsyncSession, *, worker_id: str, settings: Settings | None = None
) -> ClaimedJob | None:
    """Take one due job — or `None` if there is nothing to do.

    Due means: pending or deferred with `run_after` in the past, or running under a lease older than
    `JOB_LEASE_TIMEOUT_SECONDS`. Rows another worker is claiming right now are skipped rather than
    waited for, so two workers never contend and neither ever blocks.

    The claim is only durable once the caller commits, and the row stays locked until it does.
    """
    settings = get_settings() if settings is None else settings
    result = await session.execute(
        CLAIM,
        {
            "worker_id": worker_id,
            "lease_seconds": float(settings.job_lease_timeout_seconds),
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    return ClaimedJob(
        id=row["id"],
        remediation_id=row["remediation_id"],
        kind=row["kind"],
        payload=row["payload"],
        attempts=row["attempts"],
        run_after=row["run_after"],
        locked_by=row["locked_by"],
        locked_at=row["locked_at"],
        last_error=row["last_error"],
    )


async def complete(session: AsyncSession, job: ClaimedJob) -> None:
    """Retire a job that succeeded."""
    await _release(session, COMPLETE, job, {})


async def fail(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    error: str,
    retryable: bool = True,
    settings: Settings | None = None,
) -> JobStatus:
    """Record a failure and return where it left the job: `PENDING` for a scheduled retry,
    `FAILED` for one that will not be tried again.

    `attempts` goes up either way. A retryable failure — `429`, `5xx`, a network error — is
    rescheduled with `backoff_delay()` until `MAX_JOB_ATTEMPTS` is spent, after which the caller
    transitions the remediation to `FAILED`. `retryable=False` is for the `4xx` that will fail
    identically next time; retrying a validation error only wastes quota.
    """
    settings = get_settings() if settings is None else settings
    attempts = job.attempts + 1
    if not retryable or attempts >= settings.max_job_attempts:
        await _release(session, GIVE_UP, job, {"error": error})
        return JobStatus.FAILED
    delay = backoff_delay(attempts)
    await _release(session, RETRY, job, {"error": error, "delay_seconds": delay.total_seconds()})
    return JobStatus.PENDING


async def defer(
    session: AsyncSession, job: ClaimedJob, *, delay: datetime.timedelta = DEFER_DELAY
) -> None:
    """Put a job back because a policy said "not now", without spending an attempt.

    The concurrency cap is the case the spec names: at `MAX_CONCURRENT_SESSIONS` the work is still
    valid and still wanted, so nothing about it has failed. A job may be deferred any number of
    times; only `fail()` moves it towards `MAX_JOB_ATTEMPTS`.
    """
    await _release(session, DEFER, job, {"delay_seconds": delay.total_seconds()})


async def _release(
    session: AsyncSession, statement: TextClause, job: ClaimedJob, parameters: dict[str, Any]
) -> None:
    """Run one of the fenced release statements, or raise `LeaseLost` if it matched nothing."""
    result = await session.execute(
        statement, {"job_id": job.id, "worker_id": job.locked_by, **parameters}
    )
    if result.scalar_one_or_none() is None:
        raise LeaseLost(job.id, job.locked_by)
