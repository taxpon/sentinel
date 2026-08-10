"""The policy gates, tested on the two things they are trusted to get right.

`docs/08-testing.md` names both cases: at the concurrency cap the job is **deferred, not failed**,
and the deferral does not consume the retry budget; over `DAILY_ACU_BUDGET` the remediation is
`BLOCKED` with the right reason and an escalation job is enqueued. Both are seeded — in-flight
remediations, `acu_ledger` rows, `remediation.acus_consumed` — rather than mocked, because the
queries are the policy: a guard that reads the wrong rows passes every test written against a
patched counter.

The budget guard is the one place a wrong answer spends real money, so its comparison is asserted at
the boundary from both sides, and every way Devin's own figure can go missing has a test that still
refuses. `test_the_budget_guard_is_not_permissive_when_devin_cannot_be_read` is the specific one:
`daily_consumption()` degrading to `Unavailable` must never be the reason a session starts.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import Configure, FakeAPI
from factories import ISSUE_CLASS, REPO, a_consumption_body, a_remediation, an_acu_ledger_entry
from sentinel import queue
from sentinel.config import Settings
from sentinel.devin.client import DevinAPIError, DevinClient
from sentinel.devin.playbooks import UnknownIssueClass, acu_cap_for
from sentinel.devin.schemas import (
    Available,
    Capability,
    Consumption,
    Degradable,
    Unavailability,
    Unavailable,
)
from sentinel.models import Remediation
from sentinel.pipeline.state import State
from sentinel.policy import (
    BUDGET_EXHAUSTED,
    CONCURRENCY_CAP_REACHED,
    REMEDIATION_TERMINAL,
    SESSION_ALREADY_CREATED,
    DailySpend,
    Upsert,
    Verdict,
    admit_session,
    block,
    budget_allows,
    ensure_remediation,
    sessions_in_flight,
)
from sentinel.queue import ClaimedJob, JobKind, JobStatus, LeaseLost

WORKER = "worker-1"

CONSUMPTION_PATH = "/v3/organizations/org-abc123/consumption/daily"

# The suite's own day, not the factories' fixed `LABELED_AT`: the budget guard asks what *today*
# cost, and a test seeded on a date in the past would pass in 2026 and fail for ever after.
NOW = datetime.datetime.now(datetime.UTC)
TODAY = NOW.date()

MAX_CONCURRENT = 3
BUDGET = 100.0

# `security` is the class the factories use, and `SECURITY_FIX` caps a session at 20 ACUs. Every
# budget assertion below is stated as "the spend, plus this, against BUDGET".
CAP = acu_cap_for(ISSUE_CLASS)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The suite's settings with both policy limits pinned, so the assertions state their own
    numbers rather than inheriting whatever the defaults happen to be."""
    return settings.model_copy(
        update={"max_concurrent_sessions": MAX_CONCURRENT, "daily_acu_budget": BUDGET}
    )


# --- Doubles and helpers -------------------------------------------------------------------------


class FakeConsumption:
    """A `ConsumptionSource` that answers with whatever the test wants, including nothing.

    The Devin client is exercised over `respx` in the tests that go through it; this is for the
    cases that are about the *guard's* branch rather than about the HTTP, and for the ones that
    have to be certain the endpoint was consulted exactly once.
    """

    def __init__(self, result: Degradable[Consumption]) -> None:
        self.result = result
        self.calls = 0

    async def daily_consumption(self) -> Degradable[Consumption]:
        self.calls += 1
        return self.result


def reports(acus: float, *, day: datetime.date = TODAY) -> FakeConsumption:
    """Devin reporting `acus` spent on `day`.

    Validated from the body the endpoint really sends rather than constructed field by field: a
    double built out of the model's own fields cannot tell anyone whether the model reads the
    response, which is exactly what was wrong with the envelope this used to skip past.
    """
    return FakeConsumption(Available(Consumption.model_validate(a_consumption_body((day, acus)))))


def unavailable(reason: Unavailability = Unavailability.FORBIDDEN) -> FakeConsumption:
    """Devin refusing the capability — no enterprise scope, or an endpoint this organisation does
    not have. The guard gets no figure at all from it."""
    return FakeConsumption(Unavailable(capability=Capability.ACU_SPEND, reason=reason))


async def seed_remediation(session: AsyncSession, **overrides: Any) -> Remediation:
    """One remediation, flushed so that it has an id for the foreign keys."""
    remediation = a_remediation(**{"labeled_at": NOW, **overrides})
    session.add(remediation)
    await session.flush()
    return remediation


async def seed_in_flight(
    session: AsyncSession, count: int, *, state: State = State.RUNNING
) -> None:
    """`count` other remediations with Devin working on them, on issues of their own."""
    for offset in range(count):
        session.add(
            a_remediation(
                issue_number=1000 + offset,
                state=str(state),
                devin_session_id=f"devin-{offset}",
                labeled_at=NOW,
            )
        )
    await session.flush()


async def claim_job(
    session: AsyncSession,
    settings: Settings,
    *,
    remediation: Remediation,
    attempts: int = 0,
) -> ClaimedJob:
    """A `create_session` job for `remediation`, claimed by this worker — what the handler holds
    when it calls the policy."""
    job_id = await queue.enqueue(
        session,
        kind=JobKind.CREATE_SESSION,
        payload={"issue_number": remediation.issue_number},
        remediation_id=remediation.id,
    )
    if attempts:
        await session.execute(
            text("UPDATE job SET attempts = :attempts WHERE id = :id"),
            {"attempts": attempts, "id": job_id},
        )
    claimed = await queue.claim(session, worker_id=WORKER, settings=settings)
    assert claimed is not None and claimed.id == job_id
    return claimed


async def read_job(session: AsyncSession, job_id: int) -> Row[Any]:
    return (await session.execute(text("SELECT * FROM job WHERE id = :id"), {"id": job_id})).one()


async def read_jobs(session: AsyncSession, kind: JobKind) -> list[Row[Any]]:
    return list(
        (
            await session.execute(
                text("SELECT * FROM job WHERE kind = :kind ORDER BY id"), {"kind": str(kind)}
            )
        ).all()
    )


async def read_events(session: AsyncSession) -> list[Row[Any]]:
    return list((await session.execute(text("SELECT * FROM remediation_event ORDER BY id"))).all())


# --- Deduplication -------------------------------------------------------------------------------


async def test_create_if_absent_creates_the_remediation_when_there_is_none(
    session: AsyncSession,
) -> None:
    upsert = await ensure_remediation(
        session, repo=REPO, issue_number=7, issue_class=ISSUE_CLASS, labeled_at=NOW
    )

    remediation = (
        await session.execute(text("SELECT * FROM remediation WHERE issue_number = 7"))
    ).one()
    assert upsert == Upsert(remediation_id=remediation.id, created=True)
    assert remediation.state == State.QUEUED
    assert remediation.labeled_at == NOW


async def test_a_second_event_about_one_issue_does_not_create_a_second_remediation(
    session: AsyncSession,
) -> None:
    """The label removed and re-added case. `created=False` is what stops the caller enqueueing a
    second `create_session` job, which is what would open a second Devin session."""
    first = await ensure_remediation(
        session, repo=REPO, issue_number=7, issue_class=ISSUE_CLASS, labeled_at=NOW
    )
    second = await ensure_remediation(
        session,
        repo=REPO,
        issue_number=7,
        issue_class="flaky-test",
        labeled_at=NOW + datetime.timedelta(hours=1),
        state=State.RUNNING,
    )

    assert second == Upsert(remediation_id=first.remediation_id, created=False)
    rows = (await session.execute(text("SELECT * FROM remediation"))).all()
    # The first event starts the clock and names the class; a later one may not restate either.
    assert [(row.id, row.issue_class, row.state, row.labeled_at) for row in rows] == [
        (first.remediation_id, ISSUE_CLASS, State.QUEUED, NOW)
    ]


async def test_create_if_absent_waits_out_a_concurrent_inserter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two workers, two transactions, one issue — the window a read-then-write would lose in.

    `ON CONFLICT DO NOTHING` does not skip an *uncommitted* conflicting row and carry on: the
    second inserter blocks until the first transaction ends. That is what makes the follow-up
    `SELECT` safe, because by the time it runs the row it is looking for has been committed.
    """
    async with session_factory() as first, session_factory() as second:
        created = await ensure_remediation(
            first, repo=REPO, issue_number=7, issue_class=ISSUE_CLASS, labeled_at=NOW
        )

        racing = asyncio.create_task(
            ensure_remediation(
                second, repo=REPO, issue_number=7, issue_class=ISSUE_CLASS, labeled_at=NOW
            )
        )
        await asyncio.sleep(0.2)
        assert not racing.done(), "the second inserter did not wait for the first transaction"

        await first.commit()
        assert await racing == Upsert(remediation_id=created.remediation_id, created=False)
        await second.rollback()


# --- Concurrency ---------------------------------------------------------------------------------


@pytest.mark.parametrize("state", list(State))
async def test_only_the_two_working_states_count_against_the_cap(
    session: AsyncSession, state: State
) -> None:
    """`docs/06-event-pipeline.md` counts `SESSION_CREATED` and `RUNNING`: the states where Devin
    is working. A remediation waiting on CI or on a reviewer holds a session open but spends
    nothing, and counting it would cap the pipeline on human latency."""
    session.add(a_remediation(issue_number=1000, state=str(state), labeled_at=NOW))
    await session.flush()

    expected = state in {State.SESSION_CREATED, State.RUNNING}
    assert await sessions_in_flight(session) == int(expected)


async def test_at_the_cap_the_job_is_deferred_and_not_failed(
    session: AsyncSession, settings: Settings
) -> None:
    """The named case. Nothing has failed, so nothing is recorded as a failure: the job goes back
    to the queue with a minute on it, and the remediation is untouched."""
    await seed_in_flight(session, MAX_CONCURRENT)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.DEFERRED
    assert decision.reason == CONCURRENCY_CAP_REACHED
    assert decision.detail == {
        "in_flight": MAX_CONCURRENT,
        "max_concurrent_sessions": MAX_CONCURRENT,
    }

    row = await read_job(session, job.id)
    assert row.status == JobStatus.DEFERRED
    assert row.last_error is None
    assert row.locked_by is None
    assert row.run_after > NOW + datetime.timedelta(seconds=30)
    assert remediation.state == State.QUEUED
    assert await read_events(session) == []


async def test_deferral_does_not_consume_the_retry_budget(
    session: AsyncSession, settings: Settings
) -> None:
    """A job held back by the cap must arrive at the Devin API with its whole retry budget intact —
    `attempts` is what the backoff schedule and `MAX_JOB_ATTEMPTS` are computed from, and a job
    that was never tried has not failed."""
    await seed_in_flight(session, MAX_CONCURRENT)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation, attempts=2)

    for _ in range(3):
        decision = await admit_session(
            session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
        )
        assert decision.verdict is Verdict.DEFERRED
        job = await requeue(session, settings, job)

    assert (await read_job(session, job.id)).attempts == 2


async def requeue(session: AsyncSession, settings: Settings, job: ClaimedJob) -> ClaimedJob:
    """Make a deferred job due and claim it again — the minute passing, without waiting for it."""
    await session.execute(
        text("UPDATE job SET run_after = now() - interval '1 minute' WHERE id = :id"),
        {"id": job.id},
    )
    claimed = await queue.claim(session, worker_id=WORKER, settings=settings)
    assert claimed is not None
    return claimed


async def test_one_below_the_cap_is_admitted_with_the_job_still_held(
    session: AsyncSession, settings: Settings
) -> None:
    """The boundary from the other side. The job stays claimed: an admitted decision hands it back
    to the handler, which completes or fails it once the session has been created."""
    await seed_in_flight(session, MAX_CONCURRENT - 1)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.ADMITTED
    assert decision.admitted
    row = await read_job(session, job.id)
    assert (row.status, row.locked_by, row.attempts) == (JobStatus.RUNNING, WORKER, 0)


async def test_remediations_past_the_working_states_do_not_hold_the_queue_shut(
    session: AsyncSession, settings: Settings
) -> None:
    await seed_in_flight(session, MAX_CONCURRENT, state=State.IN_REVIEW)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.ADMITTED


# --- ACU budget ----------------------------------------------------------------------------------


async def test_over_the_budget_the_remediation_is_blocked_and_escalated(
    session: AsyncSession, settings: Settings
) -> None:
    """The named case, end to end: the state column, the audit trail and the escalation job, all in
    the caller's transaction."""
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal("95.000")))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED
    assert decision.reason == BUDGET_EXHAUSTED

    assert remediation.state == State.BLOCKED
    assert remediation.blocked_reason == BUDGET_EXHAUSTED

    event = one(await read_events(session))
    assert (event.from_state, event.to_state, event.kind) == (State.QUEUED, State.BLOCKED, "policy")
    assert event.detail["reason"] == BUDGET_EXHAUSTED
    assert event.detail["acus_spent"] == 95.0
    assert event.detail["acu_budget"] == BUDGET

    escalation = one(await read_jobs(session, JobKind.ESCALATE))
    assert escalation.remediation_id == remediation.id
    assert escalation.status == JobStatus.PENDING
    assert escalation.payload["reason"] == BUDGET_EXHAUSTED

    # The work is cancelled, not retried: a budget that is exhausted now will still be exhausted in
    # ten seconds, and the escalation is what carries it from here.
    row = await read_job(session, job.id)
    assert (row.status, row.attempts, row.last_error) == (JobStatus.DONE, 0, None)


def one(rows: list[Row[Any]]) -> Row[Any]:
    assert len(rows) == 1, rows
    return rows[0]


async def test_spending_exactly_the_budget_is_inside_it(
    session: AsyncSession, settings: Settings
) -> None:
    """The comparison at the boundary. `BUDGET - CAP` spent leaves room for exactly this session,
    and refusing it would stop a demo that is inside its own ceiling."""
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal(BUDGET - CAP)))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.ADMITTED


async def test_a_thousandth_of_an_acu_over_the_budget_is_over_it(
    session: AsyncSession, settings: Settings
) -> None:
    """The other side of the same boundary, at the resolution `numeric(10,3)` stores."""
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal(BUDGET - CAP) + Decimal("0.001")))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED


async def test_the_budget_guard_is_not_permissive_when_devin_cannot_be_read(
    session: AsyncSession, settings: Settings
) -> None:
    """`daily_consumption()` degrading to `Unavailable` is a missing *source*, not a free pass.

    No enterprise scope, or an organisation with no consumption endpoint (B8): the ledger and
    `remediation.acus_consumed` are local and always there, and the guard refuses on those alone.
    """
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal("95.000")))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    devin = unavailable()
    decision = await admit_session(
        session, job=job, remediation=remediation, devin=devin, settings=settings
    )

    assert devin.calls == 1
    assert decision.verdict is Verdict.BLOCKED
    assert decision.detail["acus_devin"] is None
    assert decision.detail["acus_spent"] == 95.0
    assert remediation.state == State.BLOCKED


async def test_with_no_ledger_at_all_the_guard_falls_back_to_sentinels_own_reconciliation(
    session: AsyncSession, settings: Settings
) -> None:
    """The `sync_acu` job has never run — which is exactly what an unavailable consumption endpoint
    implies, since it is the same endpoint the ledger is synced from. `acus_consumed` is written by
    the poller from the session responses and owes nothing to either."""
    session.add(a_remediation(issue_number=1001, labeled_at=NOW, acus_consumed=Decimal("81.000")))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=unavailable(), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED
    assert decision.detail == {
        "day": TODAY.isoformat(),
        "acus_spent": 81.0,
        "acus_devin": None,
        "acus_ledger": 0.0,
        "acus_remediations": 81.0,
        "acu_budget": BUDGET,
    }


async def test_devins_figure_refuses_a_session_the_local_ones_would_allow(
    session: AsyncSession, settings: Settings
) -> None:
    """Devin's is organisation-wide and current: it sees sessions Sentinel never created and spend
    the poller has not reconciled yet."""
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(90.0), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED
    assert decision.detail["acus_devin"] == 90.0
    assert decision.detail["acus_ledger"] == 0.0


async def test_a_local_figure_refuses_a_session_devins_would_allow(
    session: AsyncSession, settings: Settings
) -> None:
    """The reverse, which is why the largest figure wins rather than a preferred source: Devin's
    window is undocumented (B8), so a figure that is small may be small because it covers an hour.
    """
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal("95.000")))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(1.0), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED
    assert decision.detail["acus_spent"] == 95.0


async def test_a_session_labelled_yesterday_and_still_running_counts_against_today(
    session: AsyncSession, settings: Settings
) -> None:
    """The spend that would otherwise be invisible in exactly the deployment B8 describes.

    With no consumption scope, Devin has no figure and the ledger is never synced from the endpoint
    that does not answer — so this sum is the only source there is, and a session that was labelled
    yesterday and is burning ACUs right now must not fall out of it. `labeled_at` alone would put it
    on yesterday, where nothing is looking.
    """
    session.add(
        a_remediation(
            issue_number=1001,
            state=str(State.RUNNING),
            devin_session_id="devin-yesterday",
            labeled_at=NOW - datetime.timedelta(days=1),
            acus_consumed=Decimal("95.000"),
        )
    )
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=unavailable(), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED
    assert decision.detail["acus_remediations"] == 95.0


async def test_yesterdays_spend_does_not_count_against_todays_budget(
    session: AsyncSession, settings: Settings
) -> None:
    """`DAILY_ACU_BUDGET` is daily. A ledger row and a *finished* remediation from yesterday are
    both past: what is still in flight can add to today, and what is not, cannot."""
    yesterday = TODAY - datetime.timedelta(days=1)
    session.add(an_acu_ledger_entry(day=yesterday, acus=Decimal("99.000")))
    session.add(
        a_remediation(
            issue_number=1001,
            state=str(State.MERGED),
            labeled_at=NOW - datetime.timedelta(days=1),
            acus_consumed=Decimal("99.000"),
        )
    )
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session,
        job=job,
        remediation=remediation,
        devin=reports(99.0, day=yesterday),
        settings=settings,
    )

    assert decision.verdict is Verdict.ADMITTED


async def test_the_class_cap_is_added_before_the_comparison(settings: Settings) -> None:
    """The guard refuses a session it could not afford to see run to its own ceiling, rather than
    starting one and discovering the budget mid-flight."""
    spend = DailySpend(day=TODAY, devin=None, ledger=Decimal(0), remediations=Decimal(BUDGET - CAP))

    assert budget_allows(spend, issue_class=ISSUE_CLASS, settings=settings)
    assert not budget_allows(
        spend,
        issue_class="flaky-test",
        settings=settings.model_copy(update={"daily_acu_budget": 89.0}),
    )


async def test_a_budget_that_is_not_a_binary_fraction_is_compared_as_written(
    session: AsyncSession, settings: Settings
) -> None:
    """`DAILY_ACU_BUDGET` is a float and the ledger is `numeric(10,3)`, so the two have to be
    brought together somewhere. Via `str`, so that a budget of `100.3` is a hundred and three
    tenths: read as its binary neighbour it is a shade *under*, and a day that spends exactly the
    budget would be refused for a rounding error nobody could see in the figures."""
    settings = settings.model_copy(update={"daily_acu_budget": 100.3})
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal("80.300")))
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.ADMITTED


async def test_an_unhandled_issue_class_reaches_the_caller(settings: Settings) -> None:
    """It has no `max_acu_limit` to reserve — and no `playbook_id` to create a session with either,
    so the caller is already the one that has to answer for it."""
    spend = DailySpend(day=TODAY, devin=None, ledger=Decimal(0), remediations=Decimal(0))

    with pytest.raises(UnknownIssueClass):
        budget_allows(spend, issue_class="documentation", settings=settings)


async def test_a_failing_consumption_endpoint_stops_the_job_rather_than_the_remediation(
    session: AsyncSession, settings: Settings, devin_api: FakeAPI
) -> None:
    """A `503` is a fault, not a capability gap.

    The degradation ADR turns `403` and `404` into `Unavailable` and raises everything else, and the
    guard does not soften that: the error reaches the handler, which fails the job and lets the
    queue's backoff have it. The remediation is left alone, because nothing has been decided about
    it.
    """
    devin_api.responds("GET", CONSUMPTION_PATH, 503, {"error": "unavailable"})
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    async with DevinClient(settings, sleep=nap) as client:
        with pytest.raises(DevinAPIError, match="503"):
            await admit_session(
                session, job=job, remediation=remediation, devin=client, settings=settings
            )

    assert remediation.state == State.QUEUED
    assert (await read_job(session, job.id)).status == JobStatus.RUNNING


async def nap(seconds: float) -> None:
    """The client's retry backoff, without the wait."""


async def test_the_guard_reads_the_real_consumption_endpoint(
    session: AsyncSession, settings: Settings, devin_api: FakeAPI
) -> None:
    """The same refusal through the client the worker actually passes, so that the guard is known
    to read the endpoint's own shape rather than a double's."""
    devin_api.responds(
        "GET",
        CONSUMPTION_PATH,
        200,
        a_consumption_body((TODAY, 92.5)),
    )
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    async with DevinClient(settings) as client:
        decision = await admit_session(
            session, job=job, remediation=remediation, devin=client, settings=settings
        )

    assert devin_api.only("GET", CONSUMPTION_PATH)
    assert decision.verdict is Verdict.BLOCKED
    assert decision.detail["acus_devin"] == 92.5


async def test_blocking_takes_any_policy_reason_and_escalates_it(
    session: AsyncSession,
) -> None:
    """`BLOCKED` has more than one cause — an unrecognised issue class, a session reporting
    `outcome: blocked` — and they all owe the same three writes. The escalation carries the reason
    so that the comment on the issue can state it."""
    remediation = await seed_remediation(session, state=str(State.RUNNING))

    assert await block(session, remediation=remediation, reason="unknown_issue_class")

    assert remediation.state == State.BLOCKED
    assert remediation.blocked_reason == "unknown_issue_class"
    assert one(await read_events(session)).from_state == State.RUNNING
    assert one(await read_jobs(session, JobKind.ESCALATE)).payload == {
        "reason": "unknown_issue_class"
    }


async def test_blocking_a_remediation_that_is_already_terminal_writes_nothing(
    session: AsyncSession,
) -> None:
    """Terminal states absorb every trigger. A second escalation for a remediation that has already
    been escalated would comment on the issue twice."""
    remediation = await seed_remediation(session, state=str(State.MERGED))

    assert not await block(session, remediation=remediation, reason=BUDGET_EXHAUSTED)

    assert remediation.state == State.MERGED
    assert remediation.blocked_reason is None
    assert await read_events(session) == []
    assert await read_jobs(session, JobKind.ESCALATE) == []


# --- Order and idempotency -----------------------------------------------------------------------


async def test_an_exhausted_budget_is_decided_before_a_saturated_queue(
    session: AsyncSession, settings: Settings
) -> None:
    """Both gates are shut. Deferring first would postpone a terminal verdict for as long as the
    queue stays busy, leaving the issue silently `QUEUED` with nothing for an operator to see."""
    session.add(an_acu_ledger_entry(day=TODAY, acus=Decimal("95.000")))
    await seed_in_flight(session, MAX_CONCURRENT)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    assert decision.verdict is Verdict.BLOCKED
    assert (await read_job(session, job.id)).status == JobStatus.DONE


async def test_a_remediation_that_already_has_a_session_does_not_get_a_second(
    session: AsyncSession, settings: Settings
) -> None:
    """The idempotency check
    `docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md` requires of every handler with
    an external effect: a lease can expire on a worker that is merely slow, and the reclaimer must
    not post a second `POST /v3/…/sessions` for one issue."""
    remediation = await seed_remediation(
        session, state=str(State.SESSION_CREATED), devin_session_id="devin-abc"
    )
    job = await claim_job(session, settings, remediation=remediation)
    devin = reports(0.0)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=devin, settings=settings
    )

    assert devin.calls == 0, "a decision already made must not cost a round trip"
    assert decision.verdict is Verdict.DUPLICATE
    assert decision.reason == SESSION_ALREADY_CREATED
    assert not decision.admitted
    assert (await read_job(session, job.id)).status == JobStatus.DONE
    assert remediation.state == State.SESSION_CREATED
    assert await read_jobs(session, JobKind.ESCALATE) == []


@pytest.mark.parametrize("state", [State.MERGED, State.BLOCKED, State.FAILED])
async def test_a_terminal_remediation_spends_nothing(
    session: AsyncSession, settings: Settings, state: State
) -> None:
    """The issue was closed or unlabelled while the job waited in the queue. Terminal states are
    absorbing, so there is nothing to escalate and nothing to create."""
    remediation = await seed_remediation(session, state=str(state))
    job = await claim_job(session, settings, remediation=remediation)
    devin = reports(0.0)

    decision = await admit_session(
        session, job=job, remediation=remediation, devin=devin, settings=settings
    )

    assert decision.verdict is Verdict.CANCELLED
    assert decision.reason == REMEDIATION_TERMINAL
    assert devin.calls == 0, "a cancelled remediation must not cost a round trip"
    assert (await read_job(session, job.id)).status == JobStatus.DONE
    assert remediation.state == state
    assert await read_events(session) == []


async def test_a_decision_by_a_worker_that_lost_its_lease_is_refused(
    session: AsyncSession, settings: Settings
) -> None:
    """The fence is the queue's, and the policy releases jobs through it: a worker whose lease
    expired must not defer or retire a job another worker is now running."""
    await seed_in_flight(session, MAX_CONCURRENT)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)
    await session.execute(
        text("UPDATE job SET locked_by = 'worker-2' WHERE id = :id"), {"id": job.id}
    )

    with pytest.raises(LeaseLost):
        await admit_session(
            session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
        )


async def test_the_decision_is_logged_with_the_figures_it_was_reached_on(
    session: AsyncSession, settings: Settings, capture: Configure
) -> None:
    """An operator asking why a session did not start reads one line, not a table join."""
    logs = capture()
    await seed_in_flight(session, MAX_CONCURRENT)
    remediation = await seed_remediation(session)
    job = await claim_job(session, settings, remediation=remediation)

    await admit_session(
        session, job=job, remediation=remediation, devin=reports(0.0), settings=settings
    )

    record: Mapping[str, Any] = logs.last
    assert record["event"] == "policy.decision"
    assert record["verdict"] == Verdict.DEFERRED
    assert record["reason"] == CONCURRENCY_CAP_REACHED
    assert record["remediation_id"] == remediation.id
    assert record["job_id"] == job.id
    assert record["in_flight"] == MAX_CONCURRENT
