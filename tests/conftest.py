"""The shared test harness: a database, faked HTTP, an in-process API client, and clean globals.

`docs/08-testing.md#orchestrator-tests` names the pieces — `pytest-asyncio`, `respx` for the Devin
and GitHub HTTP layers, `httpx.ASGITransport` for in-process API calls, and a real Postgres from
Compose. This module is where they are assembled, so that a test file states what it is about and
nothing else.

**Isolation is the harness's job, not each test's.** A test that forgets to clean up does not
poison the run:

- every test that asks for a database gets a migrated schema with no rows in it;
- `structlog` is put back to its defaults after every test, whether or not the test configured it,
  because `structlog.configure()` installs one pipeline for the whole interpreter;
- metrics are built against a `CollectorRegistry` of the test's own, never the process-global one;
- the `respx` router is torn down with the test, and refuses any request it was not told about, so
  an unmocked call to Devin or GitHub fails loudly instead of leaving the machine.

`docs/adr/2026-08-08-tests-are-isolated-by-the-harness.md` records why the database is truncated
between tests rather than re-migrated or rolled back, and what that costs.

Builders for rows and the recorded payloads live in `factories.py`, which is imported directly:

    from factories import a_remediation, github_payload
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog
from alembic import command
from alembic.config import Config
from prometheus_client import CollectorRegistry
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from factories import DELIVERY_ID, github_payload
from sentinel import config, db
from sentinel.config import Settings
from sentinel.models import Base
from sentinel.observability.logging import configure_logging
from sentinel.observability.prom import Metrics, build_metrics

# --------------------------------------------------------------------------------- configuration

# The host-side URL documented in `.env.example`, for a run that has not exported its own. CI sets
# DATABASE_URL; a worktree sharing the machine with another one has to.
DEFAULT_DATABASE_URL = "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"

DEVIN_API_BASE = "https://api.devin.ai"
GITHUB_API_BASE = "https://api.github.com"

# Credentials shaped like the real ones, because the redaction patterns are shaped around them.
DEVIN_TOKEN = "cog_live_9f3a1c7d2b4e6f8a0c5d"
GITHUB_TOKEN = "github_pat_11ABCDEFG0abcdefghijkl_9Zx8Yw7Vu6Ts5Rq4Po3Nm"
WEBHOOK_SECRET = "whsec_7c1e9b40f6d84a2ba5c3e7f19d0b2a68"
DATABASE_URL = "postgresql+asyncpg://sentinel:s3cr3t-db-password@db:5432/sentinel"

CONFIGURATION: dict[str, Any] = {
    "devin_api_base": DEVIN_API_BASE,
    "devin_api_token": DEVIN_TOKEN,
    "devin_org_id": "org-abc123",
    "devin_playbook_ids": {"security": "playbook-sec"},
    "github_token": GITHUB_TOKEN,
    "github_webhook_secret": WEBHOOK_SECRET,
    "database_url": DATABASE_URL,
    "log_level": "debug",
}


def make_settings(**overrides: Any) -> Settings:
    """A complete configuration, independent of the developer's environment.

    Every field the model requires is passed explicitly, and initialiser arguments win over the
    environment and over `.env`, so nothing here depends on where the suite is run from. The
    `settings` fixture is this with the real test database substituted in.
    """
    return Settings(**{**CONFIGURATION, **overrides})


@pytest.fixture
def bare_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing configured at all: no variable of the model set, and no `.env` to be found.

    For the tests that load a real configuration rather than being handed `make_settings()` — the
    scripts, which declare the group they read and must start with only the variables of that
    group. `env_file` is resolved against the working directory, so the run has to happen in an
    empty one or the developer's own `.env` fills in what the test is asserting is unnecessary.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    """The configuration the suite runs under: fake credentials, a real database URL."""
    return make_settings(database_url=database_url)


# ------------------------------------------------------------------------------------- database

TABLES: tuple[str, ...] = tuple(table.name for table in Base.metadata.sorted_tables)

# Emptying the schema between tests, rather than re-creating it. `RESTART IDENTITY` puts the
# sequences back to 1, so the first remediation of every test has id 1 and a test may assert on it.
TRUNCATE = text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")

# Whether the schema is there at all. It is not, on the first database test of a run, and it is not
# after `tests/test_models.py` has downgraded to base — which is why this is checked rather than
# migrated once per session and assumed ever after.
COUNT_TABLES = text(
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:names)"
)


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    # Located from this file rather than the working directory: `script_location` in the ini is
    # relative to the ini, so this is the only path that has to be found.
    return Config(Path(__file__).resolve().parents[1] / "alembic.ini")


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine on the test database. Says nothing about what is in it — see `database`."""
    engine = db.create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture
async def database(engine: AsyncEngine, alembic_config: Config) -> AsyncEngine:
    """The migrated schema, with no rows in it.

    Emptied on the way *in* rather than on the way out, so that a failing test leaves its rows in
    the database to be looked at, and so that a test which reached the database by some other route
    still starts from an empty one.
    """
    async with engine.begin() as connection:
        found = (await connection.execute(COUNT_TABLES, {"names": list(TABLES)})).scalar_one()
        migrated = found == len(TABLES)
        if migrated:
            await connection.execute(TRUNCATE)
    if not migrated:
        await recreate_schema(engine, alembic_config)
    return engine


@pytest.fixture
def session_factory(database: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """For a test that needs several sessions at once — two workers claiming from the queue.

    Each session it builds takes a connection of its own, which is what makes
    `FOR UPDATE SKIP LOCKED` observable: a single session cannot contend with itself.
    """
    return db.create_session_factory(database)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session on an empty, migrated schema. The workhorse of the database tests."""
    async with session_factory() as session:
        yield session


@pytest.fixture
def process_engine(
    settings: Settings, database: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point `db.get_engine()` — the process-wide engine — at the test database.

    For code that takes its session from `db.session_scope()` rather than from a fixture, which is
    everything outside the tests. Dispose it with `await db.dispose_engine()` at the end of the
    test: the cache is cleared here, but closing the pool needs a running loop.
    """
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()
    yield
    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()


async def recreate_schema(engine: AsyncEngine, config: Config) -> None:
    """Drop whatever is in `public` and run the migrations over it."""
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await run_alembic(engine, config, command.upgrade, "head")


async def run_alembic(
    engine: AsyncEngine,
    config: Config,
    action: Callable[[Config, str], None],
    revision: str,
) -> None:
    """Run an Alembic command on a connection of ours, as `alembic/env.py` allows."""

    def run(connection: Connection) -> None:
        config.attributes["connection"] = connection
        action(config, revision)

    async with engine.begin() as connection:
        await connection.run_sync(run)


# ------------------------------------------------------------------------ logging and metrics


@pytest.fixture(autouse=True)
def clean_globals() -> Iterator[None]:
    """Put the process-global state back after every test, asked for or not.

    `structlog.configure()` installs one pipeline for the interpreter and `get_settings()` caches
    one configuration for it, so a test that touches either leaves the next one running against it
    — including tests that never mention logging and would have no reason to clean up.
    """
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    config.get_settings.cache_clear()


@dataclass(frozen=True)
class Capture:
    """The output of a configured logger, with the configuration that produced it."""

    stream: io.StringIO
    settings: Settings

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @property
    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]

    @property
    def last(self) -> dict[str, Any]:
        return self.records[-1]


Configure = Callable[..., Capture]


@pytest.fixture
def capture() -> Configure:
    """Configure logging into a buffer: `logs = capture(log_level="warning")`.

    Overrides are `Settings` fields. `clean_globals` undoes the configuration afterwards.
    """

    def configure(**overrides: Any) -> Capture:
        settings = make_settings(**overrides)
        stream = io.StringIO()
        configure_logging(settings, stream=stream)
        return Capture(stream=stream, settings=settings)

    return configure


@pytest.fixture
def registry() -> CollectorRegistry:
    """A registry of this test's own, so assertions cannot see another test's observations."""
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> Metrics:
    return build_metrics(registry)


# ------------------------------------------------------------------------------- faked HTTP


@dataclass(frozen=True)
class SentRequest:
    """A request Sentinel made, as the assertion wants to read it.

    `.json` is the point of this class. `.claude/rules/testing.md` requires the *captured request
    body* to be asserted rather than the fact that a call happened, because the tags, the schema and
    the ACU ceiling Sentinel sends are the part reviewers verify independently.
    """

    method: str
    url: httpx.URL
    headers: httpx.Headers
    content: bytes

    @property
    def path(self) -> str:
        return self.url.path

    @property
    def json(self) -> Any:
        return json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode()


class FakeAPI:
    """One external service, faked: what it answers, and what it was asked.

        devin_api.responds("POST", "/v3/organizations/org-abc123/sessions", 201, {"id": "s-1"})
        ...
        sent = devin_api.only("POST", "/v3/organizations/org-abc123/sessions")
        assert sent.json["tags"] == ["sentinel", "run:8f1c"]
        assert sent.headers["authorization"] == f"Bearer {DEVIN_TOKEN}"

    Anything not registered raises rather than reaching the network, so a client that starts
    calling a second endpoint fails in the test that added it.
    """

    def __init__(self, router: respx.Router, base_url: str) -> None:
        self.router = router
        self.base_url = base_url

    def route(self, method: str, path: str) -> respx.Route:
        """The underlying `respx` route, for a response that is not a single fixed body — a
        sequence of them, a `side_effect`, a delay."""
        return self.router.request(method.upper(), f"{self.base_url}{path}")

    def responds(
        self,
        method: str,
        path: str,
        status_code: int = 200,
        json: Any = None,
        **response: Any,
    ) -> respx.Route:
        """Answer `method path` with one fixed response."""
        return self.route(method, path).mock(
            return_value=httpx.Response(status_code, json=json, **response)
        )

    @property
    def requests(self) -> list[SentRequest]:
        """Everything sent to this service, in order."""
        return [
            SentRequest(
                method=call.request.method,
                url=call.request.url,
                headers=call.request.headers,
                content=call.request.content,
            )
            for call in self.router.calls
            if str(call.request.url).startswith(self.base_url)
        ]

    def sent(self, method: str | None = None, path: str | None = None) -> list[SentRequest]:
        """The requests matching `method` and `path`, in order."""
        return [
            request
            for request in self.requests
            if (method is None or request.method == method.upper())
            and (path is None or request.path == path)
        ]

    def only(self, method: str | None = None, path: str | None = None) -> SentRequest:
        """The one matching request — and an assertion that there was exactly one.

        The retry and deduplication policies are all about how many times something is sent, so
        "the body was right" and "it was sent once" are asserted together rather than separately.
        """
        matching = self.sent(method, path)
        assert len(matching) == 1, (
            f"expected exactly one {method or 'request'} {path or ''} to {self.base_url}, "
            f"got {[(request.method, request.path) for request in self.requests]}"
        )
        return matching[0]


@pytest.fixture
def http_mock() -> Iterator[respx.MockRouter]:
    """The `respx` router both fakes share, active for the length of one test.

    One router rather than one per service: it is the router that patches `httpx`, and a single
    patch is what lets an unregistered request to *either* service raise instead of being handled
    by whichever mock happened to be innermost. `assert_all_called` is off — a fake is set up for
    what the test allows, not for what it demands.
    """
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def devin_api(http_mock: respx.MockRouter) -> FakeAPI:
    return FakeAPI(http_mock, DEVIN_API_BASE)


@pytest.fixture
def github_api(http_mock: respx.MockRouter) -> FakeAPI:
    return FakeAPI(http_mock, GITHUB_API_BASE)


# ------------------------------------------------------------------------ the API, in process

ClientFactory = Callable[..., Awaitable[httpx.AsyncClient]]


@asynccontextmanager
async def run_lifespan(app: Any) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol, which `ASGITransport` does not.

    Without it, an app that opens its resources at startup is exercised with them unopened — which
    is not the arrangement production runs in. Startup failures surface here rather than as every
    request failing for an unrelated-looking reason.
    """
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))

    await incoming.put({"type": "lifespan.startup"})
    started = await outgoing.get()
    assert started["type"] == "lifespan.startup.complete", started
    try:
        yield
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        await outgoing.get()
        await task


@pytest.fixture
async def asgi_client() -> AsyncIterator[ClientFactory]:
    """A client that calls an ASGI app in this process: `client = await asgi_client(app)`.

    It takes the app rather than importing it, because a test may need one assembled differently —
    a single router, a dependency overridden — and because `sentinel.api` is built by another task.
    Startup and shutdown run unless `lifespan=False`; the client is closed with the test.
    """
    async with AsyncExitStack() as stack:

        async def build(app: Any, *, lifespan: bool = True, **options: Any) -> httpx.AsyncClient:
            if lifespan:
                await stack.enter_async_context(run_lifespan(app))
            return await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    **options,
                )
            )

        yield build


# ------------------------------------------------------------------------- webhook deliveries


@dataclass(frozen=True)
class Delivery:
    """A GitHub webhook delivery: the exact bytes, and the headers that authenticate them.

    `body` is what the signature was computed over, so it is what the request must be sent with —
    re-serialising `payload` would change the digest, which is the failure the pipeline is
    supposed to be immune to and a test must not introduce by accident.
    """

    event: str
    delivery_id: str
    payload: dict[str, Any]
    body: bytes
    headers: dict[str, str]


def sign(body: bytes, secret: str) -> str:
    """The `X-Hub-Signature-256` value GitHub would send. Computed here rather than borrowed from
    `sentinel.security.hmac`, so that the tests of that module do not verify their own arithmetic.
    """
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


DeliveryFactory = Callable[..., Delivery]


@pytest.fixture
def delivery() -> DeliveryFactory:
    """Build a signed delivery from a recorded payload:

        signed = delivery("issues.labeled")
        response = await client.post("/webhooks/github", content=signed.body,
                                     headers=signed.headers)

    The event name is the first segment of the file name, so `check_suite.completed.failure` is
    delivered as `check_suite`. Keyword arguments replace top-level keys of the payload — an
    `action` the mapping does not know, a different `repository` — and `secret` signs with the
    wrong key for the tests that must be rejected.
    """

    def build(
        name: str,
        *,
        event: str | None = None,
        delivery_id: str = DELIVERY_ID,
        secret: str = WEBHOOK_SECRET,
        **payload_overrides: Any,
    ) -> Delivery:
        payload = {**github_payload(name), **payload_overrides}
        event = event or name.split(".")[0]
        body = json.dumps(payload).encode()
        return Delivery(
            event=event,
            delivery_id=delivery_id,
            payload=payload,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": sign(body, secret),
            },
        )

    return build
