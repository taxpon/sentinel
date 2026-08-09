"""The `api` process: the one place routers are registered.

Every other module in `sentinel.api` exports a router and registers nothing, so that two tasks
adding an endpoint touch two different files and this one decides the shape of the surface. The
`api` row of `docs/02-architecture.md#runtime-processes` is what it assembles:

| Path | From |
|---|---|
| `POST /webhooks/github` | `webhooks.router` |
| `GET /api/analytics/summary`, `GET /api/remediations…` | `analytics.router` |
| `GET /healthz`, `GET /metrics` | `health.router` |
| everything else | the dashboard bundle, when one has been built into the image |

The dashboard is mounted at `/`, and only if its directory exists — a development checkout has no
`dashboard/dist` until `npm run build` has been run, which is not a reason for the API to refuse to
start. A mount at `/` would match every path, including `/healthz`; what stops it is that FastAPI
resolves mounts at a lower priority than routes rather than in registration order, so the routers
above win wherever they claim a path. `test_health.py` asserts that outcome rather than the
ordering, because the ordering is not what decides it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sentinel import db
from sentinel.api import analytics, health, webhooks
from sentinel.config import Settings, get_settings
from sentinel.observability.logging import configure_logging, get_logger

log = get_logger(__name__)

# Where the Dockerfile puts the built bundle, relative to the repository root. Resolved from this
# file so that it is found the same way whatever directory the process was started from.
DASHBOARD_DIST = Path(__file__).resolve().parents[3] / "dashboard" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on the way up, and close the connection pool on the way down.

    Startup is also where the configuration is first read for an application built without one, so
    a missing variable stops the process here — with the `ConfigurationError` naming it — rather
    than at import, where it would fail anything that so much as mentions this module.
    `dispose_engine` matters for a reload loop more than for a container: an engine cached per
    process would otherwise hand out connections from a pool whose event loop has gone.
    """
    configure_logging(app.state.settings or get_settings())
    log.info("api.started")
    try:
        yield
    finally:
        await db.dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """The application, with `settings` in place of the environment's if one is given.

    `None` means "read the configuration the way everything else does" — through `get_settings`,
    at the moment it is needed. That is what lets `app` below be built at import time without a
    complete environment, which anything importing this module for its routes would otherwise need.
    """
    app = FastAPI(
        title="Sentinel",
        description="Event-driven remediation pipeline over the Devin API.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    if settings is not None:
        # The routers read their configuration through `get_settings`, so an application built with
        # settings of its own has to say so once, here, rather than in each router.
        app.dependency_overrides[get_settings] = lambda: settings

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(analytics.router)

    if DASHBOARD_DIST.is_dir():
        # `html=True` serves `index.html` for a path with no file behind it, which is what a
        # single-page application's client-side routes need.
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    else:
        log.info("api.dashboard.absent", path=str(DASHBOARD_DIST))
    return app


app = create_app()
"""The application `uvicorn sentinel.api.main:app` serves, as `docker-compose.yml` runs it."""
