"""`GET /healthz` and `GET /metrics` — the two endpoints of `docs/07-observability.md` that read no
analytics table.

`/healthz` answers the question the demo runbook asks before anything else: is this process able to
do its job right now? Liveness alone cannot answer that — a process that has lost its database
answers a bare liveness probe perfectly well while dropping every webhook GitHub sends it. So the
check is a round trip to Postgres, and the body carries the age of `acu_ledger.synced_at` beside
it, because a budget guard reading a day-old ledger will let through spend it should have stopped
([`docs/07-observability.md`](../../../docs/07-observability.md)).

`/metrics` publishes the queue depth from a database snapshot taken at scrape time rather than from
counters this process has incremented, because the process serving it is not the one that fills the
queue (`docs/adr/2026-08-08-metrics-are-process-local.md`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import db
from sentinel.models import HEARTBEAT_ID, AcuLedger, Job, PollerHeartbeat
from sentinel.observability.logging import get_logger
from sentinel.observability.prom import METRICS, render_exposition
from sentinel.queue import JobStatus

router = APIRouter(tags=["health"])

log = get_logger(__name__)

OK = "ok"
DEGRADED = "degraded"

# Queue rows a scrape should count: everything not yet finished with. `done` and `failed` are
# history, and publishing them as depth would make the gauge climb for ever and never fall.
OUTSTANDING_STATUSES: frozenset[JobStatus] = frozenset(JobStatus) - {
    JobStatus.DONE,
    JobStatus.FAILED,
}


class HealthJson(TypedDict):
    """The body of `GET /healthz`."""

    status: str
    database: str
    acu_ledger_age_seconds: float | None


async def _session() -> AsyncIterator[AsyncSession]:
    async with db.session_scope() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session)]


@router.get("/healthz", response_model=HealthJson)
async def healthz(session: SessionDep, response: Response) -> HealthJson:
    """Whether this process can serve, and how stale the ACU ledger it would decide from is.

    `503` when the database cannot be reached, so that a container orchestrator and the runbook's
    preflight `curl` agree with each other without either having to read the body. A ledger that is
    merely old is *not* a `503`: the API is serving correctly and the staleness belongs to the
    poller, so it is reported in the body and left for a human to read.
    """
    try:
        age = await _ledger_age(session)
    except SQLAlchemyError as exc:
        # Never `str(exc)` into the body: SQLAlchemy puts the URL, and therefore the password, into
        # the message of a connection failure. The reason is in this process's log, not in a
        # response served to whoever can reach the port.
        log.warning("health.database.unreachable", error=type(exc).__name__)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthJson(status=DEGRADED, database="unreachable", acu_ledger_age_seconds=None)
    return HealthJson(status=OK, database=OK, acu_ledger_age_seconds=age)


async def _ledger_age(session: AsyncSession) -> float | None:
    """Seconds since the ACU ledger was last synced, or `None` if it never has been.

    Measured against the database's clock rather than this process's, so that a container whose
    clock has drifted reports the age Postgres would compute and not one of its own invention.
    """
    newest = (await session.execute(select(func.max(AcuLedger.synced_at)))).scalar_one_or_none()
    if newest is None:
        return None
    now = (await session.execute(select(func.now()))).scalar_one()
    return (now - newest).total_seconds()


@router.get("/metrics", include_in_schema=False)
async def metrics(session: SessionDep) -> Response:
    """The Prometheus exposition, with the cross-process figures refreshed from the database.

    Refreshed *here*, on the scrape, rather than on a timer: a gauge published on a timer is stale
    by an unknown amount at the moment it is read, and this one is a snapshot of a table the scrape
    can simply take for itself.
    """
    METRICS.publish_job_queue_depth(await _queue_depth(session))
    METRICS.set_poller_lag(await _poller_lag(session))
    body, content_type = render_exposition()
    return Response(content=body, media_type=content_type)


async def _poller_lag(session: AsyncSession) -> float:
    """Seconds since the poller last completed a tick.

    Every in-flight remediation is reconciled on every tick, so one figure speaks for all of them:
    the age of the last completed tick is how stale the oldest of them is.

    `0.0` before the poller has ever run is deliberate. The alternative — reporting the age of a
    heartbeat that does not exist as infinite — makes a fresh deployment page someone before there
    is anything to poll. A poller that stops *after* running is the failure worth alerting on, and
    that one this reports honestly, climbing tick by tick.

    Measured by Postgres rather than here, so the answer does not depend on this container's clock
    agreeing with the one that wrote the row.
    """
    ticked_at = (
        await session.execute(
            select(PollerHeartbeat.ticked_at).where(PollerHeartbeat.id == HEARTBEAT_ID)
        )
    ).scalar_one_or_none()
    if ticked_at is None:
        return 0.0
    now = (await session.execute(select(func.now()))).scalar_one()
    return (now - ticked_at).total_seconds()


async def _queue_depth(session: AsyncSession) -> dict[tuple[str, str], int]:
    """Outstanding jobs by `(kind, status)`."""
    rows = await session.execute(
        select(Job.kind, Job.status, func.count())
        .where(Job.status.in_(sorted(OUTSTANDING_STATUSES)))
        .group_by(Job.kind, Job.status)
    )
    return {(kind, job_status): count for kind, job_status, count in rows.all()}
