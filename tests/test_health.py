"""`GET /healthz`, `GET /metrics`, the application they are mounted on, and `python -m sentinel`.

One file for all four because they are one task's worth of wiring: what these tests are really
asserting is that a process started the way `docker-compose.yml` starts it serves the surface
`docs/02-architecture.md#runtime-processes` says it does.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import signal
from typing import Any

import pytest
from fastapi import FastAPI
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import sentinel.__main__ as entrypoint
from factories import a_job, an_acu_ledger_entry
from sentinel import db
from sentinel.api import health, main
from sentinel.config import Settings
from sentinel.models import HEARTBEAT_ID, PollerHeartbeat
from sentinel.observability.prom import Metrics

# --- The application ------------------------------------------------------------------------------


def _paths(app: FastAPI) -> set[str]:
    """Every path the app serves, including the ones inside an included router.

    FastAPI represents `include_router` as a single route object wrapping the router rather than as
    a flattening into `app.routes`, so a comprehension over `app.routes` sees only the four
    documentation endpoints — and an assertion built on one would pass whatever was registered.
    `original_router` is that wrapper's way back to the routes it holds.
    """
    found: set[str] = set()
    for route in app.routes:
        if hasattr(route, "path"):
            found.add(route.path)
        for included in getattr(getattr(route, "original_router", None), "routes", ()):
            if hasattr(included, "path"):
                found.add(included.path)
    return found


def test_the_three_routers_are_registered(settings: Settings) -> None:
    """The whole surface of the `api` process, named rather than counted.

    Named, because the point of `main.py` owning registration is that adding an endpoint is a
    visible change here: a count would go on passing while a path moved.
    """
    app = main.create_app(settings)
    paths = _paths(app)
    assert {"/healthz", "/metrics", "/webhooks/github"} <= paths
    assert {"/api/analytics/summary", "/api/remediations"} <= paths
    assert "/api/remediations/{remediation_id}" in paths


def test_settings_given_to_the_app_reach_the_routers(settings: Settings) -> None:
    """An application built with settings of its own does not read the environment's.

    The routers depend on `get_settings`; overriding it once in `create_app` is what makes that
    true for all of them at once, and a router added later inherits it without knowing.
    """
    app = main.create_app(settings)
    from sentinel.config import get_settings

    assert app.dependency_overrides[get_settings]() is settings


async def test_a_checkout_with_no_dashboard_build_still_starts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`dashboard/dist` exists only after `npm run build`, and the API does not depend on it."""
    monkeypatch.setattr(main, "DASHBOARD_DIST", tmp_path / "absent")
    app = main.create_app(settings)
    assert not any(getattr(route, "name", None) == "dashboard" for route in app.routes)


async def test_the_dashboard_does_not_shadow_the_api_paths(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    asgi_client: Any,
    process_engine: None,
) -> None:
    """A bundle mounted at `/` matches every path, and must still not answer for `/healthz`.

    Asserted through a request rather than on the route list, because what decides it is how
    FastAPI resolves a mount against a route — not the order they were registered in, which this
    test deliberately does not depend on.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>Sentinel</title>", encoding="utf-8")
    monkeypatch.setattr(main, "DASHBOARD_DIST", tmp_path)
    app = main.create_app(settings)
    client = await asgi_client(app, lifespan=False)

    index = await client.get("/")
    assert index.status_code == 200
    assert "Sentinel" in index.text
    # The route that would have been shadowed. It answers from the router, not from the bundle.
    assert (await client.get("/healthz")).headers["content-type"].startswith("application/json")
    await db.dispose_engine()


# --- `GET /healthz` -------------------------------------------------------------------------------


@pytest.fixture
async def client(asgi_client: Any, settings: Settings, process_engine: None) -> Any:
    """The health router over the test database, on an app of its own."""
    app = FastAPI()
    app.include_router(health.router)
    yield await asgi_client(app)
    await db.dispose_engine()


async def test_healthz_is_ok_when_the_database_answers(client: Any) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "acu_ledger_age_seconds": None}


async def test_the_ledger_age_is_reported_when_the_ledger_has_been_synced(
    client: Any, session: AsyncSession
) -> None:
    """Synced an hour ago reads as an hour, measured against the database's clock."""
    database_now = (await session.execute(text("SELECT now()"))).scalar_one()
    session.add(
        an_acu_ledger_entry(synced_at=database_now - datetime.timedelta(hours=1)),
    )
    await session.commit()

    age = (await client.get("/healthz")).json()["acu_ledger_age_seconds"]
    assert age == pytest.approx(3600, abs=60)


async def test_the_newest_sync_is_the_one_reported(client: Any, session: AsyncSession) -> None:
    """The ledger has a row per day, and the question is how stale the *view* is, not the oldest
    day in it."""
    database_now = (await session.execute(text("SELECT now()"))).scalar_one()
    today = database_now.date()
    session.add(
        an_acu_ledger_entry(
            day=today - datetime.timedelta(days=2),
            synced_at=database_now - datetime.timedelta(days=2),
        )
    )
    session.add(an_acu_ledger_entry(day=today, synced_at=database_now))
    await session.commit()

    age = (await client.get("/healthz")).json()["acu_ledger_age_seconds"]
    assert age == pytest.approx(0, abs=60)


async def test_an_unreachable_database_is_503_and_the_body_carries_no_credential(
    client: Any, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The failure SQLAlchemy raises repeats the connection URL, and the URL holds the password.

    So the assertion is on the *body*: `503`, a fixed word, and no part of the exception. A health
    endpoint is reachable by anything that can reach the port.
    """
    url = settings.database_url.get_secret_value()
    password = url.split(":")[2].split("@")[0]
    leaking = OperationalError(f"connect to {url}", (), Exception("password authentication failed"))

    async def refuse(session: AsyncSession) -> float | None:
        raise leaking

    monkeypatch.setattr(health, "_ledger_age", refuse)

    response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "database": "unreachable",
        "acu_ledger_age_seconds": None,
    }
    assert password not in response.text


# --- `GET /metrics` -------------------------------------------------------------------------------


@pytest.fixture
def scraped(
    monkeypatch: pytest.MonkeyPatch, metrics: Metrics, registry: CollectorRegistry
) -> CollectorRegistry:
    """Point the endpoint at a registry of this test's own, and render that one."""
    monkeypatch.setattr(health, "METRICS", metrics)
    monkeypatch.setattr(
        health, "render_exposition", lambda: (generate_latest(registry), "text/plain")
    )
    return registry


def _depth(registry: CollectorRegistry, kind: str, status: str) -> float | None:
    return registry.get_sample_value("sentinel_job_queue_depth", {"kind": kind, "status": status})


async def test_the_queue_depth_is_taken_from_the_database_at_scrape_time(
    client: Any, session: AsyncSession, scraped: CollectorRegistry
) -> None:
    """Not from a counter this process kept: the process that fills the queue is another one."""
    session.add(a_job(kind="create_session", status="pending"))
    session.add(a_job(kind="create_session", status="pending"))
    session.add(a_job(kind="resume_session", status="deferred"))
    await session.commit()

    assert (await client.get("/metrics")).status_code == 200
    assert _depth(scraped, "create_session", "pending") == 2
    assert _depth(scraped, "resume_session", "deferred") == 1


async def test_finished_jobs_are_not_depth(
    client: Any, session: AsyncSession, scraped: CollectorRegistry
) -> None:
    """`done` and `failed` are history. Counting them makes a queue gauge that only ever climbs."""
    session.add(a_job(status="done"))
    session.add(a_job(status="failed"))
    session.add(a_job(status="running"))
    await session.commit()

    await client.get("/metrics")
    assert _depth(scraped, "create_session", "done") is None
    assert _depth(scraped, "create_session", "failed") is None
    assert _depth(scraped, "create_session", "running") == 1


def _lag(registry: CollectorRegistry) -> float | None:
    return registry.get_sample_value("sentinel_poller_lag_seconds")


async def _beat(session: AsyncSession, ago: datetime.timedelta) -> None:
    database_now = (await session.execute(text("SELECT now()"))).scalar_one()
    session.add(PollerHeartbeat(id=HEARTBEAT_ID, ticked_at=database_now - ago))
    await session.commit()


async def test_a_poller_that_has_never_run_reads_as_zero_rather_than_infinite(
    client: Any, scraped: CollectorRegistry
) -> None:
    """A fresh deployment has no heartbeat and nothing to poll. Reporting that as an unbounded lag
    would page someone before the system has been given anything to do."""
    await client.get("/metrics")
    assert _lag(scraped) == 0


async def test_the_lag_is_the_age_of_the_last_completed_tick(
    client: Any, session: AsyncSession, scraped: CollectorRegistry
) -> None:
    await _beat(session, datetime.timedelta(minutes=5))

    await client.get("/metrics")
    assert _lag(scraped) == pytest.approx(300, abs=60)


async def test_a_poller_that_stopped_reads_as_climbing(
    client: Any, session: AsyncSession, scraped: CollectorRegistry
) -> None:
    """The failure this metric exists for: the process is gone, the heartbeat stands still, and the
    gauge grows with every scrape instead of sitting at a comfortable zero."""
    await _beat(session, datetime.timedelta(hours=2))

    await client.get("/metrics")
    assert _lag(scraped) == pytest.approx(7200, abs=60)


async def test_a_kind_that_drains_reads_as_zero_on_the_next_scrape(
    client: Any, session: AsyncSession, scraped: CollectorRegistry
) -> None:
    """A gauge holds its last value for ever, so the snapshot has to zero what it no longer sees."""
    job = a_job(kind="escalate", status="pending")
    session.add(job)
    await session.commit()
    await client.get("/metrics")
    assert _depth(scraped, "escalate", "pending") == 1

    job.status = "done"
    await session.commit()

    await client.get("/metrics")
    assert _depth(scraped, "escalate", "pending") == 0


# --- `python -m sentinel` -------------------------------------------------------------------------


def test_the_two_processes_compose_starts_are_the_ones_it_accepts() -> None:
    """`docker-compose.yml` runs `python -m sentinel worker` and `… poller`, and nothing else.

    `api` is deliberately absent: uvicorn serves `sentinel.api.main:app` directly, and a second way
    to start it would be a second place for its startup to drift.
    """
    assert sorted(entrypoint.PROCESSES) == ["poller", "worker"]


@pytest.mark.parametrize("process", ["worker", "poller"])
def test_the_named_process_is_the_one_run(process: str, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def record(*, stop: asyncio.Event) -> None:
        ran.append(process)

    monkeypatch.setitem(entrypoint.PROCESSES, process, record)
    assert entrypoint.main([process]) == 0
    assert ran == [process]


def test_an_unknown_process_is_rejected_rather_than_started() -> None:
    with pytest.raises(SystemExit) as exit_code:
        entrypoint.main(["api"])
    assert exit_code.value.code == 2


def test_ctrl_c_is_a_clean_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable only where the signal handler could not be installed. Still not a traceback."""

    async def interrupted(*, stop: asyncio.Event) -> None:
        raise KeyboardInterrupt

    monkeypatch.setitem(entrypoint.PROCESSES, "worker", interrupted)
    assert entrypoint.main(["worker"]) == 0


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_a_stop_signal_ends_the_loop_instead_of_killing_it(
    number: signal.Signals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compose and Fly both send `SIGTERM` and then, after a grace period, `SIGKILL`.

    Ignoring the first means every stop lands on the second, and a worker killed holding a job keeps
    its lease until `JOB_LEASE_TIMEOUT_SECONDS` — so a deploy would stall an in-flight remediation
    for fifteen minutes with nothing to show for it.
    """
    laps: list[int] = []

    async def loop(*, stop: asyncio.Event) -> None:
        os.kill(os.getpid(), number)
        while not stop.is_set():
            laps.append(1)
            await asyncio.sleep(0)
            assert len(laps) < 100, "the signal never reached the event"

    monkeypatch.setitem(entrypoint.PROCESSES, "worker", loop)
    assert entrypoint.main(["worker"]) == 0
    assert laps, "the process should run, not be pre-empted before it starts"


def test_the_work_in_hand_finishes_before_the_process_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of stopping gracefully. Abandoning the job mid-flight is what the lease exists to
    recover from, and recovery costs fifteen minutes."""
    finished = False

    async def loop(*, stop: asyncio.Event) -> None:
        nonlocal finished
        os.kill(os.getpid(), signal.SIGTERM)
        # Waited for rather than assumed: how many loop iterations a signal takes to arrive is the
        # runtime's business, and a test that guessed would be flaky rather than wrong.
        await asyncio.wait_for(stop.wait(), timeout=5)
        # The job in hand, carried out after the stop was requested.
        await asyncio.sleep(0)
        finished = True

    monkeypatch.setitem(entrypoint.PROCESSES, "worker", loop)
    assert entrypoint.main(["worker"]) == 0
    assert finished


def test_the_handlers_do_not_outlive_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` installs handlers on the running loop and must take them off again — a test process
    that ran two of these in one interpreter would otherwise carry the first one's handlers into the
    second's loop."""
    installed: list[signal.Signals] = []
    removed: list[signal.Signals] = []

    async def loop(*, stop: asyncio.Event) -> None:
        pass

    real_get = asyncio.get_running_loop

    async def spy() -> None:
        running = real_get()
        monkeypatch.setattr(
            running, "add_signal_handler", lambda n, *a: installed.append(n), raising=False
        )
        monkeypatch.setattr(running, "remove_signal_handler", lambda n: removed.append(n))
        await entrypoint.serve(loop)

    asyncio.run(spy())
    assert installed == list(entrypoint.STOP_SIGNALS)
    assert removed == list(entrypoint.STOP_SIGNALS)
