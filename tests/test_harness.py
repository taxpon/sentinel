"""The harness itself: proof that a test cannot see what another test left behind.

`tests/conftest.py` is infrastructure every other test file rests on, and its failure mode is not a
red test — it is a green one. A leaked `structlog` configuration, a registry carrying another
test's samples, a database still holding the previous test's rows: each of those makes some later
test pass or fail for a reason that has nothing to do with what it asserts.

Leakage can only be observed *between* tests, so several of the cases below come in pairs — one
test dirties a global, the next asserts it is clean. Those pairs are marked, and they rely on
pytest running the tests of a file in the order they are written.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import structlog
from alembic.config import Config
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from respx.models import AllMockedAssertionError
from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from conftest import (
    DEVIN_API_BASE,
    GITHUB_API_BASE,
    WEBHOOK_SECRET,
    ClientFactory,
    Configure,
    DeliveryFactory,
    FakeAPI,
    make_settings,
    recreate_schema,
)
from factories import (
    GITHUB_PAYLOADS,
    ISSUE_NUMBER,
    PR_NUMBER,
    REPO,
    a_job,
    a_remediation,
    a_remediation_event,
    a_webhook_delivery,
    an_acu_ledger_entry,
    github_payload,
)
from sentinel.config import get_settings
from sentinel.models import Job, Remediation
from sentinel.observability.logging import configure_logging, get_logger
from sentinel.observability.prom import Metrics
from sentinel.security.hmac import SIGNATURE_HEADER, SignatureResult, verify_signature

# --------------------------------------------------------------------------------- the database


async def test_the_schema_a_test_gets_is_the_one_the_migrations_produce(
    session: AsyncSession,
) -> None:
    # Not `create_all`: a model changed without a migration must fail the suite rather than only
    # production (docs/adr/2026-08-08-migrations-are-the-schema-tests-run-against.md).
    revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()

    assert revision


async def test_a_test_starts_with_an_empty_database(session: AsyncSession) -> None:
    """Half of a pair with the next test: both write the same row and commit it.

    `remediation (repo, issue_number)` is UNIQUE, so whichever of the two ran second would raise
    `IntegrityError` if the first one's row were still there. Neither test cleans up, and the
    pair fails in either order.
    """
    session.add(a_remediation())
    await session.commit()

    assert (await session.execute(select(func.count()).select_from(Remediation))).scalar_one() == 1


async def test_the_next_test_does_not_see_the_previous_one_s_rows(session: AsyncSession) -> None:
    session.add(a_remediation())
    await session.commit()

    assert (await session.execute(select(func.count()).select_from(Remediation))).scalar_one() == 1


async def test_the_identity_sequences_restart_so_ids_are_predictable(
    session: AsyncSession,
) -> None:
    remediation = a_remediation()
    session.add(remediation)
    await session.flush()

    assert remediation.id == 1


async def test_a_row_committed_on_one_connection_is_visible_on_another(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Why the isolation is a truncate and not an open transaction rolled back.

    The queue claims jobs with `FOR UPDATE SKIP LOCKED` across two connections, which only does
    anything when the rows one connection commits are visible to the other. A harness that wrapped
    each test in a single transaction would make this test — and every queue test — vacuous.
    """
    async with session_factory() as writer:
        writer.add(a_job())
        await writer.commit()

    async with session_factory() as reader:
        assert (await reader.execute(select(func.count()).select_from(Job))).scalar_one() == 1


async def test_a_test_may_drop_the_schema_entirely(
    database: AsyncEngine, session: AsyncSession
) -> None:
    """Half of a pair with the next test.

    `tests/test_models.py` downgrades to base and drops `public` on purpose, so the harness cannot
    migrate once and assume the schema survives. It checks, and rebuilds when it has to.
    """
    async with database.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))

    assert await table_names(database) == set()


async def test_the_next_test_gets_the_schema_back(session: AsyncSession) -> None:
    session.add(a_remediation())
    await session.flush()

    assert (await session.execute(select(func.count()).select_from(Remediation))).scalar_one() == 1


async def test_recreate_schema_starts_from_whatever_is_there(
    database: AsyncEngine, alembic_config: Config
) -> None:
    """The rebuild path runs against a schema that is already migrated as well as against none."""
    await recreate_schema(database, alembic_config)

    assert Remediation.__tablename__ in await table_names(database)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda c: inspect(c).get_table_names()))


# --------------------------------------------------------------------------------- the factories


async def test_every_builder_produces_a_row_the_schema_accepts(session: AsyncSession) -> None:
    remediation = a_remediation()
    session.add_all([remediation, a_webhook_delivery(), a_job(), an_acu_ledger_entry()])
    await session.flush()
    session.add(a_remediation_event(remediation_id=remediation.id))
    await session.flush()
    await session.refresh(remediation)

    assert remediation.repo == REPO
    assert remediation.issue_number == ISSUE_NUMBER
    # The defaults documented in docs/03-data-model.md are left to the database, not restated here.
    assert remediation.cycle == 0


def test_an_override_wins_over_the_default() -> None:
    remediation = a_remediation(state="RUNNING", cycle=2, pr_number=PR_NUMBER)

    assert (remediation.state, remediation.cycle, remediation.pr_number) == (
        "RUNNING",
        2,
        PR_NUMBER,
    )


# ------------------------------------------------------------------------------ logging globals


def test_logging_is_captured_into_this_test_s_own_buffer(capture: Configure) -> None:
    logs = capture()

    get_logger().info("harness.checked")

    assert logs.last["event"] == "harness.checked"


def test_a_test_may_configure_logging_without_asking_for_capture() -> None:
    """Half of a pair with the next test.

    `configure_logging` installs a pipeline for the whole interpreter. This test never asks for the
    `capture` fixture, so nothing it requested is responsible for undoing that — the autouse
    fixture is.
    """
    configure_logging(make_settings())

    assert structlog.is_configured()


def test_the_next_test_finds_structlog_at_its_defaults() -> None:
    assert not structlog.is_configured()


def test_a_bound_correlation_id_does_not_outlive_the_test(capture: Configure) -> None:
    """Half of a pair with the next test: context variables outlive the block that bound them."""
    capture()
    structlog.contextvars.bind_contextvars(run="8f1c")

    assert structlog.contextvars.get_contextvars()["run"] == "8f1c"


def test_the_next_test_starts_with_no_correlation_bound() -> None:
    assert structlog.contextvars.get_contextvars() == {}


def test_a_test_may_cache_the_process_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half of a pair with the next test. `get_settings()` caches for the life of the process.

    A test that reads it under a monkeypatched environment would otherwise hand that environment
    to every later test, long after `monkeypatch` had put the variables back.
    """
    for name, value in {
        "DEVIN_API_TOKEN": "cog_live_from_the_environment",
        "DEVIN_ORG_ID": "org-from-the-environment",
        "DEVIN_PLAYBOOK_IDS": '{"security": "playbook-sec"}',
        "GITHUB_TOKEN": "github_pat_from_the_environment",
        "GITHUB_WEBHOOK_SECRET": "whsec_from_the_environment",
        "DATABASE_URL": "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel",
    }.items():
        monkeypatch.setenv(name, value)

    assert get_settings().devin_org_id == "org-from-the-environment"


def test_the_next_test_does_not_inherit_that_configuration() -> None:
    assert get_settings.cache_info().currsize == 0


# ------------------------------------------------------------------------------ metrics globals


def test_the_registry_is_this_test_s_own(registry: CollectorRegistry, metrics: Metrics) -> None:
    """Half of a pair with the next test: the observation below must not survive it."""
    metrics.set_poller_lag(42.0)

    assert registry.get_sample_value("sentinel_poller_lag_seconds") == 42.0


def test_the_next_test_gets_a_registry_with_nothing_observed_in_it(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    assert registry.get_sample_value("sentinel_poller_lag_seconds") == 0.0


# ---------------------------------------------------------------------------------- faked HTTP


async def test_the_captured_request_is_what_a_test_asserts_on(devin_api: FakeAPI) -> None:
    path = "/v3/organizations/org-abc123/sessions"
    devin_api.responds("POST", path, 201, {"session_id": "devin-abc"})

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{DEVIN_API_BASE}{path}",
            json={"prompt": "fix it", "tags": ["sentinel", "run:8f1c"], "max_acu_limit": 10},
            headers={"Authorization": "Bearer cog_live_9f3a1c7d2b4e6f8a0c5d"},
        )

    sent = devin_api.only("POST", path)
    assert sent.json["tags"] == ["sentinel", "run:8f1c"]
    assert sent.json["max_acu_limit"] == 10
    assert sent.headers["authorization"] == "Bearer cog_live_9f3a1c7d2b4e6f8a0c5d"


async def test_only_refuses_a_second_call_to_the_same_endpoint(devin_api: FakeAPI) -> None:
    path = "/v3/organizations/org-abc123/sessions"
    devin_api.responds("POST", path, 201, {"session_id": "devin-abc"})

    async with httpx.AsyncClient() as client:
        await client.post(f"{DEVIN_API_BASE}{path}", json={})
        await client.post(f"{DEVIN_API_BASE}{path}", json={})

    assert len(devin_api.sent("POST", path)) == 2
    with pytest.raises(AssertionError):
        devin_api.only("POST", path)


async def test_a_request_nobody_mocked_never_leaves_the_process(devin_api: FakeAPI) -> None:
    # The whole point of `respx` here: a client that starts calling an endpoint the test did not
    # allow fails in that test, rather than reaching the real Devin or the real GitHub.
    async with httpx.AsyncClient() as client:
        with pytest.raises(AllMockedAssertionError):
            await client.get(f"{DEVIN_API_BASE}/v3/organizations/org-abc123/sessions/devin-abc")


async def test_the_two_services_do_not_see_each_other_s_requests(
    devin_api: FakeAPI, github_api: FakeAPI
) -> None:
    github_api.responds("POST", f"/repos/{REPO}/issues/{ISSUE_NUMBER}/comments", 201, {"id": 1})

    async with httpx.AsyncClient() as client:
        await client.post(
            f"{GITHUB_API_BASE}/repos/{REPO}/issues/{ISSUE_NUMBER}/comments",
            json={"body": "escalating"},
        )

    assert devin_api.requests == []
    assert github_api.only().json == {"body": "escalating"}


async def test_a_mock_does_not_survive_the_test_that_registered_it(devin_api: FakeAPI) -> None:
    """Half of a pair with the next test: the route below must be gone by then."""
    devin_api.responds("POST", "/v3/organizations/org-abc123/sessions", 201, {"session_id": "s"})

    async with httpx.AsyncClient() as client:
        response = await client.post(f"{DEVIN_API_BASE}/v3/organizations/org-abc123/sessions")

    assert response.status_code == 201


async def test_the_next_test_finds_neither_the_route_nor_the_calls(devin_api: FakeAPI) -> None:
    assert devin_api.requests == []
    async with httpx.AsyncClient() as client:
        with pytest.raises(AllMockedAssertionError):
            await client.post(f"{DEVIN_API_BASE}/v3/organizations/org-abc123/sessions")


# ------------------------------------------------------------------------- the API, in process


def an_app() -> FastAPI:
    """A stand-in for `sentinel.api`, which another task builds. It reports whether it was started,
    because that is the part `ASGITransport` does not do on its own."""
    started: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started.append("yes")
        yield
        started.clear()

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, Any]:
        return {"started": started}

    return app


async def test_the_client_reaches_the_app_without_a_socket(asgi_client: ClientFactory) -> None:
    client = await asgi_client(an_app())

    response = await client.get("/ping")

    assert response.status_code == 200


async def test_the_lifespan_runs_unless_it_is_turned_off(asgi_client: ClientFactory) -> None:
    with_lifespan = await asgi_client(an_app())
    without = await asgi_client(an_app(), lifespan=False)

    assert (await with_lifespan.get("/ping")).json() == {"started": ["yes"]}
    assert (await without.get("/ping")).json() == {"started": []}


async def test_the_respx_mock_does_not_intercept_the_app(
    asgi_client: ClientFactory, devin_api: FakeAPI
) -> None:
    """`respx` patches the httpcore transports, and `ASGITransport` is not one of them.

    If it were, every in-process API test would have to register a route for its own application,
    so this is asserted rather than assumed.
    """
    client = await asgi_client(an_app())

    response = await client.get("/ping")

    assert response.status_code == 200
    assert devin_api.requests == []


# ------------------------------------------------------------------------- webhook deliveries


def test_the_delivery_is_signed_over_the_bytes_it_carries(delivery: DeliveryFactory) -> None:
    signed = delivery("issues.labeled")

    result = verify_signature(signed.body, signed.headers[SIGNATURE_HEADER], WEBHOOK_SECRET)

    assert result is SignatureResult.OK
    assert signed.headers["X-GitHub-Event"] == "issues"


def test_a_delivery_signed_with_another_secret_does_not_verify(delivery: DeliveryFactory) -> None:
    signed = delivery("issues.labeled", secret="a-secret-that-is-not-ours")

    assert (
        verify_signature(signed.body, signed.headers[SIGNATURE_HEADER], WEBHOOK_SECRET)
        is SignatureResult.MISMATCH
    )


def test_an_override_reaches_the_body_that_was_signed(delivery: DeliveryFactory) -> None:
    signed = delivery("issues.labeled", action="unlabeled")

    assert signed.payload["action"] == "unlabeled"
    assert b'"action": "unlabeled"' in signed.body
    assert verify_signature(signed.body, signed.headers[SIGNATURE_HEADER], WEBHOOK_SECRET).ok


@pytest.mark.parametrize("name", GITHUB_PAYLOADS)
def test_the_event_header_is_the_first_segment_of_the_file_name(
    delivery: DeliveryFactory, name: str
) -> None:
    signed = delivery(name)

    assert signed.headers["X-GitHub-Event"] == name.split(".")[0]
    assert signed.payload["action"] == name.split(".")[1]


# --------------------------------------------------------------------------- recorded payloads

# docs/06-event-pipeline.md, one row per payload this directory has to hold.
SUBSCRIBED = {
    "issues.labeled": ("issue", "number"),
    "pull_request.opened": ("pull_request", "number"),
    "pull_request.closed.merged": ("pull_request", "merged"),
    "check_suite.completed.success": ("check_suite", "conclusion"),
    "check_suite.completed.failure": ("check_suite", "conclusion"),
    "pull_request_review.submitted.changes_requested": ("review", "state"),
    "issue_comment.created": ("comment", "body"),
}


def test_the_directory_holds_exactly_the_subscribed_events() -> None:
    assert set(GITHUB_PAYLOADS) == set(SUBSCRIBED)


@pytest.mark.parametrize(("name", "location"), sorted(SUBSCRIBED.items()))
def test_each_payload_carries_the_field_the_mapping_reads(
    name: str, location: tuple[str, str]
) -> None:
    payload = github_payload(name)
    obj, field = location

    assert payload["action"] == name.split(".")[1]
    assert payload[obj][field] is not None
    # Every payload is about the repository Sentinel watches, so a test does not have to
    # reconfigure `target_repo` to use one.
    assert payload["repository"]["full_name"] == REPO
    assert payload["sender"]["login"]


def test_the_two_check_suites_describe_the_same_pull_request() -> None:
    """The review-fix loop of docs/08 replays these two against one remediation."""
    failure = github_payload("check_suite.completed.failure")["check_suite"]
    success = github_payload("check_suite.completed.success")["check_suite"]

    assert failure["conclusion"] == "failure"
    assert success["conclusion"] == "success"
    assert [pr["number"] for pr in failure["pull_requests"]] == [PR_NUMBER]
    assert [pr["number"] for pr in success["pull_requests"]] == [PR_NUMBER]
    assert failure["head_sha"] == success["head_sha"]


def test_the_labelled_issue_carries_the_label_that_starts_a_remediation() -> None:
    payload = github_payload("issues.labeled")

    assert payload["label"]["name"] == "devin:autofix"
    assert payload["label"] in payload["issue"]["labels"]
    assert payload["issue"]["number"] == ISSUE_NUMBER


def test_a_payload_mutated_by_one_test_is_whole_again_for_the_next(
    delivery: DeliveryFactory,
) -> None:
    payload = github_payload("issues.labeled")
    payload["issue"]["number"] = -1

    assert github_payload("issues.labeled")["issue"]["number"] == ISSUE_NUMBER
    assert delivery("issues.labeled").payload["issue"]["number"] == ISSUE_NUMBER


# ------------------------------------------------------------------------ the whole thing at once


async def test_the_pieces_compose(
    session: AsyncSession,
    devin_api: FakeAPI,
    github_api: FakeAPI,
    capture: Configure,
    delivery: DeliveryFactory,
    asgi_client: ClientFactory,
) -> None:
    """A database, both fakes, a logger and an in-process client in one test, as T20 will need.

    The fixtures are independent, but a test that asks for all of them is the one that would find
    out they are not — an event loop shared by the engine and the client, a `respx` patch that
    swallowed the ASGI call, a logger configured too late to see anything.
    """
    logs = capture()
    signed = delivery("issues.labeled")
    session.add(a_remediation())
    await session.commit()

    devin_api.responds("POST", "/v3/organizations/org-abc123/sessions", 201, {"session_id": "s-1"})
    github_api.responds("POST", f"/repos/{REPO}/issues/{ISSUE_NUMBER}/comments", 201, {"id": 1})
    client = await asgi_client(an_app())

    async with httpx.AsyncClient() as outbound:
        await outbound.post(
            f"{DEVIN_API_BASE}/v3/organizations/org-abc123/sessions", json={"tags": ["sentinel"]}
        )
    get_logger().info("webhook.received", delivery_id=signed.delivery_id)

    assert (await client.get("/ping")).status_code == 200
    assert devin_api.only("POST", "/v3/organizations/org-abc123/sessions").json["tags"] == [
        "sentinel"
    ]
    assert github_api.requests == []
    assert logs.last["event"] == "webhook.received"
    assert (await session.execute(select(func.count()).select_from(Remediation))).scalar_one() == 1


async def test_the_harness_survives_a_test_that_leaves_a_task_running() -> None:
    # A poller test that forgets to cancel its loop should not be able to make a later test fail.
    task = asyncio.create_task(asyncio.sleep(30))
    await asyncio.sleep(0)
    task.cancel()
