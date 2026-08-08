"""The `worker` process: claim a job, run its handler, and decide how the job is released.

`handlers.py` decides what each job kind *does*. This module decides everything around that — which
job, under whose lease, and what happens when the handler raises. The two halves are separate
because the endings are policy: `docs/06-event-pipeline.md#reliability-policy` is a table about
retries, backoff and attempt budgets, and it is implemented once here rather than in each of the
four handlers.

One claim per iteration, run to completion before the next. Concurrency across a fleet is what the
claiming statement is for (`docs/06-event-pipeline.md#jobs-and-claiming`): several worker processes
are safe by construction, and `MAX_CONCURRENT_SESSIONS` — not the loop — is what bounds how much
Devin is doing at once.

**The claim is committed before the handler runs.** `sentinel.queue` says so and it is not an
optimisation: the row would otherwise stay locked for the length of the call it is meant to be
tracking, and no other worker could observe the job as claimed.

## How a job ends

| Outcome | Ending |
|---|---|
| The handler returned | The handler completed the job itself, in a transaction of its own |
| `LeaseLost` | Nothing. Another worker owns the row and will decide its outcome |
| Rate limited with a `retry_after` | Deferred for exactly that long, spending no attempt |
| Anything else retryable | `fail()` — one attempt, backed off, until `MAX_JOB_ATTEMPTS` |
| Anything else | `fail(retryable=False)` — a `4xx`, a malformed job, an unknown kind |

A job that reaches `JobStatus.FAILED` by either route takes its remediation to `FAILED` with it,
escalated as `docs/04-state-machine.md` requires. That is the last row of the reliability table
followed to its end: a job that will not be tried again and a remediation left in `QUEUED` for ever
is the same thing as losing the work silently.

`LeaseLost` is a normal ending, not an error. It means this worker was slow enough to be reclaimed;
whatever it did is already committed or already rolled back, and the row belongs to somebody else
(`docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md`).
"""

from __future__ import annotations

import asyncio
import datetime
import os
import socket
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import queue
from sentinel.config import Settings, get_settings
from sentinel.db import dispose_engine, get_session_factory
from sentinel.devin.client import DevinClient, DevinError, Sleep
from sentinel.devin.playbooks import MissingPlaybookId, UnknownIssueClass, UnregisteredTag
from sentinel.github.client import GitHubClient, GitHubError, GitHubRateLimited, GitHubUnavailable
from sentinel.models import Remediation
from sentinel.observability.logging import configure_logging, correlate, get_logger
from sentinel.pipeline.handlers import (
    ERROR_EVENT,
    HANDLERS,
    JOB_FAILED,
    Context,
    MalformedJob,
    describe_error,
    fail_remediation,
    load_remediation,
)
from sentinel.pipeline.state import IllegalTransitionError
from sentinel.queue import ClaimedJob, JobStatus, LeaseLost

log = get_logger(__name__)

IDLE_DELAY_SECONDS: Final = 1.0
"""How long the loop waits when the queue is empty.

Not a configured value and not `POLL_INTERVAL_SECONDS`, which is the cadence of asking Devin about
sessions. This is the latency between a webhook being accepted and its work starting, on a table in
the same database the claim already reads: a second is short enough to be invisible in the
time-to-session metric and long enough that an idle deployment is not a busy loop.
"""


class UnknownJobKind(ValueError):
    """A `job.kind` this worker has no handler for.

    Possible by design: `ClaimedJob.kind` is a `str` so that a row written by a newer process does
    not make `claim()` raise in an older one. The job is failed without a retry and stays visible in
    the queue as `failed`, rather than being claimed and abandoned on a loop.
    """


PERMANENT: Final[tuple[type[BaseException], ...]] = (
    UnknownJobKind,
    MalformedJob,
    UnknownIssueClass,
    MissingPlaybookId,
    UnregisteredTag,
    IllegalTransitionError,
)
"""Failures that will be identical on the next attempt, so the queue does not schedule one.

Each is a fault in the job row, the deployment or this code — a payload key that is missing, a
playbook id nobody configured, a trigger applied from a state the spec does not allow. The
reliability policy's "retrying a validation error only wastes quota" is the same argument: the cost
of retrying is real and the chance of a different answer is nil.
"""


def worker_id() -> str:
    """What this process writes into `job.locked_by`.

    Host and pid, because the column is read by an operator asking which worker is holding a job
    (`docs/09-operations.md`) — a random id would answer "one of them". A restarted process cannot
    collide with itself: the pid is only reused once the previous process is gone, and the job it
    was holding is reclaimable by then anyway.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_once(context: Context, *, claimed_by: str) -> bool:
    """Claim and run one job. `False` when there was nothing due.

    The claim is committed before the handler is entered, and the handler releases the job itself —
    so everything this function does after the commit is about a job somebody could reclaim, which
    is why the handlers are written to be run twice.
    """
    async with context.session_factory() as db:
        job = await queue.claim(db, worker_id=claimed_by, settings=context.settings)
        await db.commit()

    if job is None:
        return False

    with correlate(remediation_id=job.remediation_id):
        await run_job(context, job)
    return True


async def run(
    context: Context,
    *,
    claimed_by: str | None = None,
    stop: asyncio.Event | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Drain the queue, wait when it is empty, and repeat until `stop` is set.

    Only an empty queue waits: a backlog is worked through without an idle delay between jobs.
    `sleep` is injectable so a test drives the cadence rather than living through it, exactly as the
    poller and the Devin client allow.
    """
    claimed_by = worker_id() if claimed_by is None else claimed_by
    while stop is None or not stop.is_set():
        if not await run_once(context, claimed_by=claimed_by):
            await sleep(IDLE_DELAY_SECONDS)


async def main(settings: Settings | None = None) -> None:
    """The `worker` process, as `docs/09-operations.md` runs it."""
    settings = get_settings() if settings is None else settings
    configure_logging(settings)
    claimed_by = worker_id()
    log.info("worker.started", worker_id=claimed_by)
    try:
        async with DevinClient(settings) as devin, GitHubClient(settings) as github:
            context = Context(
                session_factory=get_session_factory(),
                devin=devin,
                github=github,
                settings=settings,
            )
            await run(context, claimed_by=claimed_by)
    finally:
        await dispose_engine()


async def run_job(context: Context, job: ClaimedJob) -> None:
    """Run one already-claimed job, and make sure it ends somewhere whatever the handler does.

    Separate from `run_once` because the claim and the run are separate concerns: a caller that
    already holds a lease — a test reproducing a reclaim, an operator draining one job by hand —
    needs the endings without the claiming.
    """
    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise UnknownJobKind(
                f"job {job.id} has kind {job.kind!r}, which this worker cannot run"
            )
        await handler(context, job)
    except LeaseLost as exc:
        # Not a failure: the row belongs to the worker that reclaimed it, and that worker decides
        # its outcome. Recording anything here is precisely what the fence exists to prevent.
        log.warning("worker.lease.lost", job_id=job.id, kind=job.kind, error=str(exc))
    # Broad on purpose: every ending is the queue's, including one nothing here foresaw.
    except Exception as exc:
        await _release_failure(context, job, exc)
    else:
        log.info("worker.job.done", job_id=job.id, kind=job.kind, attempts=job.attempts)


async def _release_failure(context: Context, job: ClaimedJob, exc: Exception) -> None:
    """Put a failed job back, or retire it — and take the remediation with it when it is retired."""
    error = describe_error(exc)
    delay = _rate_limit_delay(exc)
    try:
        async with context.session_factory() as db:
            if delay is not None:
                # Not a failure. GitHub rejected the request before processing it and said when to
                # come back, so the work is still wanted and nothing about it has gone wrong —
                # `docs/adr/2026-08-08-a-rate-limit-with-an-answer-defers-the-job.md`.
                await queue.defer(db, job, delay=delay)
                await db.commit()
                log.warning(
                    "worker.job.rate_limited",
                    job_id=job.id,
                    kind=job.kind,
                    retry_after_seconds=delay.total_seconds(),
                    error=error,
                )
                return

            status = await queue.fail(
                db, job, error=error, retryable=_retryable(exc), settings=context.settings
            )
            if status is JobStatus.FAILED:
                await _fail_remediation(db, job, error)
            await db.commit()
            log.warning(
                "worker.job.failed",
                job_id=job.id,
                kind=job.kind,
                attempts=job.attempts + 1,
                status=status.value,
                error=error,
            )
    except LeaseLost:
        log.warning("worker.lease.lost", job_id=job.id, kind=job.kind, error=error)


async def _fail_remediation(db: AsyncSession, job: ClaimedJob, error: str) -> None:
    """Take the remediation down with a job that will not be tried again.

    `MAX_JOB_ATTEMPTS` "then transitions the remediation to `FAILED`", says the reliability policy;
    a non-retryable failure reaches the same place by the shorter route. Either way the remediation
    escalates rather than resting in the state it happened to be in.

    A job with no remediation — `sync_acu` — has nothing to take down.
    """
    if job.remediation_id is None:
        return
    remediation: Remediation = await load_remediation(db, job)
    await fail_remediation(
        db,
        remediation,
        reason=JOB_FAILED,
        detail={"job_kind": job.kind, "attempts": job.attempts + 1, "error": error},
        kind=ERROR_EVENT,
    )


def _rate_limit_delay(exc: BaseException) -> datetime.timedelta | None:
    """How long GitHub asked us to wait, when it asked and the client could not wait it out.

    `None` for every other failure, including a rate limit GitHub named no delay for: the client
    deliberately does not substitute its own backoff there
    (`docs/adr/2026-08-08-github-waits-are-bounded-and-handed-back-to-the-queue.md`), so there is no
    schedule to honour and the queue's own backoff applies.
    """
    if isinstance(exc, GitHubRateLimited) and exc.retry_after is not None:
        return datetime.timedelta(seconds=exc.retry_after)
    return None


def _retryable(exc: BaseException) -> bool:
    """Whether the queue should schedule another attempt.

    Both clients already answer this for their own failures — `DevinError.retryable` distinguishes a
    `429` from a `422`, and the GitHub client encodes the same split in its exception types. What is
    left is Sentinel's own faults, which are permanent, and everything unforeseen, which is treated
    as transient: an attempt costs a backoff and `MAX_JOB_ATTEMPTS` bounds it, while giving up on
    the first unrecognised exception fails a remediation over a blip.
    """
    if isinstance(exc, DevinError):
        return exc.retryable
    if isinstance(exc, GitHubUnavailable):
        return True
    if isinstance(exc, GitHubError):
        return False
    return not isinstance(exc, PERMANENT)
