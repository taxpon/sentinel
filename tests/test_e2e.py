"""One remediation, driven from the label to the merge through the real code.

This is the case `docs/08-testing.md` calls the one that matters most: it exercises the review-fix
loop, which is otherwise only ever demonstrated by hand. Every layer below is the shipped one — the
webhook endpoint, the queue, the worker handlers, the poller, the state machine, the analytics —
and only the two HTTP boundaries are faked. What is asserted is not merely that the remediation
ends in `MERGED`, but the sequence of `remediation_event` rows it took to get there, because that
log is what every figure in [07](../docs/07-observability.md) is computed from.

Two properties of the loop are the point of the whole exercise, and each has an assertion of its
own:

- the **same** Devin session is resumed rather than a second one created, and
- `cycle` becomes 1, which is what the autonomy metric counts.

There is no `sleep` anywhere. The worker is driven a job at a time with `run_once` and the poller a
tick at a time with `poll_once`, so the ordering is the test's rather than the scheduler's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import DeliveryFactory, FakeAPI
from factories import ISSUE_NUMBER, PR_NUMBER, REPO, github_payload
from sentinel import db
from sentinel.api import analytics, webhooks
from sentinel.config import Settings, get_settings
from sentinel.devin.client import DevinClient
from sentinel.github.client import GitHubClient
from sentinel.models import Remediation, RemediationEvent
from sentinel.observability.prom import Metrics
from sentinel.pipeline import poller, worker
from sentinel.pipeline.handlers import Context
from sentinel.pipeline.state import State, Trigger

ORG = "org-abc123"
SESSIONS = f"/v3/organizations/{ORG}/sessions"
CONSUMPTION = f"/v3/organizations/{ORG}/consumption/daily"

ISSUE = f"/repos/{REPO}/issues/{ISSUE_NUMBER}"
ACTIONS = f"/repos/{REPO}/actions"

SESSION_ID = "sess-e2e"
SESSION_URL = f"https://app.devin.ai/sessions/{SESSION_ID}"
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
HEAD_SHA = "9d1f2c3b4a5e6f708192a3b4c5d6e7f809a1b2c3"
RUN_ID = 84410727864
CI_JOB_ID = 231855142299

WORKER = "worker-e2e"


# --- The system under test ------------------------------------------------------------------------


@pytest.fixture
async def api(
    asgi_client: Any, settings: Settings, process_engine: None, http_mock: respx.MockRouter
) -> AsyncIterator[Any]:
    """The `api` process: the routers a delivery and the dashboard actually reach."""
    app = FastAPI()
    app.include_router(webhooks.router)
    app.include_router(analytics.router)
    app.dependency_overrides[get_settings] = lambda: settings
    yield await asgi_client(app)
    await db.dispose_engine()


@pytest.fixture
async def devin(settings: Settings, metrics: Metrics) -> AsyncIterator[DevinClient]:
    async with DevinClient(settings, metrics=metrics) as client:
        yield client


@pytest.fixture
async def github(settings: Settings) -> AsyncIterator[GitHubClient]:
    async with GitHubClient(settings) as client:
        yield client


@pytest.fixture
def context(
    session_factory: async_sessionmaker[AsyncSession],
    devin: DevinClient,
    github: GitHubClient,
    settings: Settings,
) -> Context:
    """The `worker` process's world."""
    return Context(session_factory=session_factory, devin=devin, github=github, settings=settings)


# --- The outside world ----------------------------------------------------------------------------


ISSUE_CLASS = "security"
"""One of the eight classes, and one `devin_playbook_ids` is configured for in `conftest.py`.

The recorded `issues.labeled` payload carries `devin:autofix` but no `class:` label, so on its own
it is `unclassified` — which the worker correctly escalates instead of opening a session for. A
walkthrough of the happy path has to deliver a classified issue.
"""


def a_classified_issue() -> dict[str, Any]:
    """The recorded issue, with the class label a maintainer would have applied alongside."""
    issue = github_payload("issues.labeled")["issue"]
    return {**issue, "labels": [*issue["labels"], {"name": f"class:{ISSUE_CLASS}"}]}


def labelled(delivery: DeliveryFactory, delivery_id: str) -> Any:
    return delivery("issues.labeled", delivery_id=delivery_id, issue=a_classified_issue())


def a_devin_session(**overrides: Any) -> dict[str, Any]:
    return {"session_id": SESSION_ID, "status": "running", "url": SESSION_URL, **overrides}


@pytest.fixture
def outside(devin_api: FakeAPI, github_api: FakeAPI) -> Iterator[respx.Route]:
    """Everything Devin and GitHub answer over the whole walkthrough, registered once.

    Registered up front rather than restaged between steps, because a route added just before the
    call that needs it would let a step pass that only works when the test knows what is coming —
    and the point here is that the pipeline drives itself.
    """
    devin_api.responds("GET", CONSUMPTION, json={"days": []})
    creations = devin_api.responds("POST", SESSIONS, 201, a_devin_session(status="new"))
    devin_api.responds("POST", f"{SESSIONS}/{SESSION_ID}/messages", 202)
    devin_api.responds("POST", f"{SESSIONS}/{SESSION_ID}/tags", 202)

    github_api.responds("GET", ISSUE, json=a_classified_issue())
    github_api.responds(
        "GET",
        f"{ACTIONS}/runs",
        json={
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": RUN_ID,
                    "head_sha": HEAD_SHA,
                    "status": "completed",
                    "conclusion": "failure",
                    "run_started_at": "2026-08-09T10:00:00Z",
                }
            ],
        },
    )
    github_api.responds(
        "GET",
        f"{ACTIONS}/runs/{RUN_ID}/jobs",
        json={
            "total_count": 1,
            "jobs": [
                {
                    "id": CI_JOB_ID,
                    "run_id": RUN_ID,
                    "name": "pytest",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2026-08-09T10:00:30Z",
                    "html_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}",
                }
            ],
        },
    )
    github_api.responds(
        "GET",
        f"{ACTIONS}/jobs/{CI_JOB_ID}/logs",
        text="FAILED tests/test_models.py::test_it - AssertionError",
    )
    yield creations


# --- Reading the system's own record --------------------------------------------------------------


async def remediation(session_factory: async_sessionmaker[AsyncSession]) -> Remediation:
    async with session_factory() as db_session:
        return (await db_session.execute(select(Remediation))).scalar_one()


async def transitions(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    """The transition log, in the order it was written.

    Ordered by `id`, not by `created_at`: `created_at` is `transaction_timestamp()`, so two rows
    written in one transaction — which the automatic `CI_PASSED → IN_REVIEW` step produces — carry
    the same value and would come back in an arbitrary order.
    """
    async with session_factory() as db_session:
        rows = (
            await db_session.execute(select(RemediationEvent).order_by(RemediationEvent.id))
        ).scalars()
        return [f"{row.from_state or '-'}->{row.to_state}" for row in rows]


async def post(api: Any, signed: Any) -> Any:
    return await api.post("/webhooks/github", content=signed.body, headers=signed.headers)


async def drain(context: Context) -> int:
    """Run the worker until the queue is empty, and say how many jobs it ran."""
    ran = 0
    while await worker.run_once(context, claimed_by=WORKER):
        ran += 1
    return ran


async def tick(
    devin: DevinClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    devin_api: FakeAPI,
    **session_overrides: Any,
) -> None:
    """One poller tick, with Devin answering `session_overrides` about the session."""
    devin_api.responds("GET", f"{SESSIONS}/{SESSION_ID}", json=a_devin_session(**session_overrides))
    await poller.poll_once(devin, session_factory, settings=settings)


# --- The walkthrough ------------------------------------------------------------------------------


async def test_a_remediation_from_label_to_merge_through_a_failing_check_suite(
    api: Any,
    context: Context,
    devin: DevinClient,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    delivery: DeliveryFactory,
    outside: respx.Route,
) -> None:
    """label → session → PR → CI fails → resume on the same session → CI passes →
    review → merged."""
    # 1. A maintainer labels the issue. The endpoint answers before any of the work is done.
    assert (await post(api, labelled(delivery, "d-1"))).status_code == 202
    assert (await remediation(session_factory)).state == State.QUEUED

    # 2. The worker claims the job and opens the session.
    assert await drain(context) == 1
    opened = await remediation(session_factory)
    assert opened.state == State.SESSION_CREATED
    assert opened.devin_session_id == SESSION_ID

    # 3. Devin picks the session up. Nothing to link yet.
    await tick(devin, session_factory, settings, devin_api, status="running")
    assert (await remediation(session_factory)).state == State.RUNNING

    # 4. Devin opens the pull request. The *poller* is what links it — `pull_request.opened` carries
    #    nothing that resolves to a remediation, so the webhook cannot (see the ADR on linking).
    await tick(
        devin,
        session_factory,
        settings,
        devin_api,
        status="running",
        pull_requests=[{"url": PR_URL, "number": PR_NUMBER}],
    )
    linked = await remediation(session_factory)
    assert linked.state == State.PR_OPENED
    assert linked.pr_number == PR_NUMBER

    # 5. CI fails on the pull request. This is the trigger the whole loop exists for.
    failure = delivery("check_suite.completed.failure", delivery_id="d-2")
    assert (await post(api, failure)).status_code == 202
    assert (await remediation(session_factory)).state == State.CI_FAILED

    # 6. The worker resumes — the *same* session, with the failure in hand, on lap 1.
    assert await drain(context) == 1
    resumed = await remediation(session_factory)
    assert resumed.state == State.RUNNING
    assert resumed.cycle == 1
    assert resumed.devin_session_id == SESSION_ID, "resumed, not replaced"
    assert outside.call_count == 1, "no second session was created"

    # 7. The fix lands and CI goes green. Entering `CI_PASSED` moves straight on to `IN_REVIEW`.
    success = delivery("check_suite.completed.success", delivery_id="d-3")
    assert (await post(api, success)).status_code == 202
    assert (await remediation(session_factory)).state == State.IN_REVIEW

    # 8. A human merges it. Merging is never Sentinel's to do.
    merged = delivery("pull_request.closed.merged", delivery_id="d-4")
    assert (await post(api, merged)).status_code == 202

    final = await remediation(session_factory)
    assert final.state == State.MERGED
    assert final.cycle == 1

    # The audit trail, not just the destination.
    assert await transitions(session_factory) == [
        f"-->{State.QUEUED}",
        f"{State.QUEUED}->{State.SESSION_CREATED}",
        f"{State.SESSION_CREATED}->{State.RUNNING}",
        f"{State.RUNNING}->{State.PR_OPENED}",
        f"{State.PR_OPENED}->{State.CI_FAILED}",
        f"{State.CI_FAILED}->{State.RUNNING}",
        f"{State.RUNNING}->{State.CI_PASSED}",
        f"{State.CI_PASSED}->{State.IN_REVIEW}",
        f"{State.IN_REVIEW}->{State.MERGED}",
    ]

    # And the timestamps the funnel is measured from.
    assert final.session_created_at is not None
    assert final.pr_opened_at is not None
    assert final.ci_green_at is not None
    assert final.merged_at is not None


async def test_the_analytics_answer_for_the_remediation_that_just_merged(
    api: Any,
    context: Context,
    devin: DevinClient,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    delivery: DeliveryFactory,
    outside: respx.Route,
) -> None:
    """The dashboard's numbers, computed from the log the walkthrough above wrote.

    A separate test rather than more assertions on the first, because these are asserting a
    different thing: not that the pipeline moved, but that what it recorded is enough to answer the
    question an engineering leader asks.
    """
    await post(api, labelled(delivery, "a-1"))
    await drain(context)
    await tick(devin, session_factory, settings, devin_api, status="running")
    await tick(
        devin,
        session_factory,
        settings,
        devin_api,
        status="running",
        pull_requests=[{"url": PR_URL, "number": PR_NUMBER}],
    )
    await post(api, delivery("check_suite.completed.failure", delivery_id="a-2"))
    await drain(context)
    await post(api, delivery("check_suite.completed.success", delivery_id="a-3"))
    await post(api, delivery("pull_request.closed.merged", delivery_id="a-4"))

    summary = (await api.get("/api/analytics/summary")).json()

    assert summary["funnel"]["labelled"] == 1
    assert summary["funnel"]["pr_opened"] == 1
    assert summary["funnel"]["merged"] == 1
    # One lap of the fix loop: the remediation needed a correction but no human wrote it.
    assert summary["cycles"]["distribution"]["1"] == 1


async def test_a_redelivered_webhook_moves_nothing(
    api: Any,
    context: Context,
    session_factory: async_sessionmaker[AsyncSession],
    delivery: DeliveryFactory,
    outside: respx.Route,
) -> None:
    """GitHub redelivers on a timeout, and a redelivery must not open a second session.

    Asserted here rather than only in `test_webhooks.py` because deduplication is only worth
    anything if it holds with the worker running behind it: the first delivery's job has already
    been claimed and completed by the time the duplicate arrives.
    """
    first = labelled(delivery, "same")
    assert (await post(api, first)).status_code == 202
    assert await drain(context) == 1

    assert (await post(api, first)).status_code == 200, "a duplicate is not new work"
    assert await drain(context) == 0

    assert (await remediation(session_factory)).state == State.SESSION_CREATED
    assert await transitions(session_factory) == [
        f"-->{State.QUEUED}",
        f"{State.QUEUED}->{State.SESSION_CREATED}",
    ]


async def test_a_late_check_suite_does_not_disturb_a_merged_remediation(
    api: Any,
    context: Context,
    devin: DevinClient,
    devin_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    delivery: DeliveryFactory,
    outside: respx.Route,
) -> None:
    """A terminal state absorbs what arrives after it, rather than raising or moving.

    A check suite queued before the merge can be delivered after it, which is not an error on
    anyone's part — and re-engaging a merged remediation would spend a session on work that is
    already done.

    Absorbing it is not the same as discarding it: a `MERGED → MERGED` row is appended, so the
    delivery that arrived late is answerable from the log rather than invisible in it. What must
    not happen is a *move*, or a job.
    """
    await post(api, labelled(delivery, "l-1"))
    await drain(context)
    await tick(
        devin,
        session_factory,
        settings,
        devin_api,
        status="running",
        pull_requests=[{"url": PR_URL, "number": PR_NUMBER}],
    )
    await post(api, delivery("check_suite.completed.success", delivery_id="l-2"))
    await post(api, delivery("pull_request.closed.merged", delivery_id="l-3"))
    assert (await remediation(session_factory)).state == State.MERGED
    before = await transitions(session_factory)

    late = delivery("check_suite.completed.failure", delivery_id="l-4")
    assert (await post(api, late)).status_code == 202

    assert (await remediation(session_factory)).state == State.MERGED
    assert await transitions(session_factory) == [*before, f"{State.MERGED}->{State.MERGED}"]
    assert await drain(context) == 0, "and nothing was queued for it"


def test_the_walkthrough_covers_every_trigger_the_loop_uses() -> None:
    """The triggers the case above drives, named — so a new one is a visible gap rather than a
    silent one.

    `Trigger.BLOCKED`, `Trigger.FAILED` and `Trigger.CHECK_SUITE_REQUESTED` are deliberately not
    here: they are the escalation and the in-progress paths, covered where they are decided
    (`test_worker.py`, `test_webhooks.py`), and driving them here would make this case a survey
    instead of a walkthrough.
    """
    driven = {
        Trigger.ISSUE_LABELLED,
        Trigger.SESSION_CREATED,
        Trigger.SESSION_RUNNING,
        Trigger.PR_OPENED,
        Trigger.CHECK_SUITE_FAILED,
        Trigger.SESSION_RESUMED,
        Trigger.CHECK_SUITE_SUCCEEDED,
        Trigger.REVIEW_REQUESTED,
        Trigger.PR_MERGED,
    }
    assert driven <= set(Trigger)
