"""The end-to-end case of `docs/08-testing.md#orchestrator-tests`: one issue, one merged fix.

`label → create_session job → fake Devin session → PR opened → check suite fails → resume with
cycle:1 → check suite passes → review → merged`, and then the analytics response that reports it.

Only the two HTTP boundaries are faked. The webhook endpoint, the queue, the worker handlers, the
poller, the state machine and the analytics module are the real ones, over the real Postgres — so
what this file asserts is that the pieces the other test files check in isolation compose into the
pipeline `docs/06-event-pipeline.md` describes. Time is driven rather than waited for: the worker is
stepped with `run_once` and the poller with `poll_once`, so nothing sleeps and nothing races.

Three properties no single-layer test can establish:

- **The review-fix loop reuses the session.** `docs/04-state-machine.md#the-review-fix-loop` is the
  substance of the system: a failing check suite resumes the *existing* Devin session rather than
  creating a second one, and `cycle` becomes 1. Exactly one `POST …/sessions` is sent in the whole
  walkthrough, and the resume is addressed to the id that call returned.
- **The audit trail is the record.** `remediation_event` is append-only and invariant 4 puts exactly
  one row on every transition, so the *sequence* is asserted rather than only the state the
  remediation ended in. Ordered by `id`: the two rows a green check suite writes are one
  transaction, so they share a `created_at` to the microsecond and time cannot separate them.
- **Nothing else was called.** The `respx` router refuses any request it was not told about, so the
  fakes below are the complete list of what Sentinel does to Devin and to GitHub across a whole
  remediation — and there is no merge among them
  (`docs/adr/2026-08-07-humans-approve-every-merge.md`).
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import ClientFactory, Delivery, DeliveryFactory, FakeAPI
from factories import (
    ISSUE_CLASS,
    ISSUE_NUMBER,
    PR_NUMBER,
    REPO,
    a_consumption_body,
    github_payload,
)
from sentinel import db
from sentinel.api.main import create_app
from sentinel.config import Settings
from sentinel.devin.client import DevinClient
from sentinel.devin.playbooks import acu_cap_for, baseline_hours_for
from sentinel.github.client import GitHubClient
from sentinel.models import Job, Remediation, RemediationEvent
from sentinel.observability.prom import Metrics
from sentinel.pipeline import poller, worker
from sentinel.pipeline.handlers import Context
from sentinel.pipeline.state import State
from sentinel.queue import JobKind, JobStatus

WEBHOOK_URL = "/webhooks/github"
SUMMARY_URL = "/api/analytics/summary"

WORKER_ID = "worker-e2e"

# The organisation the suite is configured with, in `conftest.CONFIGURATION`. Spelled out rather
# than formatted from `settings` so that the paths below are readable as the paths a reviewer opens.
ORG = "org-abc123"
SESSIONS = f"/v3/organizations/{ORG}/sessions"
CONSUMPTION = f"/v3/organizations/{ORG}/consumption/daily"

SESSION_ID = "devin-4c8e1f92"
SESSION_URL = f"https://app.devin.ai/sessions/{SESSION_ID}"
SESSION = f"{SESSIONS}/{SESSION_ID}"
MESSAGES = f"{SESSION}/messages"
TAGS = f"{SESSION}/tags"

ISSUE = f"/repos/{REPO}/issues/{ISSUE_NUMBER}"
ACTIONS = f"/repos/{REPO}/actions"

# The commit and the pull request the recorded `check_suite` and `pull_request` payloads describe.
HEAD_SHA = "349a7f639dfb353669c001187706d7fd0112ed2f"
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"

# `pull_requests[]` as the live API sends it — `pr_url` and `pr_state`, and no number at all.
# `PR_NUMBER` reaches the remediation only by being parsed back out of `PR_URL`, and every
# `check_suite` and `pull_request_review` delivery in this file resolves by it.
DEVIN_PULL_REQUESTS = [{"pr_url": PR_URL, "pr_state": "open"}]

RUN_ID = 84410727864
WORKFLOW_PATH = ".github/workflows/devin-autofix-ci.yml"
GATE_NAME = "devin-autofix-ci"
CI_JOB_ID = 231855142299
CI_JOB_NAME = "pytest"
CI_LOG_TAIL = "E   AssertionError: expected ColumnNotFoundException, got SupersetGenericDBError"

# One delivery id per webhook, because `webhook_delivery.delivery_id` is UNIQUE and a repeat is
# answered `200 duplicate` without being handled at all.
LABELLED = "0b1c8e40-7d3a-11f1-9c2e-4f6a1d8b3e57"
CI_FAILED = "1c2d9f51-8e4b-22a2-ad3f-5a7b2e9c4f68"
CI_PASSED = "2d3ea062-9f5c-33b3-be40-6b8c3fad5a79"
TRIVIALLY_GREEN = "5a61d395-c28f-66e6-e173-9ebf6cd08dac"
CI_PENDING = "6b72e4a6-d390-77f7-f284-af0a7de19ebd"
APPROVED = "3e4fb173-a06d-44c4-cf51-7c9d4abe6b8a"
MERGED = "4f50c284-b17e-55d5-d062-8dae5bcf7c9b"

# What the session has spent by each observation. The second is what the live table shows, because
# the poller reconciles `acus_consumed` on every tick and the remediation merges from there.
ACUS_WHILE_WORKING = 2.0
ACUS_AT_PULL_REQUEST = 6.5

REPORT: dict[str, Any] = {
    "outcome": "fixed",
    "root_cause": (
        "adhoc_column_to_sqla wrapped every SQLAlchemy failure as ColumnNotFoundException, "
        "so a connection error was reported as a missing column."
    ),
    "changes": ["superset/connectors/sqla/models.py"],
    "tests": {
        "added": ["tests/unit_tests/connectors/sqla/test_models.py"],
        "command": "pytest tests/unit_tests/connectors/sqla/test_models.py",
        "passed": True,
    },
    "risk": "low",
    "pr_url": PR_URL,
}


# --- The three processes ---------------------------------------------------------------------


@pytest.fixture
async def devin(
    settings: Settings, devin_api: FakeAPI, metrics: Metrics
) -> AsyncIterator[DevinClient]:
    """The Devin client the worker and the poller share, with metrics in this test's registry."""
    async with DevinClient(settings, metrics=metrics) as client:
        yield client


@pytest.fixture
async def github(settings: Settings, github_api: FakeAPI) -> AsyncIterator[GitHubClient]:
    async with GitHubClient(settings) as client:
        yield client


@dataclass(frozen=True, slots=True)
class Pipeline:
    """The `api`, `worker` and `poller` processes of `docs/02-architecture.md`, driven a step at a
    time.

    They hold two engines between them — the API takes its session from the process-wide
    `db.session_scope()` and the other two from the fixture's factory — which is what the three
    deployed processes have, and is why every step below is visible to the next only because it was
    committed.
    """

    client: httpx.AsyncClient
    context: Context
    devin: DevinClient
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings

    async def deliver(self, signed: Delivery) -> dict[str, Any]:
        """One webhook delivery, sent as the bytes it was signed over. Returns the answer body."""
        response = await self.client.post(WEBHOOK_URL, content=signed.body, headers=signed.headers)
        assert response.status_code == 202, response.text
        body: dict[str, Any] = response.json()
        return body

    async def work(self) -> None:
        """One iteration of the worker loop, and an assertion that there was a job due."""
        assert await worker.run_once(self.context, claimed_by=WORKER_ID), "expected a due job"

    async def poll(self) -> poller.Tick:
        """One poller tick over every in-flight remediation."""
        return await poller.poll_once(self.devin, self.session_factory, settings=self.settings)


@pytest.fixture
async def pipeline(
    asgi_client: ClientFactory,
    process_engine: None,
    session_factory: async_sessionmaker[AsyncSession],
    devin: DevinClient,
    github: GitHubClient,
    settings: Settings,
    http_mock: respx.MockRouter,
) -> AsyncIterator[Pipeline]:
    """The whole application, in process. `http_mock` is what makes the fakes exhaustive."""
    client = await asgi_client(create_app(settings))
    yield Pipeline(
        client=client,
        context=Context(
            session_factory=session_factory, devin=devin, github=github, settings=settings
        ),
        devin=devin,
        session_factory=session_factory,
        settings=settings,
    )
    # Before the app's own lifespan shutdown, which runs after the fixtures that redirected the
    # process-wide engine at the test database have already been undone.
    await db.dispose_engine()


# --- What the outside world answers ------------------------------------------------------------


def a_session(**overrides: Any) -> httpx.Response:
    """One answer of `GET /v3/organizations/{org}/sessions/{id}`, as the poller reads it."""
    return httpx.Response(
        200,
        json={
            "session_id": SESSION_ID,
            "status": "running",
            "url": SESSION_URL,
            "acus_consumed": ACUS_WHILE_WORKING,
            **overrides,
        },
    )


def no_session_yet(devin_api: FakeAPI) -> None:
    """The adopt-or-create lookup `create_session` makes before it posts, finding nothing.

    Registered separately from the `POST`, so a run that skipped the lookup would fail here rather
    than create a session — `respx` answers no unregistered request.
    """
    devin_api.responds(
        "GET", SESSIONS, json={"items": [], "end_cursor": None, "has_next_page": False, "total": 0}
    )


def a_workflow_run() -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "path": WORKFLOW_PATH,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "failure",
        "run_started_at": "2026-08-07T15:11:55Z",
    }


def a_check_run(name: str, conclusion: str | None, status: str = "completed") -> dict[str, Any]:
    return {
        "id": abs(hash((name, conclusion, status))) % 10**10,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/runs/1",
        "started_at": "2026-08-07T15:12:04Z",
        "completed_at": None if status != "completed" else "2026-08-07T15:19:41Z",
    }


def the_head_sha_looks_like(github_api: FakeAPI, *runs: dict[str, Any]) -> None:
    """What `evaluate_ci` reads: the pull request, then every check run on its head.

    Both routes, because the SHA comes from the pull request rather than from the job payload — a
    payload SHA can be stale by the time the job is claimed.
    """
    github_api.responds("GET", f"/repos/{REPO}/pulls/{PR_NUMBER}", json=a_pull_request())
    github_api.responds(
        "GET",
        f"/repos/{REPO}/commits/{HEAD_SHA}/check-runs",
        json={"total_count": len(runs), "check_runs": list(runs)},
    )


def a_pull_request() -> dict[str, Any]:
    pull_request = github_payload("pull_request.opened")["pull_request"]
    return {**pull_request, "number": PR_NUMBER, "head": {**pull_request["head"], "sha": HEAD_SHA}}


# The inherited workflows of `taxpon/superset`, which conclude in seconds and say nothing about the
# diff. On the first live remediation the first of these was what Sentinel called CI green.
INHERITED_AND_GREEN = [
    a_check_run("check-hold-label", "success"),
    a_check_run("labeler", "success"),
    a_check_run("unit-tests-required", "success"),
]


# Our own workflow, all five jobs. `pytest (scoped)` skips on a frontend-only diff by design.
def our_workflow(gate: str) -> list[dict[str, Any]]:
    return [
        a_check_run("Resolve scope from the diff", "success"),
        a_check_run("pre-commit (changed files)", "success"),
        a_check_run("jest (scoped)", "success"),
        a_check_run("pytest (scoped)", "skipped"),
        a_check_run(GATE_NAME, gate),
    ]


def a_ci_job() -> dict[str, Any]:
    return {
        "id": CI_JOB_ID,
        "run_id": RUN_ID,
        "name": CI_JOB_NAME,
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2026-08-07T15:12:04Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/{CI_JOB_ID}",
    }


def classified(delivery: DeliveryFactory) -> Delivery:
    """The recorded `issues.labeled` delivery, with the `class:` label the fork carries.

    `docs/09-operations.md` puts the issue's class on a `class:<c>` label and the ingress reads the
    first one; the recorded payload predates the fork's labels being created, so an unmodified copy
    would be `unclassified` and `docs/04-state-machine.md` would route it `QUEUED -> BLOCKED`.
    """
    issue = github_payload("issues.labeled")["issue"]
    return delivery(
        "issues.labeled",
        delivery_id=LABELLED,
        issue={**issue, "labels": [*issue["labels"], {"name": f"class:{ISSUE_CLASS}"}]},
    )


def approved(delivery: DeliveryFactory) -> Delivery:
    """The recorded review, submitted as an approval rather than a request for changes.

    `docs/06-event-pipeline.md` records an approval and moves nothing: `IN_REVIEW` is already where
    a green check suite left the remediation, and the merge is what takes it further.
    """
    review = github_payload("pull_request_review.submitted.changes_requested")["review"]
    return delivery(
        "pull_request_review.submitted.changes_requested",
        delivery_id=APPROVED,
        review={**review, "state": "APPROVED"},
    )


# --- Reading the database back -----------------------------------------------------------------


async def the_remediation(factory: async_sessionmaker[AsyncSession]) -> Remediation:
    async with factory() as session:
        return (
            await session.scalars(select(Remediation).execution_options(populate_existing=True))
        ).one()


async def the_events(factory: async_sessionmaker[AsyncSession]) -> list[RemediationEvent]:
    """The append-only log, ordered by `id`.

    Not by `created_at`: it is `transaction_timestamp()`, so the `CI_PASSED` and `IN_REVIEW` rows
    one green check suite writes carry the same value and only the id orders them.
    """
    async with factory() as session:
        return list(await session.scalars(select(RemediationEvent).order_by(RemediationEvent.id)))


async def the_jobs(factory: async_sessionmaker[AsyncSession]) -> list[Job]:
    async with factory() as session:
        return list(await session.scalars(select(Job).order_by(Job.id)))


# --- The walkthrough ---------------------------------------------------------------------------


async def test_what_sentinel_sends_across_a_whole_remediation(
    pipeline: Pipeline,
    delivery: DeliveryFactory,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole of `docs/08-testing.md`'s end-to-end row, one step at a time."""

    # --- The label. The ingress writes the remediation and the job, and calls nothing. ---
    answer = await pipeline.deliver(classified(delivery))
    assert (answer["result"], answer["state"]) == ("enqueued", State.QUEUED)

    remediation = await the_remediation(session_factory)
    assert (remediation.issue_number, remediation.issue_class) == (ISSUE_NUMBER, ISSUE_CLASS)
    [create_job] = await the_jobs(session_factory)
    assert create_job.kind == JobKind.CREATE_SESSION
    assert create_job.payload["delivery_id"] == LABELLED

    # --- The worker creates the session: budget, issue, the adopt-or-create lookup, then
    # `POST /sessions`. ---
    devin_api.responds("GET", CONSUMPTION, json=a_consumption_body())
    github_api.responds("GET", ISSUE, json=github_payload("issues.labeled")["issue"])
    no_session_yet(devin_api)
    devin_api.responds(
        "POST", SESSIONS, 201, {"session_id": SESSION_ID, "status": "new", "url": SESSION_URL}
    )

    await pipeline.work()

    # The lookup asks for this remediation's three identity tags, and the creation sends them as
    # the first three of its five — so a session Devin had already made for this issue would have
    # been found by exactly the filter that was sent.
    assert devin_api.only("GET", SESSIONS).url.params.get_list("tags") == [
        "sentinel",
        f"repo:{REPO}",
        f"issue:{ISSUE_NUMBER}",
    ]
    created = devin_api.only("POST", SESSIONS).json
    assert created["tags"] == [
        "sentinel",
        f"repo:{REPO}",
        f"issue:{ISSUE_NUMBER}",
        f"class:{ISSUE_CLASS}",
        f"run:{LABELLED}",
    ]
    assert created["max_acu_limit"] == acu_cap_for(ISSUE_CLASS)
    assert created["resumable"] is True, "the review-fix loop cannot resume a session without it"

    remediation = await the_remediation(session_factory)
    assert remediation.state == State.SESSION_CREATED
    assert remediation.devin_session_id == SESSION_ID

    # --- The poller. Devin is working on the first tick and has opened a pull request on the
    # second, which is the only place the link can come from: `pull_request.opened` carries no key
    # that would find this remediation and is dropped. ---
    devin_api.route("GET", SESSION).mock(
        side_effect=[
            a_session(),
            a_session(
                acus_consumed=ACUS_AT_PULL_REQUEST,
                pull_requests=DEVIN_PULL_REQUESTS,
                structured_output=REPORT,
            ),
        ]
    )

    assert (await pipeline.poll()).moved == 1
    assert (await the_remediation(session_factory)).state == State.RUNNING

    assert (await pipeline.poll()).moved == 1
    remediation = await the_remediation(session_factory)
    assert remediation.state == State.PR_OPENED
    assert (remediation.pr_number, remediation.pr_url) == (PR_NUMBER, PR_URL)
    assert remediation.pr_opened_at is not None
    assert remediation.structured_output is not None
    assert remediation.structured_output["outcome"] == "fixed"

    # --- A trivial inherited workflow concludes first, and it is not CI. ---
    #
    # This is the defect the aggregate replaced, in the shape it occurred: on PR #9 the first suite
    # to conclude was a workflow checking for a `hold` label, thirteen seconds after the pull
    # request opened, and Sentinel read its `success` as CI green — `CI_PASSED`, then `IN_REVIEW`,
    # with the scoped suite that judges the diff still three minutes from finishing.
    the_head_sha_looks_like(github_api, *INHERITED_AND_GREEN)
    answer = await pipeline.deliver(
        delivery("check_suite.completed.success", delivery_id=TRIVIALLY_GREEN)
    )
    assert (answer["result"], answer["state"]) == ("enqueued", State.PR_OPENED)

    await pipeline.work()
    remediation = await the_remediation(session_factory)
    assert remediation.state == State.CI_RUNNING
    assert remediation.ci_green_at is None

    # --- CI fails on the head commit. The suite finds the remediation through `pr_number`. ---
    the_head_sha_looks_like(github_api, *INHERITED_AND_GREEN, *our_workflow(gate="failure"))
    answer = await pipeline.deliver(
        delivery("check_suite.completed.failure", delivery_id=CI_FAILED)
    )
    assert (answer["result"], answer["state"]) == ("enqueued", State.CI_RUNNING)

    await pipeline.work()
    assert (await the_remediation(session_factory)).state == State.CI_FAILED

    [*_, resume_job] = await the_jobs(session_factory)
    assert resume_job.kind == JobKind.RESUME_SESSION
    assert resume_job.payload["head_sha"] == HEAD_SHA

    # --- The loop edge: the *same* session is told what failed, and tagged with the lap. ---
    github_api.responds(
        "GET", f"{ACTIONS}/runs", json={"total_count": 1, "workflow_runs": [a_workflow_run()]}
    )
    github_api.responds(
        "GET", f"{ACTIONS}/runs/{RUN_ID}/jobs", json={"total_count": 1, "jobs": [a_ci_job()]}
    )
    github_api.responds("GET", f"{ACTIONS}/jobs/{CI_JOB_ID}/logs", text=CI_LOG_TAIL)
    devin_api.responds("POST", MESSAGES, 202)
    devin_api.responds("POST", TAGS, 202)

    await pipeline.work()

    # `MESSAGES` and `TAGS` are the routes of `SESSION_ID` and the router answers no others, so
    # reaching them at all is the assertion that the lap was addressed to the session the first
    # call created — and `only` says the second lap did not create a second one.
    devin_api.only("POST", SESSIONS)
    message = devin_api.only("POST", MESSAGES).json["message"]
    assert f"CI failed on {HEAD_SHA}" in message
    assert f"Failing job: {CI_JOB_NAME}" in message
    assert CI_LOG_TAIL in message
    assert "This is fix cycle 1 of 3" in message
    assert devin_api.only("POST", TAGS).json == {"tags": ["cycle:1"]}

    remediation = await the_remediation(session_factory)
    assert remediation.state == State.RUNNING
    assert remediation.cycle == 1

    # --- CI passes on the fix, but one inherited check is still running. Not green yet. ---
    the_head_sha_looks_like(
        github_api,
        *INHERITED_AND_GREEN,
        *our_workflow(gate="success"),
        a_check_run("cypress-matrix (chrome)", None, status="in_progress"),
    )
    await pipeline.deliver(delivery("check_suite.completed.success", delivery_id=CI_PENDING))
    await pipeline.work()

    # `CI_RUNNING`, not `CI_PASSED`: the gate has passed but the SHA is still owed a check, and
    # `CI_RUNNING` is what "the pull request has checks outstanding" now means.
    remediation = await the_remediation(session_factory)
    assert remediation.state == State.CI_RUNNING
    assert remediation.ci_green_at is None

    # --- The last check finishes. Entering `CI_PASSED` fires `REVIEW_REQUESTED` itself, so one
    # evaluation writes two transitions. ---
    the_head_sha_looks_like(
        github_api,
        *INHERITED_AND_GREEN,
        *our_workflow(gate="success"),
        a_check_run("cypress-matrix (chrome)", "success"),
    )
    answer = await pipeline.deliver(
        delivery("check_suite.completed.success", delivery_id=CI_PASSED)
    )
    assert (answer["result"], answer["state"]) == ("enqueued", State.CI_RUNNING)

    await pipeline.work()
    remediation = await the_remediation(session_factory)
    assert remediation.state == State.IN_REVIEW
    assert remediation.ci_green_at is not None

    # --- A human approves. Recorded, and nothing moves. ---
    answer = await pipeline.deliver(approved(delivery))
    assert (answer["result"], answer["state"]) == ("ignored", State.IN_REVIEW)

    # --- The merge. Terminal, and successful. ---
    answer = await pipeline.deliver(delivery("pull_request.closed.merged", delivery_id=MERGED))
    assert (answer["result"], answer["state"]) == ("ignored", State.MERGED)

    remediation = await the_remediation(session_factory)
    assert remediation.merged_at is not None
    assert remediation.blocked_reason is None

    # --- The audit trail: one row per transition, in order. ---
    events = await the_events(session_factory)
    assert [
        (event.from_state, event.to_state, (event.detail or {})["source"]) for event in events
    ] == [
        (None, State.QUEUED, "webhook"),
        (State.QUEUED, State.SESSION_CREATED, "worker"),
        (State.SESSION_CREATED, State.RUNNING, "poller"),
        (State.RUNNING, State.PR_OPENED, "poller"),
        # Every CI transition is now the worker's: the ingress reaches no verdict, so it writes no
        # row. The five deliveries that asked for an evaluation are in `webhook_delivery`, and the
        # four that changed nothing appear here not at all.
        (State.PR_OPENED, State.CI_RUNNING, "worker"),
        (State.CI_RUNNING, State.CI_FAILED, "worker"),
        (State.CI_FAILED, State.RUNNING, "worker"),
        (State.RUNNING, State.CI_RUNNING, "worker"),
        (State.CI_RUNNING, State.CI_PASSED, "worker"),
        (State.CI_PASSED, State.IN_REVIEW, "worker"),
        (State.IN_REVIEW, State.MERGED, "webhook"),
    ]
    # The delivery is the correlation id end to end, so a transition a webhook caused names the
    # delivery that caused it and one the worker or the poller caused names none.
    assert [event.webhook_delivery_id is not None for event in events] == [
        (event.detail or {})["source"] == "webhook" for event in events
    ]
    # The two rows of one transaction: `created_at` cannot order them, which is why `id` does.
    green, review = events[-3], events[-2]
    assert (green.to_state, review.to_state) == (State.CI_PASSED, State.IN_REVIEW)
    assert green.created_at == review.created_at

    resumed = next(event for event in events if event.from_state == State.CI_FAILED)
    assert (resumed.detail or {})["cycle"] == 1

    # --- Nothing was escalated, and nothing is still owed. ---
    assert [(job.kind, job.status) for job in await the_jobs(session_factory)] == [
        (JobKind.CREATE_SESSION, JobStatus.DONE),
        (JobKind.EVALUATE_CI, JobStatus.DONE),
        (JobKind.EVALUATE_CI, JobStatus.DONE),
        (JobKind.RESUME_SESSION, JobStatus.DONE),
        (JobKind.EVALUATE_CI, JobStatus.DONE),
        (JobKind.EVALUATE_CI, JobStatus.DONE),
    ]

    # --- The dashboard's own account of it. ---
    response = await pipeline.client.get(SUMMARY_URL)
    assert response.status_code == 200
    summary = response.json()

    assert summary["funnel"] == {
        "labelled": 1,
        "session_created": 1,
        "pr_opened": 1,
        "ci_green": 1,
        "merged": 1,
    }
    # Autonomy is a merge nobody touched, and this one took a fix cycle — so the rate the whole
    # walkthrough produces is 0, and that is the figure being asserted rather than an absence.
    assert summary["rates"] == {"success": 1.0, "merge": 1.0, "autonomy": 0.0}
    assert summary["cycles"] == {"mean": 1.0, "distribution": {"1": 1}}
    assert summary["failures"] == []
    assert summary["impact"]["hours_saved"] == baseline_hours_for(ISSUE_CLASS)
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    assert summary["throughput"] == [{"day": today, "by_class": {ISSUE_CLASS: 1}}]

    # The durations are wall-clock over a walkthrough that took no time, so what is asserted is that
    # each was computed from timestamps in the right order rather than what it came to.
    durations = summary["durations_seconds"]
    assert set(durations) == {"to_pr", "to_merge", "review_latency"}
    for name, percentiles in durations.items():
        assert 0 <= percentiles["p50"] <= percentiles["p90"] < 60, name

    # The live table's own row. `acus_consumed` is asserted here rather than through the summary,
    # which no longer reports spend: it is the one figure in the walkthrough that Devin supplied,
    # and the poller reconciling it on every tick is what carries the pull-request figure over the
    # working one.
    rows = (await pipeline.client.get("/api/remediations")).json()
    assert [row["acus_consumed"] for row in rows] == [ACUS_AT_PULL_REQUEST]


# --- What arrives when it should not ------------------------------------------------------------


async def a_labelled_remediation(
    pipeline: Pipeline, delivery: DeliveryFactory, devin_api: FakeAPI, github_api: FakeAPI
) -> None:
    """The first two steps of the walkthrough above: labelled, and a session opened for it."""
    await pipeline.deliver(classified(delivery))
    devin_api.responds("GET", CONSUMPTION, json=a_consumption_body())
    github_api.responds("GET", ISSUE, json=github_payload("issues.labeled")["issue"])
    no_session_yet(devin_api)
    creations = devin_api.responds(
        "POST", SESSIONS, 201, {"session_id": SESSION_ID, "status": "new", "url": SESSION_URL}
    )
    await pipeline.work()
    assert creations.call_count == 1


async def test_a_redelivered_webhook_opens_no_second_session(
    pipeline: Pipeline,
    delivery: DeliveryFactory,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GitHub redelivers on a timeout, and the second copy must cost nothing.

    Asserted with the worker having already run, rather than on the ingress alone: the first
    delivery's job is `DONE` by the time the duplicate lands, so a dedup that only looked for
    *pending* work would let this one through.
    """
    await a_labelled_remediation(pipeline, delivery, devin_api, github_api)
    before = await the_events(session_factory)

    repeat = classified(delivery)
    response = await pipeline.client.post(WEBHOOK_URL, content=repeat.body, headers=repeat.headers)
    assert response.status_code == 200, "a duplicate is not new work"
    assert response.json()["result"] == "duplicate"

    assert await worker.run_once(pipeline.context, claimed_by=WORKER_ID) is False
    assert len(await the_events(session_factory)) == len(before)
    assert (await the_remediation(session_factory)).state == State.SESSION_CREATED


async def test_a_check_suite_that_arrives_after_the_merge_is_absorbed(
    pipeline: Pipeline,
    delivery: DeliveryFactory,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A suite queued before the merge can be delivered after it. Nobody is at fault, and
    re-engaging a merged remediation would spend a session on work that is already done.

    Nothing at all is written for it. While a conclusion was a verdict, this appended a
    `MERGED -> MERGED` row; now the ingress reaches no verdict and there is no transition to
    absorb, so the delivery's own row in `webhook_delivery` is where a late suite is visible. Seven
    suites were still concluding on PR #9 nine minutes after review was requested, and each would
    otherwise buy two GitHub calls to be told the remediation is over.
    """
    await a_labelled_remediation(pipeline, delivery, devin_api, github_api)
    devin_api.route("GET", SESSION).mock(return_value=a_session(pull_requests=DEVIN_PULL_REQUESTS))
    await pipeline.poll()
    the_head_sha_looks_like(github_api, *INHERITED_AND_GREEN, *our_workflow(gate="success"))
    await pipeline.deliver(delivery("check_suite.completed.success", delivery_id=CI_PASSED))
    await pipeline.work()
    await pipeline.deliver(delivery("pull_request.closed.merged", delivery_id=MERGED))
    assert (await the_remediation(session_factory)).state == State.MERGED
    before = [(event.from_state, event.to_state) for event in await the_events(session_factory)]

    late = delivery("check_suite.completed.failure", delivery_id=CI_FAILED)
    assert (await pipeline.deliver(late))["result"] == "ignored"

    assert (await the_remediation(session_factory)).state == State.MERGED
    assert [
        (event.from_state, event.to_state) for event in await the_events(session_factory)
    ] == before
    assert await worker.run_once(pipeline.context, claimed_by=WORKER_ID) is False


async def test_a_question_after_the_pull_request_carries_on_to_review(
    pipeline: Pipeline,
    delivery: DeliveryFactory,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Issue #5's re-run, from the label to review, with the offer Devin actually made.

    Devin opened its pull request, reported that the issue's premise was partly wrong and that it
    had fixed both causes, and then asked whether the user would like it to run the app end to end
    as an optional extra. That question set `status_detail: waiting_for_user`, and the poller read
    it as a stall: `PR_OPENED -> BLOCKED` 41 seconds later, on `cycle: 0`, with no CI failure and no
    review. `BLOCKED` is terminal, so the remediation parked there — and every remediation in the
    run would have, because Devin offers something at the end of every session.

    The sequence asserted here is that one, and it ends in `IN_REVIEW`. The third tick is where the
    whole change lives: it observes the question, moves nothing, and lets the check suites and the
    reviewer carry the remediation the rest of the way.
    """
    await a_labelled_remediation(pipeline, delivery, devin_api, github_api)

    offering_more = a_session(
        status="running",
        status_detail="waiting_for_user",
        acus_consumed=ACUS_AT_PULL_REQUEST,
        pull_requests=DEVIN_PULL_REQUESTS,
        structured_output=REPORT,
    )
    devin_api.route("GET", SESSION).mock(
        side_effect=[
            a_session(),
            a_session(acus_consumed=ACUS_AT_PULL_REQUEST, pull_requests=DEVIN_PULL_REQUESTS),
            offering_more,
            offering_more,
        ]
    )

    assert (await pipeline.poll()).moved == 1
    assert (await pipeline.poll()).moved == 1
    assert (await the_remediation(session_factory)).state == State.PR_OPENED

    # The tick that used to end the remediation. It escalates nothing and enqueues nothing, and a
    # second one of the same observation adds no further row.
    assert (await pipeline.poll()).moved == 0
    assert (await pipeline.poll()).moved == 0

    remediation = await the_remediation(session_factory)
    assert remediation.state == State.PR_OPENED
    assert remediation.blocked_reason is None
    assert remediation.closed_at is None

    # --- CI is green on the head, and the remediation goes to review as it always should have. ---
    the_head_sha_looks_like(github_api, *INHERITED_AND_GREEN, *our_workflow(gate="success"))
    await pipeline.deliver(delivery("check_suite.completed.success", delivery_id=CI_PASSED))
    await pipeline.work()

    remediation = await the_remediation(session_factory)
    assert remediation.state == State.IN_REVIEW
    assert remediation.ci_green_at is not None

    # The question survives as an observation between the two transitions, once, rather than as the
    # escalation it used to be or as nothing at all.
    assert [
        (event.from_state, event.to_state, event.kind)
        for event in await the_events(session_factory)
    ] == [
        (None, State.QUEUED, "transition"),
        (State.QUEUED, State.SESSION_CREATED, "transition"),
        (State.SESSION_CREATED, State.RUNNING, "transition"),
        (State.RUNNING, State.PR_OPENED, "transition"),
        (State.PR_OPENED, State.PR_OPENED, poller.OBSERVATION),
        (State.PR_OPENED, State.CI_PASSED, "transition"),
        (State.CI_PASSED, State.IN_REVIEW, "transition"),
    ]

    # Nothing was escalated, and the autonomy this remediation would count towards is intact: no
    # fix cycle, and no human message answering a question nobody was there to answer.
    assert [job.kind for job in await the_jobs(session_factory)] == [
        JobKind.CREATE_SESSION,
        JobKind.EVALUATE_CI,
    ]
    assert (remediation.cycle, remediation.human_message_count) == (0, 0)
