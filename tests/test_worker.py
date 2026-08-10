"""The worker and its four handlers — the part of Sentinel that acts.

The database is real and both APIs are faked, so every assertion is about a row that was written or
a request body that was sent. Four properties carry most of the weight.

**A handler interrupted after its outbound call must not repeat it.** The lease fences the row and
not the work (`docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md`), so the test that
matters most steals the lease *after* Devin has been called and then lets a second worker claim the
same job — and asserts that exactly one session was created and exactly one resume message sent.
`sentinel.policy` narrows that window and its own ADR says it does not close it, so this is tested
here rather than assumed there.

**The captured request body is the assertion.** `.claude/rules/testing.md` requires it: the tags,
the ACU ceiling and the resume text are what a reviewer verifies independently in the Devin
dashboard, so a test that only checked a call happened would cover none of it.

**The review-fix loop resumes the same session.** Both edges are driven end to end — the cycle
number, the log excerpt, the review body *and* its inline comments, and the `cycle:N` tag.

**Escalation reads the reason before it acts, and reads it from the column.** A remediation that
reached `FAILED` because a maintainer removed the label or closed the issue is not commented on and
not labelled (`docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md`); one whose pull request
was closed unmerged still is, because the issue is still open
(`docs/adr/2026-08-09-an-abandoned-pull-request-still-escalates.md`). Which of the two happens is
decided from `remediation.blocked_reason`, so the tests below disagree the column with the payload
on purpose.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import FakeAPI
from factories import (
    ISSUE_CLASS,
    ISSUE_NUMBER,
    PR_NUMBER,
    REPO,
    a_consumption_body,
    a_remediation,
    github_payload,
)
from sentinel import queue
from sentinel.config import Settings
from sentinel.devin.client import DevinClient, RetryPolicy
from sentinel.devin.playbooks import (
    NO_ISSUE_BODY,
    NO_LOG_OUTPUT,
    NO_REVIEW_FEEDBACK,
    STRUCTURED_OUTPUT_SCHEMA,
    acu_cap_for,
)
from sentinel.github.client import GitHubClient
from sentinel.models import AcuLedger, Job, Remediation, RemediationEvent
from sentinel.observability.prom import Metrics
from sentinel.pipeline import handlers, worker
from sentinel.pipeline.handlers import (
    CANCELLED_BY_A_HUMAN,
    JOB_FAILED,
    NEEDS_HUMAN_LABEL,
    NO_FAILING_JOB,
    UNKNOWN_ISSUE_CLASS,
    Context,
    escalation_comment,
)
from sentinel.pipeline.state import CYCLE_LIMIT_EXHAUSTED, State, Trigger
from sentinel.policy import BUDGET_EXHAUSTED
from sentinel.queue import ClaimedJob, JobKind, JobStatus

DELIVERY = "0b1c8e40-7d3a-11f1-9c2e-4f6a1d8b3e57"
SESSION_ID = "devin-7b3f9c2a"
SESSION_URL = f"https://app.devin.ai/sessions/{SESSION_ID}"
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
HEAD_SHA = "349a7f639dfb353669c001187706d7fd0112ed2f"
REVIEW_ID = 4882944318
RUN_ID = 84410727864
CI_JOB_ID = 231855142299

ORG = "org-abc123"
SESSIONS = f"/v3/organizations/{ORG}/sessions"
MESSAGES = f"{SESSIONS}/{SESSION_ID}/messages"
TAGS = f"{SESSIONS}/{SESSION_ID}/tags"
CONSUMPTION = f"/v3/organizations/{ORG}/consumption/daily"

ISSUES = f"/repos/{REPO}/issues"
ISSUE = f"{ISSUES}/{ISSUE_NUMBER}"
PULLS = f"/repos/{REPO}/pulls"
ACTIONS = f"/repos/{REPO}/actions"
REVIEW_COMMENTS = f"{PULLS}/{PR_NUMBER}/reviews/{REVIEW_ID}/comments"

WORKER = "worker-a"
OTHER_WORKER = "worker-b"

ABANDONED = "pull_request_closed_unmerged"
"""`sentinel.github.events.Reason` for `pull_request.closed` with `merged: false` — the third
human-caused `FAILED`, and the one that is *not* suppressed."""


# ------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def waits() -> list[float]:
    """Every backoff either client took, instead of real time passing."""
    return []


@pytest.fixture
async def devin(
    settings: Settings, devin_api: FakeAPI, metrics: Metrics, waits: list[float]
) -> AsyncIterator[DevinClient]:
    async def sleep(delay: float) -> None:
        waits.append(delay)

    async with DevinClient(
        settings, metrics=metrics, sleep=sleep, retry=RetryPolicy(attempts=2)
    ) as client:
        yield client


@pytest.fixture
async def github(
    settings: Settings, github_api: FakeAPI, waits: list[float]
) -> AsyncIterator[GitHubClient]:
    async def sleep(delay: float) -> None:
        waits.append(delay)

    async with GitHubClient(settings, sleep=sleep) as client:
        yield client


@pytest.fixture
def context(
    session_factory: async_sessionmaker[AsyncSession],
    devin: DevinClient,
    github: GitHubClient,
    settings: Settings,
) -> Context:
    return Context(session_factory=session_factory, devin=devin, github=github, settings=settings)


Seed = Callable[..., Awaitable[int]]


@pytest.fixture
def seed(session_factory: async_sessionmaker[AsyncSession]) -> Seed:
    """A remediation, returning its id. `QUEUED` with no session unless the test says otherwise."""

    async def build(**overrides: Any) -> int:
        async with session_factory() as db:
            remediation = a_remediation(**overrides)
            db.add(remediation)
            await db.commit()
            return remediation.id

    return build


Enqueue = Callable[..., Awaitable[int]]


@pytest.fixture
def enqueue(session_factory: async_sessionmaker[AsyncSession]) -> Enqueue:
    """One job on the queue, returning its id."""

    async def build(kind: JobKind, payload: dict[str, Any], remediation_id: int | None) -> int:
        async with session_factory() as db:
            job_id = await queue.enqueue(
                db, kind=kind, payload=payload, remediation_id=remediation_id
            )
            await db.commit()
            return job_id

    return build


Claim = Callable[..., Awaitable[ClaimedJob]]


@pytest.fixture
def claim(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> Claim:
    """Take the next due job, as a worker does — claim, commit, then run."""

    async def take(worker_id: str = WORKER) -> ClaimedJob:
        async with session_factory() as db:
            claimed = await queue.claim(db, worker_id=worker_id, settings=settings)
            await db.commit()
        assert claimed is not None, "expected a due job"
        return claimed

    return take


# --------------------------------------------------------------------------------- reading rows


async def remediation_row(
    factory: async_sessionmaker[AsyncSession], remediation_id: int
) -> Remediation:
    async with factory() as db:
        result = await db.execute(select(Remediation).where(Remediation.id == remediation_id))
        return result.scalar_one()


async def job_rows(factory: async_sessionmaker[AsyncSession]) -> list[Job]:
    async with factory() as db:
        return list((await db.execute(select(Job).order_by(Job.id))).scalars())


async def event_rows(factory: async_sessionmaker[AsyncSession]) -> list[RemediationEvent]:
    async with factory() as db:
        return list(
            (await db.execute(select(RemediationEvent).order_by(RemediationEvent.id))).scalars()
        )


async def ledger_rows(factory: async_sessionmaker[AsyncSession]) -> list[AcuLedger]:
    async with factory() as db:
        return list((await db.execute(select(AcuLedger).order_by(AcuLedger.day))).scalars())


async def steal_lease(factory: async_sessionmaker[AsyncSession], job_id: int) -> None:
    """Hand a claimed job to another worker, as an expired lease being reclaimed does.

    This is the whole of the hazard: the first worker is still running, has already called Devin,
    and will find out only when it tries to release the row.
    """
    async with factory() as db:
        await db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(locked_by=OTHER_WORKER, locked_at=text("now()"))
        )
        await db.commit()


# ----------------------------------------------------------------------------------- API fakes


def a_session_body(**overrides: Any) -> dict[str, Any]:
    return {"session_id": SESSION_ID, "status": "new", "url": SESSION_URL, **overrides}


WORKFLOW_PATH = ".github/workflows/devin-autofix-ci.yml"


def a_workflow_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": RUN_ID,
        # `get_failing_job` selects on the path, so a run without one is a run from some other
        # workflow — which is exactly what must not supply the excerpt.
        "path": WORKFLOW_PATH,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "failure",
        "run_started_at": "2026-08-07T15:11:55Z",
        **overrides,
    }


def a_ci_job(**overrides: Any) -> dict[str, Any]:
    return {
        "id": CI_JOB_ID,
        "run_id": RUN_ID,
        "name": "pytest",
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2026-08-07T15:12:04Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/{CI_JOB_ID}",
        **overrides,
    }


def a_pull_request(**overrides: Any) -> dict[str, Any]:
    return {**github_payload("pull_request.opened")["pull_request"], **overrides}


def a_review_comment(**overrides: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "path": "superset/connectors/sqla/models.py",
        "line": 1462,
        "body": "This still swallows the DB error.",
        "user": {"login": "mm"},
        "html_url": f"https://github.com/{REPO}/pull/{PR_NUMBER}#discussion_r1",
        **overrides,
    }


def fake_budget(devin_api: FakeAPI, *days: tuple[datetime.date, float]) -> None:
    """The consumption endpoint the admission policy reads before it admits anything."""
    devin_api.responds("GET", CONSUMPTION, json=a_consumption_body(*days))


def a_session_page(*sessions: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": list(sessions),
        "end_cursor": None,
        "has_next_page": False,
        "total": len(sessions),
    }


def fake_no_session_yet(devin_api: FakeAPI) -> None:
    """The adopt-or-create lookup `create_session` makes before it posts, answering "none yet"."""
    devin_api.responds("GET", SESSIONS, json=a_session_page())


def fake_session_creation(devin_api: FakeAPI, **overrides: Any) -> None:
    fake_budget(devin_api)
    fake_no_session_yet(devin_api)
    devin_api.responds("POST", SESSIONS, 201, a_session_body(**overrides))


def fake_issue(github_api: FakeAPI, **overrides: Any) -> None:
    github_api.responds(
        "GET", ISSUE, json={**github_payload("issues.labeled")["issue"], **overrides}
    )


def fake_escalation_targets(github_api: FakeAPI) -> None:
    github_api.responds("POST", f"{ISSUE}/labels", json=[{"name": NEEDS_HUMAN_LABEL}])
    github_api.responds(
        "POST",
        f"{ISSUE}/comments",
        201,
        json={
            "id": 9,
            "html_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#c9",
            "body": "",
        },
    )


def fake_ci(
    github_api: FakeAPI,
    *,
    runs: list[dict[str, Any]] | None = None,
    jobs: list[dict[str, Any]] | None = None,
    log: str = "AssertionError: boom",
) -> None:
    runs = [a_workflow_run()] if runs is None else runs
    jobs = [a_ci_job()] if jobs is None else jobs
    github_api.responds(
        "GET", f"{ACTIONS}/runs", json={"total_count": len(runs), "workflow_runs": runs}
    )
    for run in runs:
        github_api.responds(
            "GET", f"{ACTIONS}/runs/{run['id']}/jobs", json={"total_count": len(jobs), "jobs": jobs}
        )
    for ci_job in jobs:
        github_api.responds("GET", f"{ACTIONS}/jobs/{ci_job['id']}/logs", text=log)


def fake_resume(devin_api: FakeAPI) -> None:
    devin_api.responds("POST", MESSAGES, 202)
    devin_api.responds("POST", TAGS, 202)


# ------------------------------------------------------------------------------- create_session


async def a_create_job(seed: Seed, enqueue: Enqueue, **overrides: Any) -> int:
    remediation_id = await seed(**overrides)
    await enqueue(JobKind.CREATE_SESSION, {"delivery_id": DELIVERY}, remediation_id)
    return remediation_id


async def test_create_session_posts_the_session_and_enters_session_created(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_session_creation(devin_api)

    await handlers.create_session(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.SESSION_CREATED
    assert remediation.devin_session_id == SESSION_ID
    assert remediation.devin_session_url == SESSION_URL
    assert remediation.session_created_at is not None

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE
    assert job.locked_by is None

    [event] = await event_rows(session_factory)
    assert (event.from_state, event.to_state) == (State.QUEUED, State.SESSION_CREATED)
    assert event.kind == "transition"
    assert event.detail == {
        "source": "worker",
        "trigger": "session_created",
        "devin_session_id": SESSION_ID,
    }


async def test_create_session_sends_the_body_a_reviewer_verifies(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    """The tags, the ACU ceiling, `resumable` and the schema are the contract, not an internal."""
    await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_session_creation(devin_api)

    await handlers.create_session(context, await claim())

    sent = devin_api.only("POST", SESSIONS).json
    assert sent["tags"] == [
        "sentinel",
        f"repo:{REPO}",
        f"issue:{ISSUE_NUMBER}",
        f"class:{ISSUE_CLASS}",
        f"run:{DELIVERY}",
    ]
    assert sent["title"] == (
        f"[sentinel] #{ISSUE_NUMBER} Helm chart: database credentials "
        "cannot be loaded from existing secret"
    )
    assert sent["repos"] == [REPO]
    assert sent["playbook_id"] == "playbook-sec"
    assert sent["max_acu_limit"] == acu_cap_for(ISSUE_CLASS)
    assert sent["resumable"] is True
    assert sent["structured_output_required"] is True
    assert sent["structured_output_schema"] == STRUCTURED_OUTPUT_SCHEMA
    assert f"GitHub issue #{ISSUE_NUMBER} in {REPO}" in sent["prompt"]
    assert "### Bug description" in sent["prompt"]


async def test_create_session_reads_the_issue_from_github_not_the_payload(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    """An issue filed with a title alone still produces a prompt, not a blank gap."""
    await a_create_job(seed, enqueue)
    fake_issue(github_api, body=None)
    fake_session_creation(devin_api)

    await handlers.create_session(context, await claim())

    assert github_api.only("GET", ISSUE).path == ISSUE
    assert NO_ISSUE_BODY in devin_api.only("POST", SESSIONS).json["prompt"]


async def test_a_reclaimed_create_session_does_not_create_a_second_session(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case `docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md` names.

    Worker A is slow rather than dead. Its lease expires while the `POST` is in flight, worker B
    reclaims the job, and A only finds out when it tries to complete. Because A commits the session
    id in a transaction of its own — before the completion that raises `LeaseLost` — B's admission
    check reads a committed id and refuses. One session, one ACU budget, no orphan.
    """
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_session_creation(devin_api)

    slow = await claim()
    await steal_lease(session_factory, slow.id)

    # Worker A, through the worker's own dispatch, so `LeaseLost` is handled as it is in production.
    await worker.run_job(context, slow)

    assert (await remediation_row(session_factory, remediation_id)).devin_session_id == SESSION_ID
    [job] = await job_rows(session_factory)
    assert job.locked_by == OTHER_WORKER, "A must not have retired a job it no longer holds"

    # Worker B now runs the same job.
    reclaimed = replace(slow, locked_by=OTHER_WORKER)
    await worker.run_job(context, reclaimed)

    devin_api.only("POST", SESSIONS)
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE
    assert len(await event_rows(session_factory)) == 1


async def test_a_create_job_retried_after_a_timeout_adopts_rather_than_creating_again(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The outer layer — the one a narrower fix would have missed entirely.

    On 2026-08-11 the client's own retries made three sessions per issue. Stopping *those* would
    have left this: the job fails, `queue.fail` schedules it again with backoff, and the second
    attempt posts a second time — the same duplicate, arriving a minute later instead of a second
    later.

    So the job really is retried here, through `run_once` both times, and the assertion is on the
    count of `POST`s across the pair. The session Devin made from the first (timed-out) request is
    what the listing answers with on the second attempt.
    """
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_budget(devin_api)
    # The request arrived and the session was made; only the response was lost.
    devin_api.route("POST", SESSIONS).mock(side_effect=httpx.ReadTimeout("no response"))
    devin_api.route("GET", SESSIONS).mock(
        side_effect=[
            httpx.Response(200, json=a_session_page()),
            httpx.Response(
                200,
                json=a_session_page(
                    {
                        "session_id": SESSION_ID,
                        "status": "running",
                        "url": SESSION_URL,
                        "tags": [
                            "sentinel",
                            f"repo:{REPO}",
                            f"issue:{ISSUE_NUMBER}",
                            f"class:{ISSUE_CLASS}",
                            f"run:{DELIVERY}",
                        ],
                    }
                ),
            ),
        ]
    )

    await worker.run_once(context, claimed_by=WORKER)

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.PENDING, "a timeout is worth another attempt"
    assert (await remediation_row(session_factory, remediation_id)).state == State.QUEUED

    # The queue would wait out the backoff; the test does not.
    async with session_factory() as db:
        await db.execute(update(Job).where(Job.id == job.id).values(run_after=text("now()")))
        await db.commit()

    await worker.run_once(context, claimed_by=WORKER)

    assert len(devin_api.sent("POST", SESSIONS)) == 1, (
        "the retried job must adopt the session the timed-out request created, not make another"
    )
    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.SESSION_CREATED
    assert remediation.devin_session_id == SESSION_ID
    assert remediation.devin_session_url == SESSION_URL
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_a_session_created_for_a_remediation_cancelled_mid_flight_is_still_recorded(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The orphan branch, and the promise the docstring makes of it.

    The remediation is cancelled *after* admission and *while* the `POST` is in flight, so the
    session exists and its remediation is terminal. `devin_session_id` is written anyway: it is the
    only record that the session exists at all, and the poller — which reads the column — is the
    only thing that could ever observe it.
    """
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_budget(devin_api)
    fake_no_session_yet(devin_api)

    async def cancel_it(request: httpx.Request) -> httpx.Response:
        async with session_factory() as db:
            await db.execute(
                update(Remediation)
                .where(Remediation.id == remediation_id)
                .values(state=State.FAILED.value, blocked_reason="issue_closed")
            )
            await db.commit()
        return httpx.Response(201, json=a_session_body())

    devin_api.route("POST", SESSIONS).mock(side_effect=cancel_it)

    await worker.run_job(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.devin_session_id == SESSION_ID, "an orphan session must still be recorded"
    assert remediation.devin_session_url == SESSION_URL
    assert remediation.state == State.FAILED, "the cancellation is not walked back"
    assert await event_rows(session_factory) == [], "no transition happened"
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_create_session_defers_at_the_concurrency_cap_without_calling_devin(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for number in range(settings.max_concurrent_sessions):
        await seed(issue_number=1000 + number, state=State.RUNNING, devin_session_id=f"s-{number}")
    await a_create_job(seed, enqueue)
    devin_api.responds("GET", CONSUMPTION, json=a_consumption_body())

    await handlers.create_session(context, await claim())

    assert devin_api.sent("POST", SESSIONS) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DEFERRED
    assert job.attempts == 0, "deferral must not spend the retry budget"


async def test_create_session_blocks_and_escalates_when_the_budget_is_exhausted(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remediation_id = await a_create_job(seed, enqueue)
    today = datetime.datetime.now(datetime.UTC).date()
    devin_api.responds("GET", CONSUMPTION, json=a_consumption_body((today, 999.0)))

    await handlers.create_session(context, await claim())

    assert devin_api.sent("POST", SESSIONS) == []
    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.BLOCKED
    assert remediation.blocked_reason == BUDGET_EXHAUSTED

    created, escalation = await job_rows(session_factory)
    assert created.status == JobStatus.DONE
    assert escalation.kind == JobKind.ESCALATE
    assert escalation.payload["reason"] == BUDGET_EXHAUSTED


async def test_create_session_blocks_an_unrecognised_issue_class(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `QUEUED -> BLOCKED` row reading "issue class unrecognised": no playbook, no session."""
    remediation_id = await a_create_job(seed, enqueue, issue_class="haunted")
    fake_budget(devin_api)

    await handlers.create_session(context, await claim())

    assert devin_api.sent("POST", SESSIONS) == []
    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.BLOCKED
    assert remediation.blocked_reason == UNKNOWN_ISSUE_CLASS

    created, escalation = await job_rows(session_factory)
    assert created.status == JobStatus.DONE
    assert escalation.payload == {"reason": UNKNOWN_ISSUE_CLASS, "issue_class": "haunted"}


async def test_create_session_completes_a_remediation_cancelled_while_it_queued(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await a_create_job(seed, enqueue, state=State.FAILED.value)

    await handlers.create_session(context, await claim())

    assert devin_api.sent("POST", SESSIONS) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_a_create_job_with_no_delivery_id_fails_before_it_spends_anything(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `run:` tag is the correlation id end to end; a job without one cannot produce it."""
    remediation_id = await seed()
    await enqueue(JobKind.CREATE_SESSION, {}, remediation_id)
    fake_budget(devin_api)

    await worker.run_job(context, await claim())

    assert devin_api.sent("POST", SESSIONS) == []
    created, escalation = await job_rows(session_factory)
    assert created.status == JobStatus.FAILED
    assert created.attempts == 1, "a malformed payload is not retried"
    assert "delivery_id" in (created.last_error or "")
    assert escalation.kind == JobKind.ESCALATE
    assert (await remediation_row(session_factory, remediation_id)).state == State.FAILED


# ------------------------------------------------------------------------------- resume_session


async def a_resume_job(
    seed: Seed,
    enqueue: Enqueue,
    *,
    state: State = State.CI_FAILED,
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> int:
    remediation_id = await seed(
        state=state.value,
        devin_session_id=SESSION_ID,
        devin_session_url=SESSION_URL,
        pr_number=PR_NUMBER,
        pr_url=PR_URL,
        **overrides,
    )
    await enqueue(JobKind.RESUME_SESSION, payload or {}, remediation_id)
    return remediation_id


async def test_ci_failure_resumes_the_same_session_with_the_failing_job(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    remediation_id = await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    fake_ci(github_api, log="E   assert 1 == 2\nFAILED tests/test_x.py::test_y")
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    message = devin_api.only("POST", MESSAGES).json["message"]
    assert message.startswith(f"CI failed on {HEAD_SHA}. Failing job: pytest.")
    assert "FAILED tests/test_x.py::test_y" in message
    assert "Diagnose the failure and push a fix to the same branch." in message
    assert f"This is fix cycle 1 of {settings.max_fix_cycles}." in message

    assert devin_api.only("POST", TAGS).json == {"tags": ["cycle:1"]}

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.RUNNING
    assert remediation.cycle == 1
    assert remediation.devin_session_id == SESSION_ID, "the same session, never a new one"

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE

    [event] = await event_rows(session_factory)
    assert (event.from_state, event.to_state) == (State.CI_FAILED, State.RUNNING)
    assert event.detail == {"source": "worker", "trigger": "session_resumed", "cycle": 1}


async def test_ci_failure_falls_back_to_the_pull_requests_head_when_the_payload_has_no_sha(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    await a_resume_job(seed, enqueue)
    github_api.responds(
        "GET", f"{PULLS}/{PR_NUMBER}", json=github_payload("pull_request.opened")["pull_request"]
    )
    fake_ci(github_api)
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    assert HEAD_SHA in devin_api.only("POST", MESSAGES).json["message"]
    assert github_api.sent("GET", f"{ACTIONS}/runs")[0].url.params["head_sha"] == HEAD_SHA


async def test_ci_failure_with_nothing_to_show_still_states_the_failure(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    """A conclusion from an app other than Actions leaves no job and no log to forward."""
    await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    github_api.responds("GET", f"{ACTIONS}/runs", json={"total_count": 0, "workflow_runs": []})
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    message = devin_api.only("POST", MESSAGES).json["message"]
    assert f"Failing job: {NO_FAILING_JOB}." in message
    assert NO_LOG_OUTPUT in message


async def test_ci_failure_forwards_an_expired_log_as_the_parenthetical(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    """GitHub expires Actions logs; losing the log must not lose the failure."""
    await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    fake_ci(github_api)
    github_api.responds("GET", f"{ACTIONS}/jobs/{CI_JOB_ID}/logs", 410, json={"message": "Gone"})
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    message = devin_api.only("POST", MESSAGES).json["message"]
    assert "Failing job: pytest." in message
    assert NO_LOG_OUTPUT in message


async def test_changes_requested_forwards_the_body_and_the_inline_comments(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A review can request changes with an empty body and say everything on the diff."""
    remediation_id = await a_resume_job(
        seed,
        enqueue,
        state=State.CHANGES_REQUESTED,
        cycle=1,
        payload={"review_id": REVIEW_ID, "review_body": "Please split this."},
    )
    github_api.responds(
        "GET",
        REVIEW_COMMENTS,
        json=[
            a_review_comment(),
            a_review_comment(id=2, path="superset/db.py", line=None, body="And this cast."),
        ],
    )
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    message = devin_api.only("POST", MESSAGES).json["message"]
    assert message.startswith(f"A reviewer requested changes on {PR_URL}.")
    assert "Please split this." in message
    assert "superset/connectors/sqla/models.py:1462\nThis still swallows the DB error." in message
    assert "superset/db.py\nAnd this cast." in message
    assert "Address the review and push a fix to the same branch." in message
    assert "This is fix cycle 2 of 3." in message

    assert devin_api.only("POST", TAGS).json == {"tags": ["cycle:2"]}
    assert github_api.only("GET", REVIEW_COMMENTS).path == REVIEW_COMMENTS

    remediation = await remediation_row(session_factory, remediation_id)
    assert (remediation.state, remediation.cycle) == (State.RUNNING, 2)


async def test_a_review_with_nothing_written_anywhere_says_so(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    await a_resume_job(
        seed, enqueue, state=State.CHANGES_REQUESTED, payload={"review_id": REVIEW_ID}
    )
    github_api.responds("GET", REVIEW_COMMENTS, json=[a_review_comment(body="")])
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    assert NO_REVIEW_FEEDBACK in devin_api.only("POST", MESSAGES).json["message"]


async def test_changes_requested_without_a_review_id_forwards_the_body_alone(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    await a_resume_job(
        seed, enqueue, state=State.CHANGES_REQUESTED, payload={"review_body": "Not yet."}
    )
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    assert "Not yet." in devin_api.only("POST", MESSAGES).json["message"]
    assert github_api.sent("GET", REVIEW_COMMENTS) == []


async def test_a_ci_job_claimed_after_a_walk_to_the_review_edge_sends_nothing(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The wrong-edge bug, as reported.

    `CI_FAILED -> CI_PASSED -> IN_REVIEW -> CHANGES_REQUESTED` needs no `RUNNING` — all three are
    legal, unabsorbed transitions. A `resume_session` enqueued by the check-suite failure and
    claimed after that walk used to take the review branch and tell the session a reviewer had
    requested changes, with nothing to act on.
    """
    remediation_id = await a_resume_job(
        seed, enqueue, state=State.CHANGES_REQUESTED, payload={"head_sha": HEAD_SHA}
    )
    fake_ci(github_api)
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    assert devin_api.sent("POST", MESSAGES) == [], "no message may be sent on the wrong edge"
    assert devin_api.sent("POST", TAGS) == []
    remediation = await remediation_row(session_factory, remediation_id)
    assert (remediation.state, remediation.cycle) == (State.CHANGES_REQUESTED, 0)
    assert await event_rows(session_factory) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_the_real_review_is_still_forwarded_after_a_stale_ci_job_is_dropped(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The harm that outlived the wrong message: the stale job left the remediation `RUNNING`, so
    the real review's own job failed `is_legal` and was completed in silence."""
    remediation_id = await a_resume_job(
        seed, enqueue, state=State.CHANGES_REQUESTED, payload={"head_sha": HEAD_SHA}
    )
    await enqueue(
        JobKind.RESUME_SESSION,
        {"review_id": REVIEW_ID, "review_body": "Split this up."},
        remediation_id,
    )
    fake_ci(github_api)
    github_api.responds("GET", REVIEW_COMMENTS, json=[a_review_comment()])
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())
    await handlers.resume_session(context, await claim(OTHER_WORKER))

    message = devin_api.only("POST", MESSAGES).json["message"]
    assert message.startswith(f"A reviewer requested changes on {PR_URL}.")
    assert "Split this up." in message
    assert "This still swallows the DB error." in message

    remediation = await remediation_row(session_factory, remediation_id)
    assert (remediation.state, remediation.cycle) == (State.RUNNING, 1), "exactly one lap"
    assert [job.status for job in await job_rows(session_factory)] == [JobStatus.DONE] * 2


@pytest.mark.parametrize(
    ("state", "payload", "sends"),
    [
        (State.CI_FAILED, {"trigger": "check_suite_failed", "head_sha": HEAD_SHA}, True),
        (State.CI_FAILED, {"trigger": "changes_requested", "review_id": REVIEW_ID}, False),
        (State.CHANGES_REQUESTED, {"trigger": "changes_requested", "review_id": REVIEW_ID}, True),
        (State.CHANGES_REQUESTED, {"trigger": "check_suite_failed", "head_sha": HEAD_SHA}, False),
        # No declared trigger: the review evidence is the fallback. A check-suite failure carries
        # none of it, and a review that carried none could not have said anything either.
        (State.CHANGES_REQUESTED, {"review_body": "No."}, True),
        (State.CHANGES_REQUESTED, {"review_id": REVIEW_ID}, True),
        (State.CHANGES_REQUESTED, {"head_sha": HEAD_SHA}, False),
        (State.CHANGES_REQUESTED, {}, False),
        # An unrecognised trigger value falls through to the evidence rather than deciding on it.
        (State.CHANGES_REQUESTED, {"trigger": "sunspots", "review_id": REVIEW_ID}, True),
        (State.CI_FAILED, {}, True),
    ],
)
async def test_the_declared_trigger_decides_the_edge_and_the_evidence_is_the_fallback(
    state: State,
    payload: dict[str, Any],
    sends: bool,
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
) -> None:
    await a_resume_job(seed, enqueue, state=state, payload=payload)
    fake_ci(github_api)
    github_api.responds("GET", f"{PULLS}/{PR_NUMBER}", json=a_pull_request())
    github_api.responds("GET", REVIEW_COMMENTS, json=[])
    fake_resume(devin_api)

    await handlers.resume_session(context, await claim())

    assert bool(devin_api.sent("POST", MESSAGES)) is sends


def test_a_resume_job_is_stale_only_across_the_edge_it_was_enqueued_on() -> None:
    """The predicate on its own, over the two loop states — no database, no HTTP."""
    assert handlers.LOOP_TRIGGERS == {
        State.CI_FAILED: Trigger.CHECK_SUITE_FAILED,
        State.CHANGES_REQUESTED: Trigger.CHANGES_REQUESTED,
    }
    for state, trigger in handlers.LOOP_TRIGGERS.items():
        assert not handlers.enqueued_on_another_edge(state, {"trigger": trigger.value})
        other = next(t for t in handlers.LOOP_TRIGGERS.values() if t is not trigger)
        assert handlers.enqueued_on_another_edge(state, {"trigger": other.value})


async def test_the_cycle_limit_fails_the_remediation_without_another_message(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remediation_id = await a_resume_job(seed, enqueue, cycle=settings.max_fix_cycles)

    await handlers.resume_session(context, await claim())

    assert devin_api.sent("POST", MESSAGES) == []
    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.FAILED
    assert remediation.blocked_reason == CYCLE_LIMIT_EXHAUSTED
    assert remediation.closed_at is not None

    resumed, escalation = await job_rows(session_factory)
    assert resumed.status == JobStatus.DONE
    assert escalation.payload["reason"] == CYCLE_LIMIT_EXHAUSTED


@pytest.mark.parametrize("state", [State.RUNNING, State.MERGED, State.FAILED, State.IN_REVIEW])
async def test_a_resume_that_no_longer_applies_is_completed_without_a_message(
    state: State,
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cancelled while it queued, or already resumed by a run that then lost its lease."""
    await a_resume_job(seed, enqueue, state=state)

    await handlers.resume_session(context, await claim())

    assert devin_api.sent("POST", MESSAGES) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE
    assert await event_rows(session_factory) == []


async def test_a_reclaimed_resume_does_not_send_a_second_message(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same window as `create_session`, and closed the same way.

    The `RUNNING` transition and the cycle increment are committed before the job is completed, so
    the worker that reclaims the job finds a state the resume is no longer legal from and sends
    nothing. One message, one cycle.
    """
    remediation_id = await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    fake_ci(github_api)
    fake_resume(devin_api)

    slow = await claim()
    await steal_lease(session_factory, slow.id)
    await worker.run_job(context, slow)

    reclaimed = replace(slow, locked_by=OTHER_WORKER)
    await worker.run_job(context, reclaimed)

    devin_api.only("POST", MESSAGES)
    remediation = await remediation_row(session_factory, remediation_id)
    assert (remediation.state, remediation.cycle) == (State.RUNNING, 1)
    assert len(await event_rows(session_factory)) == 1
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_a_failed_cycle_tag_does_not_cost_the_lap(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The message has already gone; failing here would re-send it or fail the remediation."""
    remediation_id = await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    fake_ci(github_api)
    devin_api.responds("POST", MESSAGES, 202)
    devin_api.responds("POST", TAGS, 500, json={"error": "nope"})

    await worker.run_job(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert (remediation.state, remediation.cycle) == (State.RUNNING, 1)
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


@pytest.mark.parametrize("moved_to", [State.RUNNING, State.MERGED])
async def test_a_remediation_moved_while_the_message_was_in_flight_is_not_walked_backwards(
    moved_to: State,
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The transition is computed from a re-read row, not from the one the plan was made on.

    The remediation is moved between the resume message and the transition — by another worker that
    got there first, or by a webhook. Without the re-read the handler would either raise on an
    illegal transition or stamp a second `RUNNING` event on a remediation that has since merged.
    """
    remediation_id = await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    fake_ci(github_api)
    devin_api.responds("POST", MESSAGES, 202)

    async def move_the_remediation(request: httpx.Request) -> httpx.Response:
        async with session_factory() as db:
            await db.execute(
                update(Remediation)
                .where(Remediation.id == remediation_id)
                .values(state=moved_to.value, cycle=1)
            )
            await db.commit()
        return httpx.Response(202)

    devin_api.route("POST", TAGS).mock(side_effect=move_the_remediation)

    await worker.run_job(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert (remediation.state, remediation.cycle) == (moved_to, 1)
    assert await event_rows(session_factory) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_a_cycle_spent_while_the_message_was_in_flight_still_escalates(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second place the cycle limit is reached, and the one that used to fail silently.

    Worker A sends its resume; its lease expires, B completes the lap, and a fresh check suite
    fails back into `CI_FAILED` with the last cycle spent. A's `_record_resume` re-reads, passes
    `is_legal`, and now yields `FAILED` — which must escalate like every other route to `FAILED`,
    not just write the column.
    """
    remediation_id = await a_resume_job(seed, enqueue, payload={"head_sha": HEAD_SHA})
    fake_ci(github_api)
    devin_api.responds("POST", MESSAGES, 202)

    async def spend_the_last_cycle(request: httpx.Request) -> httpx.Response:
        async with session_factory() as db:
            await db.execute(
                update(Remediation)
                .where(Remediation.id == remediation_id)
                .values(cycle=settings.max_fix_cycles)
            )
            await db.commit()
        return httpx.Response(202)

    devin_api.route("POST", TAGS).mock(side_effect=spend_the_last_cycle)

    await worker.run_job(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.FAILED
    assert remediation.blocked_reason == CYCLE_LIMIT_EXHAUSTED
    assert remediation.closed_at is not None

    resumed, escalation = await job_rows(session_factory)
    assert resumed.status == JobStatus.DONE
    assert escalation.kind == JobKind.ESCALATE, "a FAILED remediation must reach a human"
    assert escalation.payload["reason"] == CYCLE_LIMIT_EXHAUSTED


async def test_a_malformed_review_id_degrades_like_an_absent_one(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`int("none")` would raise, and `ValueError` is not permanent — so the remediation would be
    retried five times and then failed over a payload value."""
    await a_resume_job(
        seed,
        enqueue,
        state=State.CHANGES_REQUESTED,
        payload={"review_id": "not-an-id", "review_body": "Please split this."},
    )
    fake_resume(devin_api)

    await worker.run_job(context, await claim())

    assert "Please split this." in devin_api.only("POST", MESSAGES).json["message"]
    assert github_api.sent("GET", REVIEW_COMMENTS) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


# ------------------------------------------------------------------------------------- escalate


async def an_escalate_job(
    seed: Seed, enqueue: Enqueue, *, reason: str, state: State = State.FAILED, **overrides: Any
) -> int:
    remediation_id = await seed(
        state=state.value,
        blocked_reason=reason,
        devin_session_url=SESSION_URL,
        **overrides,
    )
    await enqueue(JobKind.ESCALATE, {"reason": reason, "state": state.value}, remediation_id)
    return remediation_id


async def test_escalate_labels_the_issue_and_comments_with_the_reason(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await an_escalate_job(seed, enqueue, reason=BUDGET_EXHAUSTED, state=State.BLOCKED)
    fake_escalation_targets(github_api)

    await handlers.escalate(context, await claim())

    assert github_api.only("POST", f"{ISSUE}/labels").json == {"labels": [NEEDS_HUMAN_LABEL]}
    body = github_api.only("POST", f"{ISSUE}/comments").json["body"]
    assert BUDGET_EXHAUSTED in body
    assert SESSION_URL in body
    assert State.BLOCKED in body

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_escalate_labels_before_it_comments(
    context: Context, seed: Seed, enqueue: Enqueue, claim: Claim, github_api: FakeAPI
) -> None:
    """Adding a label twice is free; commenting twice is a second escalation on the same issue."""
    await an_escalate_job(seed, enqueue, reason=JOB_FAILED)
    fake_escalation_targets(github_api)

    await handlers.escalate(context, await claim())

    assert [request.path for request in github_api.requests] == [
        f"{ISSUE}/labels",
        f"{ISSUE}/comments",
    ]


def test_the_suppressed_reasons_are_the_two_the_cancellation_record_names() -> None:
    """Written out rather than read from the constant: a test parametrised over the set it is
    checking would pass by having nothing to run if the set were ever emptied.

    The strings are `sentinel.github.events.Reason`'s, which this module cannot import until T20
    merges. Pinning them here is what keeps the two spellings from drifting in the meantime.
    """
    assert sorted(CANCELLED_BY_A_HUMAN) == ["autofix_label_removed", "issue_closed"]
    assert ABANDONED not in CANCELLED_BY_A_HUMAN


@pytest.mark.parametrize("reason", ["autofix_label_removed", "issue_closed"])
async def test_escalation_is_suppressed_when_a_maintainer_called_the_work_off(
    reason: str,
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md`: do not tell a person to look at
    the thing they were looking at when they stopped it."""
    await an_escalate_job(seed, enqueue, reason=reason)
    fake_escalation_targets(github_api)

    await handlers.escalate(context, await claim())

    assert github_api.requests == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_an_abandoned_pull_request_is_escalated_rather_than_suppressed(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`docs/adr/2026-08-09-an-abandoned-pull-request-still-escalates.md`.

    A maintainer closing Devin's pull request is as deliberate as closing the issue, and it is the
    one of the three that leaves the issue open and still carrying `devin:autofix` — so it is the
    one the escalation is for.
    """
    await an_escalate_job(seed, enqueue, reason=ABANDONED)
    fake_escalation_targets(github_api)

    await handlers.escalate(context, await claim())

    assert github_api.only("POST", f"{ISSUE}/labels").json == {"labels": [NEEDS_HUMAN_LABEL]}
    assert ABANDONED in github_api.only("POST", f"{ISSUE}/comments").json["body"]
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_escalate_reads_the_reason_from_the_column_not_the_payload(
    context: Context, seed: Seed, enqueue: Enqueue, claim: Claim, github_api: FakeAPI
) -> None:
    """`remediation.blocked_reason` is the one place the ingress is obliged to write the reason, and
    what the failure breakdown groups on. The `escalate` payload is specified by no record, so it
    must not be what decides whether a human is told."""
    remediation_id = await seed(state=State.FAILED.value, blocked_reason="issue_closed")
    await enqueue(JobKind.ESCALATE, {"reason": "session_error"}, remediation_id)
    fake_escalation_targets(github_api)

    await handlers.escalate(context, await claim())

    assert github_api.requests == []


async def test_escalate_falls_back_to_the_payload_when_the_column_is_empty(
    context: Context, seed: Seed, enqueue: Enqueue, claim: Claim, github_api: FakeAPI
) -> None:
    """A producer that filled only the payload still escalates on something legible."""
    remediation_id = await seed(state=State.FAILED.value)
    await enqueue(JobKind.ESCALATE, {"reason": "session_error"}, remediation_id)
    fake_escalation_targets(github_api)

    await handlers.escalate(context, await claim())

    assert "session_error" in github_api.only("POST", f"{ISSUE}/comments").json["body"]


def test_the_escalation_comment_names_the_reason_the_state_and_the_session() -> None:
    comment = escalation_comment(
        state=State.BLOCKED.value,
        reason=BUDGET_EXHAUSTED,
        session_url=SESSION_URL,
        detail={"acus_spent": 104.5},
    )

    assert "- State: `BLOCKED`" in comment
    assert f"- Reason: `{BUDGET_EXHAUSTED}`" in comment
    assert f"- Devin session: {SESSION_URL}" in comment
    assert "- acus_spent: `104.5`" in comment
    assert comment.endswith("automatically.")


def test_the_escalation_comment_omits_a_session_that_never_existed() -> None:
    comment = escalation_comment(state=State.BLOCKED.value, reason=UNKNOWN_ISSUE_CLASS)

    assert "Devin session" not in comment
    assert f"- Reason: `{UNKNOWN_ISSUE_CLASS}`" in comment


# ------------------------------------------------------------------------------------- sync_acu


async def test_sync_acu_writes_every_day_the_endpoint_reports(
    context: Context,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await enqueue(JobKind.SYNC_ACU, {}, None)
    devin_api.responds(
        "GET",
        CONSUMPTION,
        json=a_consumption_body(
            (datetime.date(2026, 8, 7), 12.5), (datetime.date(2026, 8, 8), 3.25)
        ),
    )

    await handlers.sync_acu(context, await claim())

    yesterday, today = await ledger_rows(session_factory)
    assert (yesterday.day.isoformat(), yesterday.acus) == ("2026-08-07", Decimal("12.500"))
    assert (today.day.isoformat(), today.acus) == ("2026-08-08", Decimal("3.250"))
    assert yesterday.synced_at is not None

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_sync_acu_updates_a_day_it_has_already_written(
    context: Context,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await enqueue(JobKind.SYNC_ACU, {}, None)
    await enqueue(JobKind.SYNC_ACU, {}, None)
    devin_api.route("GET", CONSUMPTION).mock(
        side_effect=[
            httpx.Response(200, json=a_consumption_body((datetime.date(2026, 8, 8), 3.25))),
            httpx.Response(200, json=a_consumption_body((datetime.date(2026, 8, 8), 9.0))),
        ]
    )

    await handlers.sync_acu(context, await claim())
    await handlers.sync_acu(context, await claim(OTHER_WORKER))

    [row] = await ledger_rows(session_factory)
    assert row.acus == Decimal("9.000")


async def test_sync_acu_succeeds_when_the_organisation_has_no_consumption_scope(
    context: Context,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The endpoint degrades to a value; the budget guard has two other sources."""
    await enqueue(JobKind.SYNC_ACU, {}, None)
    devin_api.responds("GET", CONSUMPTION, 403, json={"message": "no scope"})

    await handlers.sync_acu(context, await claim())

    assert await ledger_rows(session_factory) == []
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


# --------------------------------------------------------------------------------- the worker


async def test_run_once_reports_an_empty_queue(context: Context) -> None:
    assert await worker.run_once(context, claimed_by=WORKER) is False


async def test_run_once_claims_and_dispatches(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_session_creation(devin_api)

    assert await worker.run_once(context, claimed_by=WORKER) is True

    assert (await remediation_row(session_factory, remediation_id)).state == State.SESSION_CREATED
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE


async def test_run_drains_the_backlog_before_it_waits(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for number in range(3):
        remediation_id = await seed(issue_number=2000 + number, state=State.FAILED.value)
        await enqueue(JobKind.ESCALATE, {"reason": "session_error"}, remediation_id)
    for number in range(3):
        github_api.responds("POST", f"{ISSUES}/{2000 + number}/labels", json=[])
        github_api.responds(
            "POST", f"{ISSUES}/{2000 + number}/comments", 201, json={"id": 1, "html_url": "u"}
        )

    stop = asyncio.Event()
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        stop.set()

    await worker.run(context, claimed_by=WORKER, stop=stop, sleep=sleep)

    assert [job.status for job in await job_rows(session_factory)] == [JobStatus.DONE] * 3
    assert delays == [worker.IDLE_DELAY_SECONDS], "only an empty queue waits"


async def test_an_unknown_job_kind_fails_without_a_retry(
    context: Context,
    seed: Seed,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remediation_id = await seed()
    async with session_factory() as db:
        await db.execute(
            Job.__table__.insert().values(
                remediation_id=remediation_id, kind="teleport", payload={}, status="pending"
            )
        )
        await db.commit()

    await worker.run_once(context, claimed_by=WORKER)

    teleport, escalation = await job_rows(session_factory)
    assert teleport.status == JobStatus.FAILED
    assert teleport.attempts == 1
    assert "teleport" in (teleport.last_error or "")
    assert escalation.kind == JobKind.ESCALATE
    assert (await remediation_row(session_factory, remediation_id)).blocked_reason == JOB_FAILED


async def test_a_retryable_failure_is_scheduled_rather_than_given_up_on(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_budget(devin_api)
    fake_no_session_yet(devin_api)
    devin_api.responds("POST", SESSIONS, 503, json={"message": "later"})

    await worker.run_once(context, claimed_by=WORKER)

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.PENDING
    assert job.attempts == 1
    assert job.run_after > datetime.datetime.now(datetime.UTC)
    assert (await remediation_row(session_factory, remediation_id)).state == State.QUEUED


async def test_a_rejected_body_fails_immediately(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`4xx` other than `429`: retrying a validation error only wastes quota."""
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_budget(devin_api)
    fake_no_session_yet(devin_api)
    devin_api.responds("POST", SESSIONS, 422, json={"message": "unregistered tag"})

    await worker.run_once(context, claimed_by=WORKER)

    created, escalation = await job_rows(session_factory)
    assert created.status == JobStatus.FAILED
    assert created.attempts == 1
    assert "unregistered tag" in (created.last_error or "")

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.FAILED
    assert remediation.blocked_reason == JOB_FAILED
    assert escalation.payload["error"].startswith("DevinAPIError")

    [event] = await event_rows(session_factory)
    assert event.kind == "error"
    assert event.detail["job_kind"] == JobKind.CREATE_SESSION


async def test_a_github_4xx_fails_immediately_like_a_devin_one(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Non-retryable row applies to both APIs. The GitHub half is a different branch of
    `_retryable` from the Devin half, and it is reached through a different exception type."""
    remediation_id = await an_escalate_job(seed, enqueue, reason="session_error")
    github_api.responds("POST", f"{ISSUE}/labels", 422, json={"message": "no such label"})

    await worker.run_once(context, claimed_by=WORKER)

    escalation = (await job_rows(session_factory))[0]
    assert escalation.status == JobStatus.FAILED
    assert escalation.attempts == 1, "a 4xx must not be retried"
    assert "no such label" in (escalation.last_error or "")
    assert (await remediation_row(session_factory, remediation_id)).state == State.FAILED


async def test_a_github_404_is_not_retried_either(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await an_escalate_job(seed, enqueue, reason="session_error")
    github_api.responds("POST", f"{ISSUE}/labels", 404, json={"message": "Not Found"})

    await worker.run_once(context, claimed_by=WORKER)

    escalation = (await job_rows(session_factory))[0]
    assert escalation.status == JobStatus.FAILED
    assert escalation.attempts == 1


async def test_the_last_attempt_takes_the_remediation_with_it(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`MAX_JOB_ATTEMPTS` "then transitions the remediation to `FAILED`"."""
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_budget(devin_api)
    fake_no_session_yet(devin_api)
    devin_api.responds("POST", SESSIONS, 503, json={"message": "later"})

    async with session_factory() as db:
        await db.execute(
            update(Job).values(attempts=settings.max_job_attempts - 1, run_after=text("now()"))
        )
        await db.commit()

    await worker.run_once(context, claimed_by=WORKER)

    created, escalation = await job_rows(session_factory)
    assert created.status == JobStatus.FAILED
    assert created.attempts == settings.max_job_attempts
    assert escalation.payload["attempts"] == settings.max_job_attempts
    assert (await remediation_row(session_factory, remediation_id)).state == State.FAILED


async def test_a_rate_limit_with_an_answer_defers_for_exactly_that_long(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GitHub named a delay; honouring it is better than backing off against a guess."""
    await an_escalate_job(seed, enqueue, reason="session_error")
    github_api.responds(
        "POST",
        f"{ISSUE}/labels",
        429,
        headers={"retry-after": "300"},
        json={"message": "slow down"},
    )

    before = datetime.datetime.now(datetime.UTC)
    await worker.run_once(context, claimed_by=WORKER)

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DEFERRED
    assert job.attempts == 0, "a rate limit is not a failed attempt"
    assert job.run_after >= before + datetime.timedelta(seconds=299)


async def test_a_rate_limit_with_no_answer_falls_back_to_the_queues_own_backoff(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The client deliberately does not invent a delay, so there is no schedule to honour."""
    await an_escalate_job(seed, enqueue, reason="session_error")
    github_api.responds(
        "POST",
        f"{ISSUE}/labels",
        403,
        headers={"x-ratelimit-remaining": "0"},
        json={"message": "rate limited"},
    )

    await worker.run_once(context, claimed_by=WORKER)

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.PENDING
    assert job.attempts == 1


async def test_a_lost_lease_leaves_the_row_to_the_worker_that_holds_it(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker that lost its lease must not fail, retry or defer a job somebody else is running."""
    remediation_id = await a_create_job(seed, enqueue)
    fake_issue(github_api)
    fake_budget(devin_api)
    fake_no_session_yet(devin_api)
    devin_api.responds("POST", SESSIONS, 503, json={"message": "later"})

    slow = await claim()
    await steal_lease(session_factory, slow.id)

    await worker.run_job(context, slow)

    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.RUNNING
    assert job.locked_by == OTHER_WORKER
    assert job.attempts == 0
    assert (await remediation_row(session_factory, remediation_id)).state == State.QUEUED


# -------------------------------------------------------------------------------- evaluate_ci


GATE = "devin-autofix-ci"

# Two of the five jobs of the fork's own workflow. `pytest (scoped)` skips on a frontend-only diff,
# which must not hold the pull request back.
OUR_WORKFLOW = (("jest (scoped)", "success"), ("pytest (scoped)", "skipped"))

# What concluded first on PR #9, and what Sentinel read as CI green.
INHERITED = (("check-hold-label", "success"), ("labeler", "success"))


def a_check_run_body(name: str, conclusion: str | None, status: str = "completed") -> Any:
    return {
        "id": abs(hash((name, conclusion, status))) % 10**10,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": None,
        "started_at": None,
        "completed_at": None,
    }


def fake_head_sha(github_api: FakeAPI, *runs: Any) -> None:
    """The two routes `evaluate_ci` reads: the pull request, then the check runs on its head."""
    bodies = [run if isinstance(run, dict) else a_check_run_body(*run) for run in runs]
    github_api.responds(
        "GET", f"{PULLS}/{PR_NUMBER}", json=a_pull_request(head={"sha": HEAD_SHA, "ref": "fix"})
    )
    github_api.responds(
        "GET",
        f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs",
        json={"total_count": len(bodies), "check_runs": bodies},
    )


async def an_evaluation_job(
    seed: Seed, enqueue: Enqueue, *, state: State = State.CI_RUNNING, **overrides: Any
) -> int:
    remediation_id = await seed(
        state=state.value,
        devin_session_id=SESSION_ID,
        devin_session_url=SESSION_URL,
        pr_number=PR_NUMBER,
        pr_url=PR_URL,
        **overrides,
    )
    await enqueue(JobKind.EVALUATE_CI, {"pr_number": PR_NUMBER}, remediation_id)
    return remediation_id


async def test_a_trivially_green_inherited_workflow_is_not_ci_green(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The defect, through the handler.

    On PR #9 `check-hold-label` concluded thirteen seconds after the pull request opened, Sentinel
    moved to `CI_PASSED` and then straight to `IN_REVIEW`, and the scoped suite that judges the diff
    had another three and a quarter minutes to run. Nothing here may reach `CI_PASSED`.
    """
    remediation_id = await an_evaluation_job(seed, enqueue)
    fake_head_sha(github_api, *INHERITED)

    await handlers.evaluate_ci(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.CI_RUNNING
    assert remediation.ci_green_at is None


async def test_a_failure_outside_our_workflow_does_not_resume_the_session(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`Dependency Review` fails on every pull request in the fork because the repository has no
    dependency graph enabled. It reached Sentinel as `check_suite.completed` with conclusion
    `failure`, moved the remediation `IN_REVIEW -> CI_FAILED`, and resumed the session five seconds
    later — a fix cycle spent asking Devin to change a repository setting from a two-file diff.

    It holds the pull request out of `CI_PASSED`, which is honest, and it resumes nothing.
    """
    remediation_id = await an_evaluation_job(seed, enqueue, state=State.IN_REVIEW)
    fake_head_sha(
        github_api, *INHERITED, *OUR_WORKFLOW, (GATE, "success"), ("dependency-review", "failure")
    )

    await handlers.evaluate_ci(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.IN_REVIEW
    assert remediation.cycle == 0
    assert not devin_api.sent("POST", MESSAGES)
    assert [job.kind for job in await job_rows(session_factory)] == [JobKind.EVALUATE_CI]


async def test_the_gate_failing_resumes_the_session(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other half: a rule that never reports failure is as broken as one that never reports
    success. The resume job names the edge, because `enqueued_on_another_edge` reads it."""
    remediation_id = await an_evaluation_job(seed, enqueue)
    fake_head_sha(github_api, *INHERITED, *OUR_WORKFLOW, (GATE, "failure"))

    await handlers.evaluate_ci(context, await claim())

    assert (await remediation_row(session_factory, remediation_id)).state == State.CI_FAILED
    [_, resume] = await job_rows(session_factory)
    assert resume.kind == JobKind.RESUME_SESSION
    assert resume.payload["trigger"] == "check_suite_failed"
    assert resume.payload["head_sha"] == HEAD_SHA


async def test_the_gate_failing_is_reported_without_waiting_for_the_rest(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Holding a failure the session can act on until an unrelated Cypress shard finishes is
    latency spent on nothing."""
    remediation_id = await an_evaluation_job(seed, enqueue)
    fake_head_sha(
        github_api,
        *OUR_WORKFLOW,
        (GATE, "failure"),
        a_check_run_body("cypress-matrix (chrome)", None, status="in_progress"),
    )

    await handlers.evaluate_ci(context, await claim())

    assert (await remediation_row(session_factory, remediation_id)).state == State.CI_FAILED


async def test_a_green_head_sha_passes_ci_and_asks_for_review(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Entering `CI_PASSED` obliges the caller to apply `REVIEW_REQUESTED`, and each transition
    writes its own `remediation_event` — invariant 4 of `docs/04-state-machine.md`."""
    remediation_id = await an_evaluation_job(seed, enqueue)
    fake_head_sha(github_api, *INHERITED, *OUR_WORKFLOW, (GATE, "success"))

    await handlers.evaluate_ci(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.IN_REVIEW
    assert remediation.ci_green_at is not None
    assert [(event.from_state, event.to_state) for event in await event_rows(session_factory)] == [
        (State.CI_RUNNING, State.CI_PASSED),
        (State.CI_PASSED, State.IN_REVIEW),
    ]


async def test_a_second_evaluation_of_a_green_pull_request_writes_nothing(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every completed suite enqueues an evaluation, and 18 of them concluded on PR #9. Once the
    remediation is in review the trigger absorbs, and an absorbed evaluation is not recorded — the
    delivery rows are the trail, as they are for the poller re-reading the same session."""
    green = datetime.datetime(2026, 8, 7, 16, 0, tzinfo=datetime.UTC)
    remediation_id = await an_evaluation_job(
        seed, enqueue, state=State.IN_REVIEW, ci_green_at=green
    )
    fake_head_sha(github_api, *INHERITED, *OUR_WORKFLOW, (GATE, "success"))

    await handlers.evaluate_ci(context, await claim())

    remediation = await remediation_row(session_factory, remediation_id)
    assert remediation.state == State.IN_REVIEW
    assert remediation.ci_green_at == green, "the first green, not this one"
    assert await event_rows(session_factory) == []


async def test_the_head_sha_comes_from_the_pull_request_not_the_payload(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A payload SHA can be stale by the time the job is claimed: Devin pushes a fix, `concurrency`
    cancels the previous run, and a cancelled run is complete-and-not-failing — the exact shape an
    "all clear" rule reads as green. The pull request's own head is the only current answer."""
    stale = "0" * 40
    remediation_id = await seed(
        state=State.CI_RUNNING.value,
        devin_session_id=SESSION_ID,
        pr_number=PR_NUMBER,
        pr_url=PR_URL,
    )
    await enqueue(JobKind.EVALUATE_CI, {"pr_number": PR_NUMBER, "head_sha": stale}, remediation_id)
    fake_head_sha(github_api, *OUR_WORKFLOW, (GATE, "success"))

    await handlers.evaluate_ci(context, await claim())

    assert (await remediation_row(session_factory, remediation_id)).state == State.IN_REVIEW
    assert github_api.sent("GET", f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs")
    assert not github_api.sent("GET", f"/repos/{REPO}/commits/{stale}/check-runs")


async def test_two_evaluations_of_the_same_head_sha_move_it_once(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every completed suite enqueues an evaluation, so two workers reading one green SHA is the
    ordinary case rather than a rare race. The second must find a state its trigger absorbs from,
    and must not re-apply `REVIEW_REQUESTED` — which is illegal from `IN_REVIEW` and would raise."""
    remediation_id = await an_evaluation_job(seed, enqueue)
    await enqueue(JobKind.EVALUATE_CI, {"pr_number": PR_NUMBER}, remediation_id)
    fake_head_sha(github_api, *INHERITED, *OUR_WORKFLOW, (GATE, "success"))

    await handlers.evaluate_ci(context, await claim())
    await handlers.evaluate_ci(context, await claim())

    assert (await remediation_row(session_factory, remediation_id)).state == State.IN_REVIEW
    assert [(event.from_state, event.to_state) for event in await event_rows(session_factory)] == [
        (State.CI_RUNNING, State.CI_PASSED),
        (State.CI_PASSED, State.IN_REVIEW),
    ]


async def test_an_evaluation_for_a_merged_remediation_calls_nothing(
    context: Context,
    seed: Seed,
    enqueue: Enqueue,
    claim: Claim,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Suites keep concluding after a merge — seven did on PR #9. Each would otherwise buy two
    GitHub calls to be told the remediation is over."""
    await an_evaluation_job(seed, enqueue, state=State.MERGED)

    await handlers.evaluate_ci(context, await claim())

    assert not github_api.requests
    [job] = await job_rows(session_factory)
    assert job.status == JobStatus.DONE
    assert await event_rows(session_factory) == []


async def test_every_job_kind_has_a_handler() -> None:
    """A kind added to the spec's table without a handler is a job nothing can run."""
    assert set(handlers.HANDLERS) == {kind.value for kind in JobKind}
