"""The queue, tested where its correctness actually lives: on more than one connection.

`.claude/rules/testing.md` says Postgres is real here because SQLite cannot emulate
`FOR UPDATE SKIP LOCKED`. That is only half of it — a *single* session cannot emulate it either. On
one connection the `status = 'pending'` predicate alone hides a row this connection already claimed,
so the whole file would pass with the locking clause deleted. Every claiming test below therefore
opens two or three sessions from `session_factory`, leaves their transactions open at the same time,
and sets `lock_timeout` so a claimer that waits fails in a second instead of hanging the suite.

That was checked rather than assumed. Deleting `FOR UPDATE SKIP LOCKED` from `sentinel.queue.CLAIM`
— and, separately, weakening it to a plain `FOR UPDATE` — fails four tests with a `lock_timeout`
error: `test_two_workers_claim_disjoint_jobs_without_blocking`,
`test_a_worker_finding_only_locked_rows_gets_nothing`,
`test_concurrent_workers_claim_every_job_exactly_once` and
`test_an_expired_lease_is_reclaimed_while_a_live_one_is_skipped`.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import pytest
from sqlalchemy import Row, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from factories import a_remediation
from sentinel import queue
from sentinel.config import Settings
from sentinel.queue import ClaimedJob, JobKind, JobStatus, LeaseLost

WORKER = "worker-1"

# Long enough that a slow machine cannot expire a lease the test means to be live, short enough that
# `test_an_expired_lease_is_reclaimed_while_a_live_one_is_skipped` can backdate past it.
LEASE = datetime.timedelta(seconds=900)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The suite's settings with the two policy limits pinned, so the assertions state their own
    numbers rather than inheriting whatever the defaults happen to be."""
    return settings.model_copy(
        update={
            "job_lease_timeout_seconds": int(LEASE.total_seconds()),
            "max_job_attempts": 5,
        }
    )


# --- Helpers -------------------------------------------------------------------------------------


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """`conftest`'s single session, with `lock_timeout` — no connection in this file may wait.

    Overridden here rather than left alone because a test that waits on a lock does not fail, it
    hangs, and the tests below deliberately arrange for locks to be met.
    """
    async with session_factory() as session:
        await fail_fast_on_locks(session)
        yield session


async def fail_fast_on_locks(session: AsyncSession) -> None:
    """Make a lock wait an error after a second, for the whole session and not just this
    transaction.

    `SET LOCAL` would be undone by the first `commit()`, and a claim made after one would go back to
    waiting for ever — which is how a suite that means to prove `SKIP LOCKED` ends up proving
    nothing while appearing to run. The setting cannot escape the test either: `conftest` builds an
    engine per test and disposes it, so the connection carrying it is never handed to another one.
    """
    await session.execute(text("SET lock_timeout = '1s'"))


@asynccontextmanager
async def worker_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A worker with a connection of its own — the arrangement that makes `SKIP LOCKED` observable.

    A single session cannot contend with itself: it sees its own uncommitted claim, so the
    `status = 'pending'` predicate alone would hide the row and the locking clause would be dead
    code.
    """
    async with session_factory() as session:
        await fail_fast_on_locks(session)
        yield session


async def enqueue_job(
    session: AsyncSession, *, run_after: datetime.datetime | None = None, **payload: object
) -> int:
    job_id = await queue.enqueue(
        session, kind=JobKind.CREATE_SESSION, payload=dict(payload), run_after=run_after
    )
    await session.commit()
    return job_id


async def read_job(session: AsyncSession, job_id: int) -> Row[tuple[object, ...]]:
    """The row as the database holds it, in a transaction of its own, so a test reads committed
    state rather than whatever its own session has cached."""
    await session.rollback()
    return (await session.execute(text("SELECT * FROM job WHERE id = :id"), {"id": job_id})).one()


async def backdate(session: AsyncSession, job_id: int, *, by: datetime.timedelta) -> None:
    """Move a job's `run_after` and `locked_at` into the past — the test's way of letting time
    pass without sleeping through a backoff or a fifteen-minute lease."""
    await session.execute(
        text(
            "UPDATE job SET run_after = run_after - make_interval(secs => :seconds),"
            " locked_at = locked_at - make_interval(secs => :seconds) WHERE id = :id"
        ),
        {"seconds": by.total_seconds(), "id": job_id},
    )
    await session.commit()


async def claim_one(
    session: AsyncSession, settings: Settings, worker_id: str = WORKER
) -> ClaimedJob:
    claimed = await queue.claim(session, worker_id=worker_id, settings=settings)
    assert claimed is not None
    return claimed


async def now(session: AsyncSession) -> datetime.datetime:
    return (await session.execute(text("SELECT now()"))).scalar_one()


# --- Enqueueing ----------------------------------------------------------------------------------


async def test_an_enqueued_job_is_claimable_immediately(
    session: AsyncSession, settings: Settings
) -> None:
    """`run_after` takes its `now()` default, which is what makes an un-backed-off job due."""
    job_id = await enqueue_job(session, issue_number=42876)

    claimed = await claim_one(session, settings)

    assert claimed.id == job_id
    assert claimed.kind == JobKind.CREATE_SESSION
    assert claimed.payload == {"issue_number": 42876}
    assert claimed.attempts == 0
    assert claimed.locked_by == WORKER
    assert claimed.remediation_id is None


async def test_a_job_enqueued_for_later_is_not_claimable_yet(
    session: AsyncSession, settings: Settings
) -> None:
    await enqueue_job(session, run_after=await now(session) + datetime.timedelta(minutes=5))

    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None


async def test_an_enqueued_job_carries_its_remediation(
    session: AsyncSession, settings: Settings
) -> None:
    """The foreign key is real, so a job that belongs to a remediation has to name one that exists;
    `sync_acu` is the kind that legitimately has none."""
    remediation = a_remediation()
    session.add(remediation)
    await session.flush()

    await queue.enqueue(
        session,
        kind=JobKind.RESUME_SESSION,
        payload={"cycle": 1},
        remediation_id=remediation.id,
    )
    await session.commit()

    claimed = await claim_one(session, settings)
    assert claimed.remediation_id == remediation.id
    assert claimed.kind == JobKind.RESUME_SESSION


async def test_nothing_to_claim_returns_none(session: AsyncSession, settings: Settings) -> None:
    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None


async def test_the_oldest_due_job_is_claimed_first(
    session: AsyncSession, settings: Settings
) -> None:
    """`ORDER BY run_after` — the queue is a queue, not a stack."""
    current = await now(session)
    second = await enqueue_job(session, run_after=current - datetime.timedelta(seconds=5))
    first = await enqueue_job(session, run_after=current - datetime.timedelta(seconds=30))

    assert (await claim_one(session, settings)).id == first
    assert (await claim_one(session, settings)).id == second


# --- Concurrency ---------------------------------------------------------------------------------


async def test_two_workers_claim_disjoint_jobs_without_blocking(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """The reason the queue is in Postgres at all.

    Both claims are made on connections of their own and *neither transaction is committed* while
    the other runs, so the second worker meets a row the first has locked and not yet released.
    Without `SKIP LOCKED` it would queue behind that transaction, which this test never ends, and
    `lock_timeout` turns the resulting hang into a failure after a second.

    The two `run_after` values are distinct so the ordering is not luck: both workers consider the
    older job first, and the second one has to skip it.
    """
    current = await now(session)
    older = await enqueue_job(session, run_after=current - datetime.timedelta(seconds=30))
    newer = await enqueue_job(session, run_after=current - datetime.timedelta(seconds=5))

    async with (
        worker_session(session_factory) as first,
        worker_session(session_factory) as second,
    ):
        first_claim = await claim_one(first, settings, "worker-a")
        second_claim = await claim_one(second, settings, "worker-b")

        assert first_claim.id == older
        assert second_claim.id == newer
        assert first_claim.locked_by == "worker-a"
        assert second_claim.locked_by == "worker-b"


async def test_a_worker_finding_only_locked_rows_gets_nothing(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Skipping is not the same as waiting.

    Two jobs, both held by uncommitted claims: the third worker must return empty-handed
    immediately. Without `SKIP LOCKED` it waits for a transaction that has not finished, which is
    the failure `lock_timeout` makes visible.
    """
    await enqueue_job(session, issue_number=1)
    await enqueue_job(session, issue_number=2)

    async with (
        worker_session(session_factory) as first,
        worker_session(session_factory) as second,
        worker_session(session_factory) as third,
    ):
        await claim_one(first, settings, "worker-a")
        await claim_one(second, settings, "worker-b")

        assert await queue.claim(third, worker_id="worker-c", settings=settings) is None


async def test_concurrent_workers_claim_every_job_exactly_once(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Six workers, six jobs, all claiming at once on six connections: six distinct ids.

    `asyncio.gather` overlaps the statements rather than merely interleaving two of them, and no
    transaction is committed until every claim has been made, so no lock is released early.

    `return_exceptions=True` is load-bearing: `gather` does not cancel the others when one raises,
    and unwinding the stack while five statements are still waiting on locks closes connections
    from under them. Collecting every result first means a failure is reported as a failure rather
    than as a wedged test run.
    """
    expected = {await enqueue_job(session, issue_number=index) for index in range(6)}

    async def claim_with(worker_id: str, session: AsyncSession) -> int:
        return (await claim_one(session, settings, worker_id)).id

    async with AsyncExitStack() as stack:
        workers = [
            await stack.enter_async_context(worker_session(session_factory)) for _ in range(6)
        ]
        claimed = await asyncio.gather(
            *(claim_with(f"worker-{index}", worker) for index, worker in enumerate(workers)),
            return_exceptions=True,
        )

    assert [result for result in claimed if isinstance(result, BaseException)] == []
    assert set(claimed) == expected


async def test_a_claim_that_waits_is_a_test_failure_not_a_hang(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """The guard the concurrency tests rest on, asserted directly.

    A plain `FOR UPDATE` over the same row does block, and `lock_timeout` turns that into a
    `DBAPIError` rather than a suite that never finishes. If this ever stops raising, the tests
    above have lost their teeth and would hang instead of failing.
    """
    job_id = await enqueue_job(session)

    async with (
        worker_session(session_factory) as first,
        worker_session(session_factory) as second,
    ):
        await claim_one(first, settings)
        with pytest.raises(DBAPIError):
            await second.execute(
                text("SELECT id FROM job WHERE id = :id FOR UPDATE"), {"id": job_id}
            )


# --- Leases --------------------------------------------------------------------------------------


async def test_an_expired_lease_is_reclaimed_while_a_live_one_is_skipped(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """A worker that dies holding a job must not strand it — and a worker still holding one must
    not have it taken away.

    Both jobs are `running`. The dead worker's lease is older than `JOB_LEASE_TIMEOUT_SECONDS` and
    its transaction is long gone; the live worker's claim is uncommitted and its row still locked,
    so `SKIP LOCKED` is what keeps the reclaimer off it. `attempts` is untouched by the reclaim:
    losing a lease is recovery, not a failed attempt.
    """
    current = await now(session)
    stranded = await enqueue_job(session, run_after=current - datetime.timedelta(seconds=30))
    held = await enqueue_job(session, run_after=current - datetime.timedelta(seconds=5))

    async with worker_session(session_factory) as dead:
        assert (await claim_one(dead, settings, "dead-worker")).id == stranded
        await dead.commit()

    async with worker_session(session_factory) as live:
        # Claimed while the dead worker's lease is still live, so this one gets the other job.
        assert (await claim_one(live, settings, "live-worker")).id == held
        await backdate(session, stranded, by=LEASE + datetime.timedelta(minutes=5))

        async with worker_session(session_factory) as reclaimer:
            reclaimed = await queue.claim(reclaimer, worker_id="reclaimer", settings=settings)
            assert reclaimed is not None
            assert (reclaimed.id, reclaimed.locked_by, reclaimed.attempts) == (
                stranded,
                "reclaimer",
                0,
            )
            await reclaimer.commit()

            # `held` is still `pending` as far as this connection can see — the live worker's claim
            # is uncommitted — so the subquery selects its id and `SKIP LOCKED` is the only reason
            # this returns instead of waiting for a transaction that has not finished.
            assert await queue.claim(reclaimer, worker_id="reclaimer", settings=settings) is None, (
                f"job {held} is held under a live lease and must not be reclaimable"
            )


async def test_a_lease_just_short_of_the_timeout_is_not_reclaimed(
    session: AsyncSession, settings: Settings
) -> None:
    """The boundary is `JOB_LEASE_TIMEOUT_SECONDS`, not "some time"."""
    job_id = await enqueue_job(session)
    await claim_one(session, settings, "dead-worker")
    await session.commit()
    await backdate(session, job_id, by=LEASE - datetime.timedelta(seconds=30))

    assert await queue.claim(session, worker_id="reclaimer", settings=settings) is None

    await backdate(session, job_id, by=datetime.timedelta(seconds=60))
    assert await queue.claim(session, worker_id="reclaimer", settings=settings) is not None


async def test_a_worker_that_lost_its_lease_cannot_retire_the_job(
    session: AsyncSession, settings: Settings
) -> None:
    """The fence. The stalled worker's `complete()` must not retire a job somebody else is running,
    and the reclaimer's must still work.
    """
    job_id = await enqueue_job(session)
    stalled = await claim_one(session, settings, "stalled-worker")
    await session.commit()
    await backdate(session, job_id, by=LEASE + datetime.timedelta(minutes=1))
    reclaimed = await claim_one(session, settings, "reclaimer")
    await session.commit()

    with pytest.raises(LeaseLost) as raised:
        await queue.complete(session, stalled)
    assert raised.value.job_id == job_id
    assert raised.value.worker_id == "stalled-worker"

    await queue.complete(session, reclaimed)
    await session.commit()
    assert (await read_job(session, job_id)).status == JobStatus.DONE


async def test_a_lost_lease_also_blocks_failing_and_deferring(
    session: AsyncSession, settings: Settings
) -> None:
    """All three releases are fenced, not only completion — a stalled worker must not be able to
    reschedule or defer a job another worker owns."""
    job_id = await enqueue_job(session)
    stalled = await claim_one(session, settings, "stalled-worker")
    await session.commit()
    await backdate(session, job_id, by=LEASE + datetime.timedelta(minutes=1))
    await claim_one(session, settings, "reclaimer")
    await session.commit()

    with pytest.raises(LeaseLost):
        await queue.fail(session, stalled, error="boom", settings=settings)
    with pytest.raises(LeaseLost):
        await queue.defer(session, stalled)

    row = await read_job(session, job_id)
    assert (row.status, row.attempts, row.locked_by) == (JobStatus.RUNNING, 0, "reclaimer")


# --- Completion ----------------------------------------------------------------------------------


async def test_a_completed_job_is_done_and_gone_from_the_queue(
    session: AsyncSession, settings: Settings
) -> None:
    job_id = await enqueue_job(session)
    claimed = await claim_one(session, settings)

    await queue.complete(session, claimed)
    await session.commit()

    row = await read_job(session, job_id)
    assert (row.status, row.locked_by, row.locked_at) == (JobStatus.DONE, None, None)
    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None


# --- Backoff -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(1, 10), (2, 20), (3, 40), (4, 80), (5, 160), (6, 320), (7, 600), (8, 600)],
)
def test_the_backoff_schedule_is_the_one_the_spec_states(attempts: int, seconds: int) -> None:
    """`2^attempts * 5 s`, capped at ten minutes. `attempts` is the count after the failure, so the
    first retry waits ten seconds. Jitter at its maximum is the schedule itself."""
    assert queue.backoff_delay(attempts, jitter=lambda: 1.0) == datetime.timedelta(seconds=seconds)


@pytest.mark.parametrize("attempts", range(1, 9))
def test_jitter_takes_at_most_half_the_delay_away(attempts: int) -> None:
    """Equal jitter: the delay is never more than the schedule — which is what keeps the ten-minute
    cap a real ceiling — and never less than half of it, which is what keeps it a backoff."""
    scheduled = queue.backoff_delay(attempts, jitter=lambda: 1.0)

    assert queue.backoff_delay(attempts, jitter=lambda: 0.0) == scheduled / 2
    drawn = [queue.backoff_delay(attempts) for _ in range(200)]
    assert all(scheduled / 2 <= delay <= scheduled for delay in drawn)
    assert len(set(drawn)) > 1, "the delay is meant to be jittered, not fixed"


async def test_a_retryable_failure_is_rescheduled_with_backoff(
    session: AsyncSession, settings: Settings
) -> None:
    """The row after one failure: one attempt spent, the error recorded, the lock released, and
    `run_after` inside the jittered window for attempt one."""
    job_id = await enqueue_job(session)
    claimed = await claim_one(session, settings)
    before = await now(session)

    outcome = await queue.fail(session, claimed, error="502 from Devin", settings=settings)
    await session.commit()

    assert outcome is JobStatus.PENDING
    row = await read_job(session, job_id)
    assert (row.status, row.attempts, row.last_error) == (
        JobStatus.PENDING,
        1,
        "502 from Devin",
    )
    assert (row.locked_by, row.locked_at) == (None, None)
    scheduled = queue.backoff_delay(1, jitter=lambda: 1.0)
    assert before + scheduled / 2 <= row.run_after <= before + scheduled
    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None


async def test_a_retried_job_is_claimable_again_once_its_backoff_has_passed(
    session: AsyncSession, settings: Settings
) -> None:
    job_id = await enqueue_job(session)
    await queue.fail(session, await claim_one(session, settings), error="boom", settings=settings)
    await session.commit()
    await backdate(session, job_id, by=queue.BACKOFF_CAP)

    reclaimed = await claim_one(session, settings)
    assert (reclaimed.id, reclaimed.attempts, reclaimed.last_error) == (job_id, 1, "boom")


async def test_max_job_attempts_is_honoured(session: AsyncSession, settings: Settings) -> None:
    """`MAX_JOB_ATTEMPTS` failures in total, then the job is `failed` and never claimed again —
    the point at which the caller transitions the remediation to `FAILED`."""
    settings = settings.model_copy(update={"max_job_attempts": 3})
    job_id = await enqueue_job(session)

    outcomes = []
    for _ in range(3):
        claimed = await claim_one(session, settings)
        outcomes.append(
            await queue.fail(session, claimed, error="429 from Devin", settings=settings)
        )
        await session.commit()
        await backdate(session, job_id, by=queue.BACKOFF_CAP)

    assert outcomes == [JobStatus.PENDING, JobStatus.PENDING, JobStatus.FAILED]
    row = await read_job(session, job_id)
    assert (row.status, row.attempts) == (JobStatus.FAILED, 3)
    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None


async def test_a_non_retryable_failure_gives_up_on_the_first_attempt(
    session: AsyncSession, settings: Settings
) -> None:
    """A `4xx` that is not `429` will fail identically next time; retrying it only wastes quota."""
    job_id = await enqueue_job(session)
    claimed = await claim_one(session, settings)

    outcome = await queue.fail(
        session, claimed, error="422 invalid playbook", retryable=False, settings=settings
    )
    await session.commit()

    assert outcome is JobStatus.FAILED
    row = await read_job(session, job_id)
    assert (row.status, row.attempts, row.last_error) == (
        JobStatus.FAILED,
        1,
        "422 invalid playbook",
    )
    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None


# --- Deferral ------------------------------------------------------------------------------------


async def test_a_deferred_job_waits_and_then_is_claimable_again(
    session: AsyncSession, settings: Settings
) -> None:
    """Deferral rests in its own status so an operator can tell a queue held back by the
    concurrency cap from one backing off (`docs/09-operations.md`), and `run_after` is what brings
    it back a minute later."""
    job_id = await enqueue_job(session)
    claimed = await claim_one(session, settings)
    before = await now(session)

    await queue.defer(session, claimed)
    await session.commit()

    row = await read_job(session, job_id)
    assert (row.status, row.locked_by, row.locked_at) == (JobStatus.DEFERRED, None, None)
    assert row.run_after >= before + queue.DEFER_DELAY
    assert await queue.claim(session, worker_id=WORKER, settings=settings) is None

    await backdate(session, job_id, by=queue.DEFER_DELAY)
    assert (await claim_one(session, settings)).id == job_id


async def test_deferral_does_not_consume_the_retry_budget(
    session: AsyncSession, settings: Settings
) -> None:
    """The distinction T21 depends on: a busy pipeline defers the same job far more often than
    `MAX_JOB_ATTEMPTS` allows failures, and must not exhaust its retries doing so.

    The job is deferred twice as many times as the budget permits and still has every attempt
    available, then fails once and shows exactly one spent.
    """
    settings = settings.model_copy(update={"max_job_attempts": 3})
    job_id = await enqueue_job(session)

    for _ in range(6):
        claimed = await claim_one(session, settings)
        assert claimed.attempts == 0
        await queue.defer(session, claimed)
        await session.commit()
        await backdate(session, job_id, by=queue.DEFER_DELAY)

    assert (await read_job(session, job_id)).attempts == 0

    outcome = await queue.fail(
        session, await claim_one(session, settings), error="boom", settings=settings
    )
    await session.commit()
    assert outcome is JobStatus.PENDING
    assert (await read_job(session, job_id)).attempts == 1


async def test_a_deferred_job_can_be_deferred_with_a_delay_of_its_own(
    session: AsyncSession, settings: Settings
) -> None:
    an_hour = datetime.timedelta(hours=1)
    job_id = await enqueue_job(session)
    before = await now(session)

    await queue.defer(session, await claim_one(session, settings), delay=an_hour)
    await session.commit()

    assert (await read_job(session, job_id)).run_after == before + an_hour
