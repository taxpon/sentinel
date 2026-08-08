"""The Devin session poller, checked against `docs/04-state-machine.md` and `docs/08-testing.md`.

Devin is faked with `respx` and the database is real, so every assertion here is about a row that
was actually written. Three properties carry most of the weight:

**All seven statuses are mapped.** The table is read against `SessionStatus` itself, so a status
added to the API without a decision here fails the suite rather than being silently ignored.

**Reconciliation is idempotent.** The poller runs every `POLL_INTERVAL_SECONDS` and almost every
tick sees exactly what the last one saw. Polling twice must therefore produce one state change and
one `remediation_event`, and a third poll that sees something genuinely new must produce exactly one
more — that pair of assertions is what separates a poller from a machine that walks a remediation
round in circles.

**The pull request link is write-once.** `pr_opened_at` is the numerator of time-to-PR and is
stamped here and nowhere else, so a second observation of `pull_requests[]` must not re-stamp it,
move the state backwards, or overwrite the linked pull request with a later one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import DEVIN_TOKEN, FakeAPI
from factories import ISSUE_CLASS, a_remediation
from sentinel.config import Settings
from sentinel.devin.client import SESSION, DevinClient
from sentinel.devin.playbooks import acu_cap_for
from sentinel.devin.schemas import SessionStatus
from sentinel.models import Job, Remediation, RemediationEvent
from sentinel.observability.prom import Metrics
from sentinel.pipeline import poller
from sentinel.pipeline.state import State, Trigger
from sentinel.queue import JobKind

SESSION_ID = "devin-7b3f9c2a"
SESSION_URL = f"https://app.devin.ai/sessions/{SESSION_ID}"
PR_URL = "https://github.com/taxpon/superset/pull/42889"
PR_NUMBER = 42889

REPORT: dict[str, Any] = {
    "outcome": "fixed",
    "root_cause": "The session cookie was signed with a key rotated out of the keyring.",
    "changes": ["superset/security/manager.py"],
    "tests": {"added": ["tests/security/test_manager.py"], "command": "pytest -q", "passed": True},
    "risk": "low",
}

# What each of the seven Devin statuses leaves a remediation in, starting from `SESSION_CREATED`,
# for a session carrying nothing else: no pull request, no report, no ACUs spent.
#
# `new` moves nothing — the session exists and nothing has claimed it yet. `exit` and `error` are
# Devin saying it is finished with the session, and one that finished with nothing to show leaves
# the remediation nowhere to go. `suspended` is not finished, so it does not.
STATE_FOR_STATUS: dict[SessionStatus, State] = {
    SessionStatus.NEW: State.SESSION_CREATED,
    SessionStatus.CLAIMED: State.RUNNING,
    SessionStatus.RUNNING: State.RUNNING,
    SessionStatus.RESUMING: State.RUNNING,
    SessionStatus.SUSPENDED: State.RUNNING,
    SessionStatus.EXIT: State.FAILED,
    SessionStatus.ERROR: State.FAILED,
}

ACU_CAP = acu_cap_for(ISSUE_CLASS)
"""`max_acu_limit` for the class `a_remediation` creates."""


def a_session(**overrides: Any) -> dict[str, Any]:
    """The body `GET /v3/organizations/{org}/sessions/{id}` answers with."""
    return {"session_id": SESSION_ID, "status": "running", "url": SESSION_URL, **overrides}


def a_pull_request(url: str = PR_URL, number: int = PR_NUMBER) -> list[dict[str, Any]]:
    return [{"url": url, "number": number}]


def responds(body: dict[str, Any]) -> httpx.Response:
    """One answer of a route that answers differently on each tick."""
    return httpx.Response(200, json=body)


class Ticks:
    """A `sleep` that records the interval and stops the loop after a fixed number of ticks."""

    def __init__(self, stop: asyncio.Event, after: int) -> None:
        self.delays: list[float] = []
        self._stop = stop
        self._after = after

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self._after:
            self._stop.set()


@pytest.fixture
def session_path(settings: Settings) -> str:
    """The templated route, filled in — so the test asserts the path the client actually builds."""
    return SESSION.format(org_id=settings.devin_org_id, session_id=SESSION_ID)


@pytest.fixture
async def devin(
    settings: Settings, devin_api: FakeAPI, metrics: Metrics
) -> AsyncIterator[DevinClient]:
    """The client the poller uses: faked HTTP, and metrics in this test's own registry."""
    async with DevinClient(settings, metrics=metrics) as client:
        yield client


Seed = Callable[..., Awaitable[Remediation]]


@pytest.fixture
def seed(session: AsyncSession) -> Seed:
    """A remediation with a session to poll, in `SESSION_CREATED` unless the test says otherwise."""

    async def build(**overrides: Any) -> Remediation:
        remediation = a_remediation(
            **{"state": State.SESSION_CREATED.value, "devin_session_id": SESSION_ID, **overrides}
        )
        session.add(remediation)
        await session.commit()
        return remediation

    return build


Poll = Callable[[], Awaitable[poller.Tick]]


@pytest.fixture
def poll(
    devin: DevinClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Poll:
    """One tick of the poller, against the test database."""

    async def tick() -> poller.Tick:
        return await poller.poll_once(devin, session_factory, settings=settings)

    return tick


async def events(session: AsyncSession) -> list[RemediationEvent]:
    """The append-only log, oldest first."""
    result = await session.execute(select(RemediationEvent).order_by(RemediationEvent.id))
    return list(result.scalars())


async def jobs(session: AsyncSession) -> list[Job]:
    result = await session.execute(select(Job).order_by(Job.id))
    return list(result.scalars())


async def row_version(session: AsyncSession, remediation_id: int) -> str:
    """Postgres's `xmin` for one remediation — the transaction that last wrote the row.

    The only way to ask "was this row written at all", which is what an idempotent reconciliation
    has to answer. A column-value assertion cannot: an `UPDATE` that sets every column to the value
    it already held looks identical from the outside and is exactly the thing to avoid.
    """
    result = await session.execute(
        text("SELECT xmin::text FROM remediation WHERE id = :id"), {"id": remediation_id}
    )
    return str(result.scalar_one())


# --- Status mapping -------------------------------------------------------------------------------


def test_every_devin_status_is_mapped() -> None:
    """`docs/08-testing.md` asks for all seven, and `docs/05-devin-integration.md` names them."""
    assert set(STATE_FOR_STATUS) == set(SessionStatus)


@pytest.mark.parametrize(("status", "expected"), list(STATE_FOR_STATUS.items()))
async def test_status_maps_to_state(
    status: SessionStatus,
    expected: State,
    seed: Seed,
    session: AsyncSession,
    devin_api: FakeAPI,
    session_path: str,
    poll: Poll,
) -> None:
    remediation = await seed()
    devin_api.responds("GET", session_path, 200, a_session(status=status.value))

    await poll()

    await session.refresh(remediation)
    assert remediation.state == expected.value
    # The last observed status is recorded whether or not it moved anything: `docs/09-operations.md`
    # tells an operator to read it off the row when a remediation stops advancing.
    assert remediation.devin_status == status.value
    assert remediation.devin_session_url == SESSION_URL

    sent = devin_api.only("GET", session_path)
    assert sent.headers["authorization"] == f"Bearer {DEVIN_TOKEN}"


async def test_a_new_session_writes_no_event(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`new` carries no trigger, so there is nothing to record."""
    await seed()
    devin_api.responds("GET", session_path, 200, a_session(status="new"))

    assert (await poll()).moved == 0
    assert await events(session) == []


async def test_an_errored_session_fails_and_escalates(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`docs/04-state-machine.md`: session status `error` forces `FAILED`, which escalates."""
    remediation = await seed()
    devin_api.responds("GET", session_path, 200, a_session(status="error"))

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.FAILED.value
    assert remediation.blocked_reason == poller.SESSION_ERROR
    assert remediation.closed_at is not None
    # Through `RUNNING`: the session was claimed at some point, or it could not have errored.
    assert [(event.from_state, event.to_state) for event in await events(session)] == [
        (State.SESSION_CREATED.value, State.RUNNING.value),
        (State.RUNNING.value, State.FAILED.value),
    ]
    # `remediation_id` is how the escalation handler knows which issue to comment on.
    assert [(job.kind, job.remediation_id, job.payload) for job in await jobs(session)] == [
        (
            JobKind.ESCALATE.value,
            remediation.id,
            {"reason": poller.SESSION_ERROR, "state": State.FAILED.value},
        )
    ]


async def test_a_blocked_report_outranks_an_errored_session(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`BLOCKED` is applied before `FAILED`, so a session that said *why* it stopped is recorded as
    blocked with that reason rather than as a bare error. The second trigger is then absorbed by the
    terminal state the first produced."""
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET",
        session_path,
        200,
        a_session(
            status="error",
            structured_output={**REPORT, "outcome": "blocked", "blocked_reason": "no upstream fix"},
        ),
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.BLOCKED.value
    assert remediation.blocked_reason == "no upstream fix"
    assert [event.to_state for event in await events(session)] == [State.BLOCKED.value]


# --- A session with nothing to show ---------------------------------------------------------------


async def test_a_finished_session_without_a_pull_request_fails(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """The failure this component exists to catch. Devin is finished, no pull request came of it,
    and no GitHub event will ever arrive — so without this the remediation sits in `RUNNING`, is
    re-read once per tick for ever, and counts as in flight on the funnel."""
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds("GET", session_path, 200, a_session(status="exit", structured_output=REPORT))

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.FAILED.value
    assert remediation.blocked_reason == poller.SESSION_ENDED_WITHOUT_PULL_REQUEST
    assert remediation.closed_at is not None
    assert [(job.kind, job.remediation_id) for job in await jobs(session)] == [
        (JobKind.ESCALATE.value, remediation.id)
    ]

    # And it stops costing a request per tick, because `FAILED` is not polled.
    assert (await poll()).polled == 0
    devin_api.only("GET", session_path)


async def test_a_session_that_spent_its_acu_cap_fails(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`docs/04-state-machine.md` tabulates "ACU cap hit" as a cause of `FAILED` and nothing else
    applies it: the budget guard in `docs/06-event-pipeline.md` is the *daily* one, checked before a
    session exists. Devin enforces `max_acu_limit` itself, so the status it stops at does not
    matter — `acus_consumed` against the class cap is the whole test."""
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET", session_path, 200, a_session(status="suspended", acus_consumed=float(ACU_CAP))
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.FAILED.value
    assert remediation.blocked_reason == poller.ACU_CAP_EXHAUSTED
    assert [(job.kind, job.payload["reason"]) for job in await jobs(session)] == [
        (JobKind.ESCALATE.value, poller.ACU_CAP_EXHAUSTED)
    ]


async def test_a_session_below_its_acu_cap_is_left_alone(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET", session_path, 200, a_session(status="running", acus_consumed=float(ACU_CAP) - 0.5)
    )

    assert (await poll()).moved == 0

    await session.refresh(remediation)
    assert remediation.state == State.RUNNING.value


async def test_a_remediation_holding_a_pull_request_survives_its_session(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """The pull request is the deliverable. A session that finished, or spent its whole cap, after
    opening one has done its job — check suites and a reviewer carry the remediation the rest of the
    way, and failing it here would discard finished work."""
    remediation = await seed(state=State.PR_OPENED.value, pr_url=PR_URL, pr_number=PR_NUMBER)
    devin_api.responds(
        "GET",
        session_path,
        200,
        a_session(status="exit", acus_consumed=float(ACU_CAP) + 5, structured_output=REPORT),
    )

    assert (await poll()).moved == 0

    await session.refresh(remediation)
    assert remediation.state == State.PR_OPENED.value
    assert remediation.blocked_reason is None


async def test_a_pull_request_the_session_stops_reporting_does_not_fail_the_remediation(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`pull_requests[]` is read from an API whose shape is unverified (B8). A linked remediation is
    judged on its own link, not on whether this observation happens to repeat it."""
    remediation = await seed(state=State.IN_REVIEW.value, pr_url=PR_URL, pr_number=PR_NUMBER)
    devin_api.responds("GET", session_path, 200, a_session(status="exit", pull_requests=[]))

    assert (await poll()).moved == 0

    await session.refresh(remediation)
    assert remediation.state == State.IN_REVIEW.value


# --- The pull request -----------------------------------------------------------------------------


async def test_pull_request_is_discovered_from_the_session(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """The poller is the only thing that links a pull request, and the only thing that stamps
    `pr_opened_at` — the numerator of time-to-PR in `docs/07-observability.md`."""
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET", session_path, 200, a_session(status="exit", pull_requests=a_pull_request())
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.PR_OPENED.value
    assert remediation.pr_url == PR_URL
    assert remediation.pr_number == PR_NUMBER
    assert remediation.pr_opened_at is not None
    assert [event.to_state for event in await events(session)] == [State.PR_OPENED.value]


async def test_a_transition_carries_the_cycle_forward(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """No trigger the poller applies increments `cycle`, and none may reset it: it counts the fix
    laps the worker paid for, and `docs/07-observability.md` reads the autonomy rate off it."""
    remediation = await seed(state=State.RUNNING.value, cycle=2)
    devin_api.responds(
        "GET", session_path, 200, a_session(status="exit", pull_requests=a_pull_request())
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.PR_OPENED.value
    assert remediation.cycle == 2


async def test_a_session_first_seen_at_exit_still_passes_through_running(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """Invariant 5 of `docs/04-state-machine.md`: `PR_OPENED` is on every path into the CI states,
    and `RUNNING` is the only state it is reachable from. A poller that missed the whole session —
    it was down, or the session was quick — must still link the pull request."""
    remediation = await seed()
    devin_api.responds(
        "GET", session_path, 200, a_session(status="exit", pull_requests=a_pull_request())
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.PR_OPENED.value
    assert remediation.pr_url == PR_URL
    assert [event.to_state for event in await events(session)] == [
        State.RUNNING.value,
        State.PR_OPENED.value,
    ]


async def test_the_pull_request_link_is_write_once(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`Trigger.PR_OPENED` carries `PullRequestCondition.UNLINKED`, so re-reading
    `pull_requests[]` on a later tick is absorbed rather than re-stamping anything.

    The second tick has to be *absorbed*, not merely harmless: a `PR_OPENED` applied without telling
    the machine about the link raises `IllegalTransitionError` from `PR_OPENED`, which leaves every
    column exactly as correct as absorption does. `Tick.failed` is what tells the two apart.
    """
    remediation = await seed(state=State.RUNNING.value)
    route = devin_api.route("GET", session_path)
    route.mock(
        side_effect=[
            responds(a_session(pull_requests=a_pull_request())),
            responds(
                a_session(pull_requests=a_pull_request(url=PR_URL + "0", number=PR_NUMBER + 1))
            ),
        ]
    )

    await poll()
    await session.refresh(remediation)
    linked_at = remediation.pr_opened_at

    assert (await poll()) == poller.Tick(polled=1, moved=0, unreachable=0, failed=0)

    await session.refresh(remediation)
    assert remediation.state == State.PR_OPENED.value
    assert remediation.pr_url == PR_URL
    assert remediation.pr_number == PR_NUMBER
    assert remediation.pr_opened_at == linked_at
    assert len(await events(session)) == 1


async def test_a_running_session_does_not_walk_the_fix_loop_backwards(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """A remediation in `CI_FAILED` has a pull request and a session that is running again. Neither
    `SESSION_RUNNING` — which belongs to the worker, with a cycle increment — nor a re-observed
    `PR_OPENED` may move it."""
    remediation = await seed(
        state=State.CI_FAILED.value, cycle=1, pr_url=PR_URL, pr_number=PR_NUMBER
    )
    devin_api.responds("GET", session_path, 200, a_session(pull_requests=a_pull_request()))

    assert (await poll()) == poller.Tick(polled=1, moved=0, unreachable=0, failed=0)

    await session.refresh(remediation)
    assert remediation.state == State.CI_FAILED.value
    assert remediation.cycle == 1
    assert await events(session) == []


# --- Idempotence ----------------------------------------------------------------------------------


async def test_reconciliation_is_idempotent(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """The case the whole design turns on: two ticks that see the same session produce **one** state
    change, and a third tick that sees a real change produces exactly one more."""
    remediation = await seed()
    route = devin_api.route("GET", session_path)
    route.mock(
        side_effect=[
            responds(a_session(acus_consumed=1.5)),
            responds(a_session(acus_consumed=1.5)),
            responds(a_session(acus_consumed=3.0, pull_requests=a_pull_request())),
        ]
    )

    assert (await poll()).moved == 1
    assert (await poll()).moved == 0

    await session.refresh(remediation)
    assert remediation.state == State.RUNNING.value
    assert [event.to_state for event in await events(session)] == [State.RUNNING.value]

    assert (await poll()).moved == 1

    await session.refresh(remediation)
    assert remediation.state == State.PR_OPENED.value
    assert remediation.acus_consumed == Decimal("3.0")
    assert [event.to_state for event in await events(session)] == [
        State.RUNNING.value,
        State.PR_OPENED.value,
    ]
    # Three ticks, three requests: the reconciliation is idempotent, not memoised.
    assert len(devin_api.sent("GET", session_path)) == 3


async def test_a_tick_that_sees_nothing_new_does_not_write_the_row(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """Idempotence at the storage layer, which no assertion on a column value can reach: a column
    whose value is unchanged must not be re-assigned, or every remediation would be rewritten once
    per `POLL_INTERVAL_SECONDS` for as long as it is in flight.

    `0.1` is the value that catches this. It has no exact binary form, so an `acus_consumed` built
    from the float rather than from its text differs from the `numeric(10,3)` read back — the two
    round to the same stored value, and the row is rewritten every tick for ever.
    """
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds("GET", session_path, 200, a_session(acus_consumed=0.1))

    await poll()
    written = await row_version(session, remediation.id)

    await poll()

    assert await row_version(session, remediation.id) == written
    await session.refresh(remediation)
    assert remediation.acus_consumed == Decimal("0.100")


async def test_a_terminal_remediation_is_not_polled_at_all(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """Nothing a session says can move a terminal state, so asking costs a request per tick for
    ever and answers nothing. `QUEUED` is excluded by its state alone — a session id written to a
    row before its state moves must not make it pollable."""
    await seed(state=State.MERGED.value, issue_number=1)
    await seed(state=State.QUEUED.value, issue_number=2)
    remediation = await seed(issue_number=3)
    devin_api.responds("GET", session_path, 200, a_session())

    assert (await poll()).polled == 1

    devin_api.only("GET", session_path)
    await session.refresh(remediation)
    assert remediation.state == State.RUNNING.value


async def test_a_remediation_with_no_session_is_not_polled(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """There is nothing to ask about. The worker writes the session id and the state together, so
    this is a row that should not exist — but a request to `/sessions/None` is a poor way to find
    that out, and `in_flight` decides it in one place."""
    await seed(issue_number=1, devin_session_id=None)
    remediation = await seed(issue_number=2)
    devin_api.responds("GET", session_path, 200, a_session())

    assert (await poll()) == poller.Tick(polled=1, moved=1, unreachable=0)

    devin_api.only("GET", session_path)
    await session.refresh(remediation)
    assert remediation.state == State.RUNNING.value


# --- ACUs and the structured report ---------------------------------------------------------------


async def test_acus_and_structured_output_are_reconciled(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET",
        session_path,
        200,
        a_session(
            status="exit",
            acus_consumed=0.1,
            structured_output=REPORT,
            pull_requests=a_pull_request(),
        ),
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.acus_consumed == Decimal("0.100")
    assert remediation.structured_output == {
        **REPORT,
        "blocked_reason": None,
        "pr_url": None,
        "confidence": None,
    }


async def test_a_session_that_has_consumed_nothing_reports_zero(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`acus_consumed` is `NOT NULL DEFAULT 0`, and a session Devin has not claimed reports no
    consumption at all."""
    remediation = await seed()
    devin_api.responds("GET", session_path, 200, a_session(status="new", acus_consumed=None))

    await poll()

    await session.refresh(remediation)
    assert remediation.acus_consumed == Decimal("0")


async def test_a_blocked_report_blocks_and_escalates(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`structured_output.outcome == "blocked"` forces `BLOCKED` from any state, with the reason
    stored for the failure-breakdown panel."""
    remediation = await seed(state=State.RUNNING.value)
    reason = "the fix requires an upstream decision on the cookie format"
    devin_api.responds(
        "GET",
        session_path,
        200,
        a_session(
            status="exit",
            structured_output={**REPORT, "outcome": "blocked", "blocked_reason": reason},
        ),
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.BLOCKED.value
    assert remediation.blocked_reason == reason
    assert remediation.closed_at is not None
    assert [(job.kind, job.payload["reason"]) for job in await jobs(session)] == [
        (JobKind.ESCALATE.value, reason)
    ]


async def test_a_blocked_report_without_a_reason_still_names_one(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`blocked_reason` is what the failure breakdown groups by, so a null would drop the row out
    of the panel that exists to show it."""
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET", session_path, 200, a_session(structured_output={**REPORT, "outcome": "blocked"})
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.BLOCKED.value
    assert remediation.blocked_reason == poller.DEVIN_REPORTED_BLOCKED


# --- The stall signal -----------------------------------------------------------------------------


async def test_a_session_waiting_for_user_is_blocked(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """`status_detail: waiting_for_user` on a working session is the stall signal of
    `docs/05-devin-integration.md`. Nothing in an unattended pipeline will answer the question, so
    it is escalated rather than left looking busy."""
    remediation = await seed(state=State.RUNNING.value)
    devin_api.responds(
        "GET", session_path, 200, a_session(status="running", status_detail="waiting_for_user")
    )

    await poll()

    await session.refresh(remediation)
    assert remediation.state == State.BLOCKED.value
    assert remediation.blocked_reason == poller.SESSION_WAITING_FOR_USER
    assert [(job.kind, job.payload["reason"]) for job in await jobs(session)] == [
        (JobKind.ESCALATE.value, poller.SESSION_WAITING_FOR_USER)
    ]


async def test_a_finished_session_waiting_for_input_is_not_a_stall(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """A session that has finished its lap and is waiting to be resumed is the normal state of
    every remediation sitting in review. Only a session Devin is *running* is stalled by the
    detail."""
    remediation = await seed(state=State.IN_REVIEW.value, pr_url=PR_URL)
    devin_api.responds(
        "GET", session_path, 200, a_session(status="exit", status_detail="waiting_for_user")
    )

    assert (await poll()).moved == 0

    await session.refresh(remediation)
    assert remediation.state == State.IN_REVIEW.value
    assert remediation.blocked_reason is None


@pytest.mark.parametrize("status", [SessionStatus.RESUMING, SessionStatus.CLAIMED])
async def test_a_session_being_started_is_not_stalled(
    status: SessionStatus,
    seed: Seed,
    session: AsyncSession,
    devin_api: FakeAPI,
    session_path: str,
    poll: Poll,
) -> None:
    """The false positive that would cost the most. `resuming` is by definition a session the worker
    has just sent a message to, so a `waiting_for_user` observed on it may be left over from before
    that message landed — and `status_detail` timing is unverified (B8). Blocking here would end a
    remediation in the middle of its fix loop, and `BLOCKED` is terminal.

    `claimed` is the same argument from the other end: work has not begun, so nothing has been
    asked yet.
    """
    remediation = await seed(state=State.CI_FAILED.value, cycle=1, pr_url=PR_URL)
    devin_api.responds(
        "GET", session_path, 200, a_session(status=status.value, status_detail="waiting_for_user")
    )

    assert (await poll()).moved == 0

    await session.refresh(remediation)
    assert remediation.state == State.CI_FAILED.value
    assert remediation.blocked_reason is None


# --- What Devin will not answer -------------------------------------------------------------------


async def test_a_session_devin_will_not_answer_for_is_left_alone(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """The next tick asks again, and nothing moves on the strength of a failed request."""
    remediation = await seed()
    devin_api.responds("GET", session_path, 404, {"detail": "no such session"})

    assert (await poll()) == poller.Tick(polled=1, moved=0, unreachable=1)

    await session.refresh(remediation)
    assert remediation.state == State.SESSION_CREATED.value
    assert await events(session) == []


async def test_an_unreadable_session_is_escalated(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """A body the v3 schemas reject will be rejected identically next tick, so waiting cannot
    resolve it. It is escalated with the reason recorded, not polled for ever."""
    remediation = await seed()
    devin_api.responds("GET", session_path, 200, a_session(status="daydreaming"))

    assert (await poll()).unreachable == 1

    await session.refresh(remediation)
    assert remediation.state == State.FAILED.value
    assert remediation.blocked_reason == poller.SESSION_UNREADABLE
    assert [(job.kind, job.payload["reason"]) for job in await jobs(session)] == [
        (JobKind.ESCALATE.value, poller.SESSION_UNREADABLE)
    ]


async def test_one_unreachable_session_does_not_hold_up_the_tick(
    seed: Seed,
    session: AsyncSession,
    settings: Settings,
    devin_api: FakeAPI,
    session_path: str,
    poll: Poll,
) -> None:
    """Each remediation is reconciled in a transaction of its own, so the one Devin refuses does not
    roll back the ones it answered for."""
    other_id = "devin-0000dead"
    other_path = SESSION.format(org_id=settings.devin_org_id, session_id=other_id)
    await seed(issue_number=1, devin_session_id=other_id)
    remediation = await seed(issue_number=2)
    devin_api.responds("GET", other_path, 404, {"detail": "no such session"})
    devin_api.responds("GET", session_path, 200, a_session())

    assert (await poll()) == poller.Tick(polled=2, moved=1, unreachable=1)

    await session.refresh(remediation)
    assert remediation.state == State.RUNNING.value


async def test_one_remediation_that_raises_does_not_end_the_tick(
    seed: Seed,
    session: AsyncSession,
    settings: Settings,
    devin_api: FakeAPI,
    session_path: str,
    poll: Poll,
) -> None:
    """`in_flight` is ordered by id, so anything escaping the reconciliation of one row would
    strand every remediation after it — and `restart: unless-stopped` would re-poison the process on
    the same row after every restart.

    A session reported as `new` while carrying a pull request is one way to get there: it moves
    nothing, so `PR_OPENED` is applied from `SESSION_CREATED`, which the state machine refuses.
    """
    poisoned_id = "devin-0000dead"
    poisoned_path = SESSION.format(org_id=settings.devin_org_id, session_id=poisoned_id)
    poisoned = await seed(issue_number=1, devin_session_id=poisoned_id)
    remediation = await seed(issue_number=2)
    devin_api.responds(
        "GET", poisoned_path, 200, a_session(status="new", pull_requests=a_pull_request())
    )
    devin_api.responds("GET", session_path, 200, a_session())

    assert (await poll()) == poller.Tick(polled=2, moved=1, unreachable=0, failed=1)

    # The poisoned row keeps the state it had, and the one behind it was still reconciled.
    await session.refresh(poisoned)
    await session.refresh(remediation)
    assert poisoned.state == State.SESSION_CREATED.value
    assert remediation.state == State.RUNNING.value


# --- The audit trail ------------------------------------------------------------------------------


async def test_a_poller_transition_names_itself_and_no_delivery(
    seed: Seed, session: AsyncSession, devin_api: FakeAPI, session_path: str, poll: Poll
) -> None:
    """Nothing GitHub sent caused this, which is the whole reason the poller exists. The worker's
    transitions carry no delivery either, so the event says which process observed it."""
    await seed()
    devin_api.responds("GET", session_path, 200, a_session(status="claimed"))

    await poll()

    [event] = await events(session)
    assert event.webhook_delivery_id is None
    assert event.kind == "transition"
    assert event.detail == {
        "source": poller.SOURCE,
        "trigger": Trigger.SESSION_RUNNING.value,
        "devin_session_id": SESSION_ID,
        "devin_status": SessionStatus.CLAIMED.value,
    }


# --- The loop -------------------------------------------------------------------------------------


async def test_run_polls_every_interval_until_stopped(
    seed: Seed,
    session: AsyncSession,
    settings: Settings,
    devin: DevinClient,
    session_factory: async_sessionmaker[AsyncSession],
    devin_api: FakeAPI,
    session_path: str,
) -> None:
    remediation = await seed()
    devin_api.responds("GET", session_path, 200, a_session())
    stop = asyncio.Event()
    ticks = Ticks(stop, after=3)

    await poller.run(devin, session_factory, settings=settings, stop=stop, sleep=ticks)

    assert ticks.delays == [settings.poll_interval_seconds] * 3
    assert len(devin_api.sent("GET", session_path)) == 3
    # Three ticks over an unchanging session: still one transition.
    await session.refresh(remediation)
    assert remediation.state == State.RUNNING.value
    assert len(await events(session)) == 1
