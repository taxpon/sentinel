"""The read endpoints of `docs/07-observability.md#analytics-api`, as a router `main.py` registers.

Three endpoints, all reads, all served from the database this process is already connected to:

| Endpoint | Body |
|---|---|
| `GET /api/analytics/summary?window=7d` | The summary schema, from `sentinel.analytics.metrics` |
| `GET /api/remediations` | The live table's rows |
| `GET /api/remediations/{id}` | One remediation's `remediation_event` log |

**This layer computes nothing.** Every figure in the summary is `metrics.summary()`'s, and the
schema it answers with is `metrics.SummaryJson` — the module's own `TypedDict`, handed to FastAPI as
the response model rather than transcribed into Pydantic models here. There is therefore one
statement of the published schema in the Python, it sits next to the code that fills it in, and a
key that goes missing fails the request instead of reaching a dashboard that indexes into it
(`docs/adr/2026-08-08-the-metrics-module-is-the-published-schema.md`).

The same principle decides the error: `parse_window` raises `ValueError` for a malformed window
spec — and only `ValueError`, deliberately, including for the counts too large to be a `timedelta`
— so the boundary translates that into `400` with the message it raised. What "malformed" means,
and the 365-day ceiling, are the metrics module's; naming a status code is this module's.

`/metrics` and `/healthz`, which the same table in the spec lists, belong to the process wiring
(T17, T26) rather than here: neither reads the analytics tables.
"""

from __future__ import annotations

import datetime
from collections.abc import AsyncIterator
from typing import Annotated, Any, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import db
from sentinel.analytics import metrics
from sentinel.models import Remediation, RemediationEvent

router = APIRouter(prefix="/api", tags=["analytics"])


# --- The live table and the timeline --------------------------------------------------------------

# `docs/07-observability.md` pins a JSON schema for the summary and only names the *columns* of
# these two, so the field names are the `remediation` and `remediation_event` columns of
# `docs/03-data-model.md`, which is what `dashboard/src/api.ts` transcribed them from.


class RemediationRowJson(TypedDict):
    """One row of `GET /api/remediations`: a remediation, and the two links that verify it."""

    id: int
    repo: str
    issue_number: int
    issue_class: str
    state: str
    cycle: int
    acus_consumed: float
    elapsed_seconds: int
    devin_session_url: str | None
    pr_number: int | None
    pr_url: str | None
    blocked_reason: str | None
    labeled_at: str


class RemediationEventJson(TypedDict):
    """One row of `GET /api/remediations/{id}`, straight off the append-only log."""

    id: int
    remediation_id: int
    from_state: str | None
    to_state: str
    kind: str
    detail: dict[str, Any] | None
    created_at: str


# --- Dependencies ---------------------------------------------------------------------------------


async def _session() -> AsyncIterator[AsyncSession]:
    """One unit of work per request, from the process-wide engine."""
    async with db.session_scope() as session:
        yield session


def _window(
    window: Annotated[
        str,
        Query(description=f"A count of days or hours, such as {metrics.DEFAULT_WINDOW!r}."),
    ] = metrics.DEFAULT_WINDOW,
) -> metrics.Window:
    """The window the query string asks for, or `400` naming what was wrong with it.

    A dependency rather than a line in the handler so that the interval reaching `metrics.summary()`
    has been through `parse_window`. `Window` is a public dataclass, so a caller could construct a
    degenerate `end <= start` one — this module never does, and every figure over such a window is
    computed from the empty set of remediations it selects.
    """
    try:
        return metrics.parse_window(window)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


SessionDep = Annotated[AsyncSession, Depends(_session)]
WindowDep = Annotated[metrics.Window, Depends(_window)]


# --- Endpoints ------------------------------------------------------------------------------------


@router.get("/analytics/summary")
async def analytics_summary(session: SessionDep, window: WindowDep) -> metrics.SummaryJson:
    """Everything the dashboard needs in one response, for the window it asked for."""
    return await metrics.summary(session, window)


@router.get("/remediations")
async def remediations(session: SessionDep) -> list[RemediationRowJson]:
    """Every remediation, newest first.

    Unwindowed and uncapped: the live table answers "what is happening right now" and a row it
    silently dropped would be worse than a long table. The volume is the one the metrics module
    reduces in Python for the same reason — tens of remediations, not thousands.

    Both live panels re-sort what they receive
    (`docs/adr/2026-08-08-live-panels-sort-client-side-with-an-id-tiebreak.md`), so this order is
    not the one the table displays. It is a total order anyway, tie-broken on `id`, so that two
    polls of an unchanged database cannot disagree: a batch of issues labelled together shares a
    `labeled_at` to the microsecond, and an untie-broken tie is free to come back differently each
    time.
    """
    now = datetime.datetime.now(datetime.UTC)
    rows = await session.scalars(
        select(Remediation).order_by(Remediation.labeled_at.desc(), Remediation.id.desc())
    )
    return [_row(row, now) for row in rows]


@router.get("/remediations/{remediation_id}")
async def remediation_timeline(
    remediation_id: int, session: SessionDep
) -> list[RemediationEventJson]:
    """One remediation's transitions, oldest first, tie-broken on `id`.

    A remediation that does not exist is `404` rather than an empty list. Every remediation is
    created with the event that put it in `QUEUED`, so an empty log is not a state the pipeline can
    produce — returning one would answer "no such remediation" with something the timeline panel
    would render as a remediation that has done nothing.
    """
    if await session.get(Remediation, remediation_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no remediation with id {remediation_id}"
        )
    events = await session.scalars(
        select(RemediationEvent)
        .where(RemediationEvent.remediation_id == remediation_id)
        .order_by(RemediationEvent.created_at, RemediationEvent.id)
    )
    return [_event(event) for event in events]


# --- Rows -----------------------------------------------------------------------------------------


def _row(row: Remediation, now: datetime.datetime) -> RemediationRowJson:
    """`elapsed_seconds` is the age of the remediation — `now - labeled_at`, for every state.

    It keeps running after a terminal state, which is what the live table's "Age" column means and
    what `dashboard/src/api.ts` documents the field as. How long a fix *took* is a different
    question, and one the summary already answers from the same timestamps: MTTR for a merge, and
    `closed_at - labeled_at` for an escalation (`docs/03-data-model.md`). Freezing the clock here
    would put a second, quieter answer to it on the same screen.
    """
    return {
        "id": row.id,
        "repo": row.repo,
        "issue_number": row.issue_number,
        "issue_class": row.issue_class,
        "state": row.state,
        "cycle": row.cycle,
        "acus_consumed": float(row.acus_consumed),
        "elapsed_seconds": round((now - row.labeled_at).total_seconds()),
        "devin_session_url": row.devin_session_url,
        "pr_number": row.pr_number,
        "pr_url": row.pr_url,
        "blocked_reason": row.blocked_reason,
        "labeled_at": _format(row.labeled_at),
    }


def _event(event: RemediationEvent) -> RemediationEventJson:
    return {
        "id": event.id,
        "remediation_id": event.remediation_id,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "kind": event.kind,
        "detail": event.detail,
        "created_at": _format(event.created_at),
    }


def _format(moment: datetime.datetime) -> str:
    """`metrics.TIMESTAMP_FORMAT`, so every instant in the API reads the same way."""
    return moment.astimezone(datetime.UTC).strftime(metrics.TIMESTAMP_FORMAT)
