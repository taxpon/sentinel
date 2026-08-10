"""`sentinel.api.webhooks` — the ingress path of `docs/06-event-pipeline.md#ingress-path`.

Every test calls the router in this process through `httpx.ASGITransport`, against the real
Postgres, through the real `db.session_scope()`, with the recorded deliveries in
`tests/fixtures/github/`. What is under test is the boundary: the status it chooses, the rows it
writes, and the work it enqueues.

Three properties are asserted structurally rather than case by case, because they are what the
endpoint exists to guarantee:

- **Nothing external is called.** `http_mock` refuses any request it was not told about, so a call
  to Devin or GitHub from the request path fails the test that provoked it rather than the one that
  happened to look.
- **Both deduplication layers hold under concurrency.** They are database constraints, so the tests
  that matter run two requests on two connections at once — a check that only ever sees one caller
  proves nothing about the layer it is standing in for.
- **`transition()` is never reached without a trigger.** `trigger_for` returns `None` for a
  re-labelled issue and for a delivery about a remediation nobody created, and applying the trigger
  anyway raises. Both come back `202`, and the assertion is that no `remediation_event` was written.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from conftest import WEBHOOK_SECRET, ClientFactory, Delivery, DeliveryFactory, sign
from factories import ISSUE_NUMBER, PR_NUMBER, REPO, a_remediation, github_payload
from sentinel import db
from sentinel.api import webhooks
from sentinel.config import Settings, get_settings
from sentinel.models import Job, Remediation, RemediationEvent, WebhookDelivery
from sentinel.queue import JobKind
from sentinel.security.hmac import MAX_BODY_BYTES, SIGNATURE_HEADER

URL = "/webhooks/github"

PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"

SECOND_DELIVERY = "1c2d9f51-8e4b-22a2-ad3f-5a7b2e9c4f68"
THIRD_DELIVERY = "2d3ea062-9f5c-33b3-be40-6b8c3fad5a79"


# ----------------------------------------------------------------------------------- the client


@pytest.fixture
async def client(
    asgi_client: ClientFactory,
    settings: Settings,
    process_engine: None,
    http_mock: respx.MockRouter,
) -> Any:
    """The router mounted on an app of its own, over the test database.

    `process_engine` points `db.session_scope()` — which the handler uses unaltered — at the test
    database, so a request takes a connection of its own and sees only what a test has committed.
    `http_mock` is present on every test rather than on the ones that care: it is what makes "no
    external call on the request path" an assertion the whole file makes at once.
    """
    app = FastAPI()
    app.include_router(webhooks.router)
    app.dependency_overrides[get_settings] = lambda: settings
    yield await asgi_client(app)
    await db.dispose_engine()


async def post(client: httpx.AsyncClient, signed: Delivery, **kwargs: Any) -> httpx.Response:
    """Deliver `signed` exactly as it was signed: the same bytes, and the headers GitHub sends."""
    return await client.post(URL, content=signed.body, headers=signed.headers, **kwargs)


# ------------------------------------------------------------------------------- reading it back


async def rows(session: AsyncSession, model: Any) -> list[Any]:
    """Every row of `model`, oldest first, read fresh.

    `populate_existing` is what makes it fresh: `expire_on_commit=False` keeps rows loaded across a
    commit, so a test that seeded a remediation and then made a request on another connection would
    otherwise be handed the instance it seeded rather than what the handler wrote to it.
    """
    statement = select(model).order_by(model.id).execution_options(populate_existing=True)
    return list(await session.scalars(statement))


async def one(session: AsyncSession, model: Any) -> Any:
    """The single row of `model` — and an assertion that there is exactly one of it."""
    found = await rows(session, model)
    assert len(found) == 1, f"expected exactly one {model.__name__}, got {len(found)}"
    return found[0]


async def count(session: AsyncSession, model: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def seed(session: AsyncSession, **overrides: Any) -> Remediation:
    """One remediation, committed, so that a request on another connection can see it."""
    remediation = a_remediation(**overrides)
    session.add(remediation)
    await session.commit()
    return remediation


def linked(**overrides: Any) -> dict[str, Any]:
    """A remediation with a pull request linked — what every CI and review trigger requires."""
    return {"pr_number": PR_NUMBER, "pr_url": PR_URL, **overrides}


# ------------------------------------------------------------------------- signature verification


async def test_a_valid_signature_is_accepted(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    response = await post(client, delivery("issues.labeled"))

    assert response.status_code == 202
    assert await count(session, WebhookDelivery) == 1


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({}, "missing_header", id="missing"),
        pytest.param({SIGNATURE_HEADER: "sha256=nonsense"}, "malformed_header", id="malformed"),
        pytest.param({SIGNATURE_HEADER: f"sha256={'a' * 64}"}, "mismatch", id="mismatch"),
    ],
)
async def test_a_signature_that_does_not_verify_is_401_and_writes_nothing(
    client: httpx.AsyncClient,
    delivery: DeliveryFactory,
    session: AsyncSession,
    headers: dict[str, str],
    expected: str,
) -> None:
    signed = delivery("issues.labeled")
    sent = {key: value for key, value in signed.headers.items() if key != SIGNATURE_HEADER}

    response = await client.post(URL, content=signed.body, headers={**sent, **headers})

    assert response.status_code == 401
    assert response.json()["detail"] == expected
    assert await count(session, WebhookDelivery) == 0
    assert await count(session, Remediation) == 0


async def test_a_body_signed_by_github_and_re_encoded_by_a_proxy_is_401(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """The realistic 401: the payload is genuine and the bytes are not the ones that were signed.

    Verification runs on the raw body precisely so that this fails — anything that pretty-prints or
    re-serialises the JSON between GitHub and here changes the digest.
    """
    signed = delivery("issues.labeled")
    re_encoded = json.dumps(signed.payload, indent=2).encode()
    assert re_encoded != signed.body

    response = await client.post(URL, content=re_encoded, headers=signed.headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "mismatch"
    assert await count(session, WebhookDelivery) == 0


async def test_a_body_above_the_limit_is_413_before_it_is_hashed(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    signed = delivery("issues.labeled")
    oversized = b"x" * (MAX_BODY_BYTES + 1)

    response = await client.post(
        URL,
        content=oversized,
        headers={**signed.headers, SIGNATURE_HEADER: sign(oversized, WEBHOOK_SECRET)},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "body_too_large"
    assert await count(session, WebhookDelivery) == 0


async def test_the_wrong_secret_is_401(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    response = await post(client, delivery("issues.labeled", secret="whsec_not_ours"))

    assert response.status_code == 401
    assert await count(session, WebhookDelivery) == 0


# ----------------------------------------------------------------------- what a delivery must be


@pytest.mark.parametrize("missing", ["X-GitHub-Delivery", "X-GitHub-Event"])
async def test_a_delivery_without_its_identifying_headers_is_400(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession, missing: str
) -> None:
    signed = delivery("issues.labeled")
    sent = {key: value for key, value in signed.headers.items() if key != missing}

    response = await client.post(URL, content=signed.body, headers=sent)

    assert response.status_code == 400
    assert await count(session, WebhookDelivery) == 0


async def test_a_body_that_is_not_json_is_400(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """`webhook_delivery.payload` is `jsonb NOT NULL` and holds the body verbatim, so there is
    nowhere to record one that is not JSON at all."""
    signed = delivery("issues.labeled")
    body = b"not json"

    response = await client.post(
        URL,
        content=body,
        headers={**signed.headers, SIGNATURE_HEADER: sign(body, WEBHOOK_SECRET)},
    )

    assert response.status_code == 400
    assert await count(session, WebhookDelivery) == 0


async def test_a_json_body_that_is_not_an_object_is_recorded_and_ignored(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """A well-formed body can decode to a list as easily as to an object, and `jsonb` holds it."""
    signed = delivery("issues.labeled")
    body = b"[1, 2, 3]"

    response = await client.post(
        URL,
        content=body,
        headers={**signed.headers, SIGNATURE_HEADER: sign(body, WEBHOOK_SECRET)},
    )

    assert response.status_code == 202
    assert response.json()["reason"] == "malformed_body"
    stored = await one(session, WebhookDelivery)
    assert stored.payload == [1, 2, 3]
    assert stored.handler_result == "ignored"
    assert stored.action is None


# ------------------------------------------------------------------------ delivery deduplication


async def test_the_same_delivery_twice_writes_one_row_and_one_job(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """`docs/08-testing.md`: one row, one job, `200` on the second.

    An error status on the second would make GitHub retry it for ever, which is the failure the
    `duplicate` body exists to avoid.
    """
    signed = delivery("issues.labeled")

    first = await post(client, signed)
    second = await post(client, signed)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["result"] == "duplicate"
    assert await count(session, WebhookDelivery) == 1
    assert await count(session, Remediation) == 1
    assert await count(session, Job) == 1
    assert await count(session, RemediationEvent) == 1


async def test_concurrent_identical_deliveries_write_one_row_and_one_job(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """The delivery layer is a UNIQUE constraint, so it is proved against two connections at once.

    `ON CONFLICT DO NOTHING` makes the second request wait on the first transaction rather than
    reading an absence that was true a moment ago — the window a read-then-write would have.
    """
    signed = delivery("issues.labeled")

    responses = await asyncio.gather(post(client, signed), post(client, signed))

    assert sorted(response.status_code for response in responses) == [200, 202]
    assert await count(session, WebhookDelivery) == 1
    assert await count(session, Remediation) == 1
    assert await count(session, Job) == 1


# -------------------------------------------------------------------------- domain deduplication


async def test_the_label_added_twice_creates_one_remediation(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """Two *different* deliveries about one issue: the label removed and re-added.

    `Trigger.ISSUE_LABELLED` is legal only from `None`, so the second delivery must not reach
    `transition()` at all — hence one `remediation_event`, not two, and no second session.
    """
    first = await post(client, delivery("issues.labeled"))
    second = await post(client, delivery("issues.labeled", delivery_id=SECOND_DELIVERY))

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["result"] == "enqueued"
    assert second.json()["result"] == "ignored"
    assert await count(session, WebhookDelivery) == 2
    assert await count(session, Remediation) == 1
    assert await count(session, Job) == 1
    assert await count(session, RemediationEvent) == 1


async def test_label_and_comment_on_the_same_issue_create_one_remediation(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """Two different events about one issue produce one remediation and one `create_session`."""
    await post(client, delivery("issues.labeled"))
    await post(client, delivery("issue_comment.created", delivery_id=SECOND_DELIVERY))

    remediation = await one(session, Remediation)
    assert remediation.issue_number == ISSUE_NUMBER
    assert remediation.state == "QUEUED"
    # The comment is forwarded and counted as human intervention, which is what makes the autonomy
    # rate in `docs/07-observability.md` mean anything.
    assert remediation.human_message_count == 1
    kinds = [job.kind for job in await rows(session, Job)]
    assert kinds == [JobKind.CREATE_SESSION, JobKind.RESUME_SESSION]


async def test_concurrent_deliveries_about_one_issue_create_one_remediation(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """The domain layer is `UNIQUE (repo, issue_number)`, proved on two connections at once.

    Two distinct deliveries race, so the delivery layer cannot absorb either of them: exactly one
    inserts the remediation, and only that one enqueues a session.
    """
    responses = await asyncio.gather(
        post(client, delivery("issues.labeled")),
        post(client, delivery("issues.labeled", delivery_id=SECOND_DELIVERY)),
    )

    assert [response.status_code for response in responses] == [202, 202]
    assert sorted(response.json()["result"] for response in responses) == ["enqueued", "ignored"]
    assert await count(session, WebhookDelivery) == 2
    assert await count(session, Remediation) == 1
    assert await count(session, Job) == 1
    assert await count(session, RemediationEvent) == 1


# --------------------------------------------------------------------------- the subscribed table


@dataclass(frozen=True)
class Case:
    """One row of the subscribed-events table in `docs/06-event-pipeline.md`.

    `seed` is the remediation the delivery is about, or `None` where the delivery is the one that
    creates it. `state` is where the remediation is left; `jobs` is what a worker is asked to do.
    """

    name: str
    fixture: str
    expected_state: str | None
    seed: dict[str, Any] | None = None
    event: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    result: str = "enqueued"
    reason: str | None = None
    jobs: tuple[JobKind, ...] = ()
    events: int = 1


def unmerged() -> dict[str, Any]:
    """`pull_request.closed` with `merged: false` — the abandoned pull request of the spec table."""
    pull_request = github_payload("pull_request.closed.merged")["pull_request"]
    return {"pull_request": {**pull_request, "merged": False}}


def approved() -> dict[str, Any]:
    review = github_payload("pull_request_review.submitted.changes_requested")["review"]
    return {"review": {**review, "state": "APPROVED"}}


CASES: tuple[Case, ...] = (
    Case(
        "issues.labeled starts a remediation",
        "issues.labeled",
        "QUEUED",
        jobs=(JobKind.CREATE_SESSION,),
    ),
    Case(
        "issues.unlabeled cancels",
        "issues.labeled",
        "FAILED",
        seed={"state": "RUNNING"},
        overrides={"action": "unlabeled"},
        result="ignored",
        reason="autofix_label_removed",
    ),
    Case(
        "issues.closed cancels",
        "issues.labeled",
        "FAILED",
        seed={"state": "RUNNING"},
        overrides={"action": "closed"},
        result="ignored",
        reason="issue_closed",
    ),
    Case(
        "pull_request.opened is left to the poller",
        "pull_request.opened",
        None,
        result="ignored",
        reason="linked_by_the_poller",
        events=0,
    ),
    Case(
        "pull_request.closed merged",
        "pull_request.closed.merged",
        "MERGED",
        seed=linked(state="IN_REVIEW"),
        result="ignored",
    ),
    Case(
        "pull_request.closed unmerged is abandoned",
        "pull_request.closed.merged",
        "FAILED",
        seed=linked(state="RUNNING"),
        overrides=unmerged(),
        reason="pull_request_closed_unmerged",
        jobs=(JobKind.ESCALATE,),
    ),
    Case(
        "pull_request_review changes_requested resumes",
        "pull_request_review.submitted.changes_requested",
        "CHANGES_REQUESTED",
        seed=linked(state="IN_REVIEW"),
        jobs=(JobKind.RESUME_SESSION,),
    ),
    Case(
        "pull_request_review approved records only",
        "pull_request_review.submitted.changes_requested",
        "IN_REVIEW",
        seed=linked(state="IN_REVIEW"),
        overrides=approved(),
        result="ignored",
        events=0,
    ),
    Case(
        "check_suite.requested starts CI",
        "check_suite.completed.success",
        "CI_RUNNING",
        seed=linked(state="PR_OPENED"),
        overrides={"action": "requested"},
        result="ignored",
    ),
    # Both conclusions do the same thing here, and it is not a transition. The conclusion belongs
    # to one of the fork's 46 workflows, so the ingress hands the question to `evaluate_ci` and the
    # state is left where it was until a worker has read the whole head SHA.
    Case(
        "check_suite success asks for an evaluation",
        "check_suite.completed.success",
        "CI_RUNNING",
        seed=linked(state="CI_RUNNING"),
        jobs=(JobKind.EVALUATE_CI,),
        events=0,
    ),
    Case(
        "check_suite failure asks for an evaluation too",
        "check_suite.completed.failure",
        "CI_RUNNING",
        seed=linked(state="CI_RUNNING"),
        jobs=(JobKind.EVALUATE_CI,),
        events=0,
    ),
    Case(
        "issue_comment forwards to the session",
        "issue_comment.created",
        "RUNNING",
        seed={"state": "RUNNING"},
        jobs=(JobKind.RESUME_SESSION,),
        events=0,
    ),
    Case(
        "ping is acknowledged",
        "issues.labeled",
        None,
        event="ping",
        result="ignored",
        reason="ping",
        events=0,
    ),
    Case(
        "an unknown event is recorded",
        "issues.labeled",
        None,
        event="deployment_status",
        result="ignored",
        reason="unknown_event",
        events=0,
    ),
    Case(
        "an event from another repository is dropped",
        "issues.labeled",
        None,
        overrides={"repository": {"full_name": "someone/else"}},
        result="ignored",
        reason="other_repository",
        events=0,
    ),
    Case(
        "another label on the issue is not the autofix label",
        "issues.labeled",
        None,
        overrides={"label": {"name": "documentation"}},
        result="ignored",
        reason="other_label",
        events=0,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_each_subscribed_event_reaches_its_state_and_its_work(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession, case: Case
) -> None:
    """The subscribed-events table of `docs/06-event-pipeline.md`, row by row.

    Every row is answered promptly and none of them is a `5xx`, including the ones the mapping
    cannot classify: GitHub redelivers what it thinks failed.
    """
    if case.seed is not None:
        await seed(session, **case.seed)

    response = await post(client, delivery(case.fixture, event=case.event, **case.overrides))

    assert response.status_code == 202
    body = response.json()
    assert body["result"] == case.result
    assert body["reason"] == case.reason
    assert body["state"] == case.expected_state

    stored = await one(session, WebhookDelivery)
    assert stored.handler_result == case.result
    assert stored.processed_at is not None

    if case.expected_state is None:
        assert await count(session, Remediation) == (1 if case.seed else 0)
    else:
        remediation = await one(session, Remediation)
        assert remediation.state == case.expected_state

    assert [job.kind for job in await rows(session, Job)] == list(case.jobs)
    assert await count(session, RemediationEvent) == case.events


# ------------------------------------------------------------- the transitions worth their own test


async def test_the_first_event_links_the_delivery_that_caused_it(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """`remediation_event.webhook_delivery_id` is what makes the audit trail joinable back to the
    body GitHub sent, and `from_state` is null for the event that creates the remediation."""
    signed = delivery("issues.labeled")

    await post(client, signed)

    stored = await one(session, WebhookDelivery)
    event = await one(session, RemediationEvent)
    assert event.webhook_delivery_id == stored.id
    assert (event.from_state, event.to_state) == (None, "QUEUED")
    assert event.kind == "transition"
    assert event.detail["source"] == "webhook"
    assert event.detail["trigger"] == "issue_labelled"


async def test_the_create_session_job_carries_the_delivery_id(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """The delivery id is the correlation id end to end — it becomes the `run:` Devin tag, so the
    job that creates the session cannot be enqueued without it."""
    signed = delivery("issues.labeled")

    await post(client, signed)

    job = await one(session, Job)
    remediation = await one(session, Remediation)
    assert job.kind == JobKind.CREATE_SESSION
    assert job.remediation_id == remediation.id
    assert job.payload["delivery_id"] == signed.delivery_id
    assert job.payload["trigger"] == "issue_labelled"
    assert job.payload["issue_number"] == ISSUE_NUMBER
    assert job.payload["repo"] == REPO


async def test_an_evaluation_job_carries_the_pull_request_it_is_about(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """`evaluate_ci` looks the head SHA up for itself — a payload SHA can be stale by the time the
    job is claimed — but it needs to know which pull request to ask about."""
    await seed(session, **linked(state="CI_RUNNING"))

    await post(client, delivery("check_suite.completed.failure"))

    job = await one(session, Job)
    assert job.kind == JobKind.EVALUATE_CI
    assert job.payload["pr_number"] == PR_NUMBER
    assert job.payload["intent"] == "evaluate_ci"
    # No trigger: nothing was decided here. The worker chooses one from the whole head SHA.
    assert "trigger" not in job.payload


async def test_a_forwarded_comment_carries_no_trigger(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """A person talking to the session does not move the remediation, and the absent `trigger` is
    how the handler knows there is a message to forward and no state to apply."""
    await seed(session, state="RUNNING")

    await post(client, delivery("issue_comment.created"))

    job = await one(session, Job)
    assert job.kind == JobKind.RESUME_SESSION
    assert "trigger" not in job.payload
    assert job.payload["intent"] == "forward_comment"


@pytest.mark.parametrize("conclusion", ["success", "failure"])
async def test_a_completed_check_suite_moves_nothing_by_itself(
    conclusion: str,
    client: httpx.AsyncClient,
    delivery: DeliveryFactory,
    session: AsyncSession,
) -> None:
    """The heart of the change. Whatever a suite concluded, the ingress writes no transition and no
    `remediation_event`: the conclusion is one of the fork's 46 workflows talking, and until a
    worker has read every check run on the head SHA there is nothing to say about the pull request.

    Sentinel used to move to `CI_PASSED` here — on the first live remediation, off a workflow that
    checks for a `hold` label, three seconds after the pull request opened.
    """
    await seed(session, **linked(state="CI_RUNNING"))

    response = await post(client, delivery(f"check_suite.completed.{conclusion}"))

    assert response.status_code == 202
    remediation = await one(session, Remediation)
    assert remediation.state == "CI_RUNNING"
    assert remediation.ci_green_at is None
    assert await count(session, RemediationEvent) == 0
    job = await one(session, Job)
    assert job.kind == JobKind.EVALUATE_CI


@pytest.mark.parametrize("state", ["BLOCKED", "FAILED"])
async def test_a_merge_is_stamped_even_where_it_moves_nothing(
    state: str,
    client: httpx.AsyncClient,
    delivery: DeliveryFactory,
    session: AsyncSession,
) -> None:
    """Remediation 1 sat in `BLOCKED` while a human resolved the escalation by merging its pull
    request. `PR_MERGED` is legal only from `IN_REVIEW`, so a terminal state absorbed it,
    `merged_at` was never stamped, and the funnel reported `merged: 0` beside a link to a pull
    request GitHub shows as merged.

    The state is deliberately left alone — flattening it to `MERGED` would erase the escalation,
    which is a real thing that happened. Only the observation is recorded.
    """
    await seed(session, **linked(state=state))

    response = await post(client, delivery("pull_request.closed.merged"))

    assert response.status_code == 202
    remediation = await one(session, Remediation)
    assert remediation.state == state, "a merge does not undo an escalation"
    assert remediation.merged_at is not None, "but it is still a merge, and it is recorded"
    event = await one(session, RemediationEvent)
    assert (event.from_state, event.to_state) == (state, state)
    assert event.detail["trigger"] == "pr_merged"


async def test_a_merge_observed_twice_keeps_the_first_time(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """`merged_at` is when the pull request was merged, not when the latest delivery about it
    arrived — MTTR and review latency are both subtractions from it."""
    merged = datetime.datetime(2026, 8, 7, 16, 0, tzinfo=datetime.UTC)
    await seed(session, **linked(state="BLOCKED", merged_at=merged))

    await post(client, delivery("pull_request.closed.merged"))

    remediation = await one(session, Remediation)
    assert remediation.merged_at == merged


async def test_a_check_suite_completing_after_a_merge_asks_for_nothing(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """Terminal states are absorbing (`docs/04-state-machine.md`, invariant 1). There is no
    transition to record and nothing an evaluation could change, so no job is enqueued — the
    delivery's own row is where a suite arriving after a merge is visible.

    Seven suites were still concluding on PR #9 nine minutes after it went to review, and on a
    merged remediation each of them would otherwise buy two GitHub calls to reach the same answer.
    """
    await seed(session, **linked(state="MERGED"))

    response = await post(client, delivery("check_suite.completed.failure"))

    assert response.status_code == 202
    remediation = await one(session, Remediation)
    assert remediation.state == "MERGED"
    assert await count(session, Job) == 0
    assert await count(session, RemediationEvent) == 0


async def test_a_comment_on_a_terminal_remediation_forwards_nothing(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """Nothing will read the message, and a comment on a merged issue is conversation rather than
    intervention — so it must not move the autonomy rate either."""
    await seed(session, state="MERGED")

    response = await post(client, delivery("issue_comment.created"))

    assert response.status_code == 202
    remediation = await one(session, Remediation)
    assert remediation.human_message_count == 0
    assert await count(session, Job) == 0


async def test_a_delivery_about_a_remediation_nobody_created_is_recorded_only(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """A check suite for a branch Sentinel never touched: every trigger but one is illegal from
    `None`, so reaching `transition()` here would raise on an ordinary event."""
    response = await post(client, delivery("check_suite.completed.failure"))

    assert response.status_code == 202
    assert response.json()["result"] == "ignored"
    assert await count(session, Remediation) == 0
    assert await count(session, RemediationEvent) == 0
    assert await count(session, Job) == 0


# --------------------------------------------------------------------------------- cancellation


@pytest.mark.parametrize(
    ("action", "expected"),
    [("unlabeled", "autofix_label_removed"), ("closed", "issue_closed")],
)
async def test_cancellation_writes_the_reason_to_the_remediation_and_does_not_escalate(
    client: httpx.AsyncClient,
    delivery: DeliveryFactory,
    session: AsyncSession,
    action: str,
    expected: str,
) -> None:
    """The reason goes to `remediation.blocked_reason`, not only to the event.

    `docs/07-observability.md` computes the failure breakdown as a count grouped by
    `blocked_reason` over `state in (BLOCKED, FAILED)`, so a reason living only on the event would
    show there as an unlabelled null bucket. Escalation is suppressed: telling a person to look at
    the issue they have just closed is telling them to look at what they were already looking at.
    """
    await seed(session, state="RUNNING")

    await post(client, delivery("issues.labeled", action=action))

    remediation = await one(session, Remediation)
    assert remediation.state == "FAILED"
    assert remediation.blocked_reason == expected
    assert remediation.closed_at is not None
    event = await one(session, RemediationEvent)
    assert event.detail["reason"] == expected
    assert await count(session, Job) == 0


async def test_a_cancelled_remediation_that_is_already_terminal_is_untouched(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """Sentinel closes the issue itself when a pull request merges, so a successful remediation
    produces an `issues.closed` delivery of its own. "Cancel if not yet terminal" is what
    `transition()` absorbing a terminal state already means."""
    merged = datetime.datetime(2026, 8, 7, 18, 0, tzinfo=datetime.UTC)
    await seed(session, **linked(state="MERGED", merged_at=merged))

    await post(client, delivery("issues.labeled", action="closed"))

    remediation = await one(session, Remediation)
    assert remediation.state == "MERGED"
    assert remediation.blocked_reason is None
    assert remediation.merged_at == merged


async def test_an_abandoned_pull_request_escalates(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """The cancellation ADR suppresses escalation for the two reasons a maintainer calls the work
    off by. A pull request closed unmerged is not one of them, so `FAILED` escalates as the state
    machine's table says it does."""
    await seed(session, **linked(state="RUNNING"))

    await post(client, delivery("pull_request.closed.merged", **unmerged()))

    remediation = await one(session, Remediation)
    assert remediation.state == "FAILED"
    assert remediation.blocked_reason == "pull_request_closed_unmerged"
    job = await one(session, Job)
    assert job.kind == JobKind.ESCALATE
    assert job.payload["reason"] == "pull_request_closed_unmerged"


# ------------------------------------------------------------------------------------ issue class


async def test_the_issue_class_comes_from_the_class_label(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    payload = github_payload("issues.labeled")
    labelled = {
        **payload["issue"],
        "labels": [*payload["issue"]["labels"], {"name": "class:flaky-test"}],
    }

    await post(client, delivery("issues.labeled", issue=labelled))

    remediation = await one(session, Remediation)
    assert remediation.issue_class == "flaky-test"


async def test_an_issue_with_no_class_label_is_unclassified(
    client: httpx.AsyncClient, delivery: DeliveryFactory, session: AsyncSession
) -> None:
    """`unclassified` is not one of the classes in `docs/01-overview.md`, which is the point: the
    worker raises `UnknownIssueClass` for it and `docs/04-state-machine.md` routes that to
    `BLOCKED`, so a human classifies the issue instead of an ACU budget being spent on a guess."""
    await post(client, delivery("issues.labeled"))

    remediation = await one(session, Remediation)
    assert remediation.issue_class == "unclassified"
    assert remediation.state == "QUEUED"


# ------------------------------------------------------------------- nothing external, ever


async def test_no_external_call_is_made_on_the_request_path(
    client: httpx.AsyncClient,
    delivery: DeliveryFactory,
    session: AsyncSession,
    http_mock: respx.MockRouter,
) -> None:
    """The request path is the signature, a handful of writes and a response — GitHub abandons a
    delivery after about ten seconds
    (`docs/adr/2026-08-07-respond-202-before-external-calls.md`)."""
    await seed(session, **linked(state="CI_RUNNING"))

    await post(client, delivery("check_suite.completed.failure"))

    assert not http_mock.calls


# ---------------------------------------------------------------------------- the whole sequence


async def test_a_remediation_walks_from_label_to_merge(
    client: httpx.AsyncClient,
    delivery: DeliveryFactory,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One issue, four deliveries, and the states between them.

    The two steps only the poller can take — creating the session and linking the pull request —
    are applied here directly, because this is about what the webhooks in between do with them.
    """
    await post(client, delivery("issues.labeled"))

    async with session_factory() as other:
        remediation = (await other.scalars(select(Remediation))).one()
        remediation.state = "PR_OPENED"
        remediation.devin_session_id = "devin-4a1b"
        remediation.pr_number = PR_NUMBER
        remediation.pr_url = PR_URL
        await other.commit()

    # Both check suites ask for an evaluation and neither moves the remediation: the verdict is a
    # worker's to reach (`tests/test_worker.py`), and this test is about the ingress. The states
    # between are applied directly for the same reason the poller's two steps are.
    await post(client, delivery("check_suite.completed.failure", delivery_id=SECOND_DELIVERY))
    asked = await one(session, Remediation)
    assert asked.state == "PR_OPENED"

    async with session_factory() as other:
        remediation = (await other.scalars(select(Remediation))).one()
        remediation.state = "IN_REVIEW"
        await other.commit()

    await post(client, delivery("check_suite.completed.success", delivery_id=THIRD_DELIVERY))

    await post(
        client,
        delivery("pull_request.closed.merged", delivery_id="3e4fb173-a06d-44c4-cf51-7c9d4bae6b8a"),
    )
    merged = await one(session, Remediation)
    assert merged.state == "MERGED"
    assert merged.merged_at is not None

    kinds = [job.kind for job in await rows(session, Job)]
    assert kinds == [JobKind.CREATE_SESSION, JobKind.EVALUATE_CI, JobKind.EVALUATE_CI]
    assert await count(session, WebhookDelivery) == 4
    assert [row.handler_result for row in await rows(session, WebhookDelivery)] == [
        "enqueued",
        "enqueued",
        "enqueued",
        "ignored",
    ]
