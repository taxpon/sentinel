"""Reconcile Devin sessions into the database, once every `POLL_INTERVAL_SECONDS`.

Devin has no outbound webhook for session status, so this loop is not a convenience — it is the only
mechanism by which session progress is observable at all
(`docs/adr/2026-08-07-poller-drives-state-machine.md`). It is also what links the pull request: a
`pull_request.opened` webhook carries no issue number, so `pull_requests[]` on the session is the
only place the link can be resolved, and `pr_opened_at` — the time-to-PR metric of
`docs/07-observability.md` — is stamped here and nowhere else.

One tick reads every in-flight remediation, calls `GET /v3/organizations/{org}/sessions/{id}` for
each, and reconciles what came back:

| Session status | Trigger the observation carries |
|---|---|
| `new` | none — the session exists, nothing has claimed it |
| `claimed`, `running`, `resuming` | `SESSION_RUNNING` |
| `suspended` | `SESSION_RUNNING` — reaching it means it was claimed |
| `exit` | `SESSION_RUNNING`, then `FAILED` unless a pull request came of it |
| `error` | `SESSION_RUNNING`, then `FAILED` |

plus `PR_OPENED` whenever `pull_requests[]` is non-empty, `BLOCKED` whenever the session says it
cannot proceed, and `FAILED` whenever it has spent its ACU cap. A status other than `new` yields
`SESSION_RUNNING` even when the session has since finished, because `docs/04-state-machine.md` puts
`PR_OPENED` on every path into the CI states (invariant 5) and `RUNNING` is the only state it is
reachable from: a poller that first sees a session at `exit` must still walk it through `RUNNING`,
or the pull request could never be linked.

**A session that ends with nothing to show fails the remediation**, rather than leaving it in
`RUNNING` for a poller to re-read for ever. Both causes are ones only this module can see — Devin
reports them and GitHub never hears about them
(`docs/adr/2026-08-08-a-session-with-nothing-to-show-fails.md`).

**Reconciliation is idempotent**, because it runs every tick and almost every tick sees exactly what
the last one saw. Two rules make it so, and between them they cover every trigger:

- `SESSION_RUNNING` is applied only where `is_legal` says it moves something. It is news exactly
  once, on the way out of `SESSION_CREATED`; from `RUNNING` onwards the state machine would reject
  it, and from `CI_FAILED` it would walk a remediation backwards out of the fix loop.
- Everything else is applied unguarded and absorbed by the state machine — `PR_OPENED` by
  `PullRequestCondition.UNLINKED`, `BLOCKED` and `FAILED` by the terminal state they produce. The
  link is write-once *there*, so checking it here would only duplicate a rule and risk disagreeing
  with it.

An absorbed trigger writes no `remediation_event` and no job. The observed columns —
`devin_status`, `acus_consumed`, the report — are reconciled on every tick whether anything moved or
not, because they are the observation itself rather than a record of a change. See
`docs/adr/2026-08-08-the-poller-records-only-what-moves.md`.

This module registers no metric of its own. Devin latency and error rate are observed by
`DevinClient` in whichever process makes the call, and poller lag is published from a database
snapshot by whichever process serves `/metrics`
(`docs/adr/2026-08-08-metrics-are-process-local.md`).
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel.config import Settings, get_settings
from sentinel.db import dispose_engine, get_session_factory
from sentinel.devin.client import DevinClient, DevinError, DevinResponseError, Sleep
from sentinel.devin.playbooks import acu_cap_for
from sentinel.devin.schemas import Outcome, PullRequest, Session, SessionStatus
from sentinel.models import Remediation, RemediationEvent
from sentinel.observability.logging import configure_logging, correlate, get_logger
from sentinel.pipeline.state import (
    NON_TERMINAL_STATES,
    State,
    Transition,
    Trigger,
    is_legal,
    transition,
)
from sentinel.queue import JobKind, enqueue

log = get_logger(__name__)

POLLED_STATES: Final[frozenset[State]] = NON_TERMINAL_STATES - {State.QUEUED}
"""Which remediations a tick reads. `QUEUED` has no session to poll yet, and a terminal state
absorbs everything a session could still say — so polling one would spend a request per tick, for
ever, on a remediation that cannot change."""

ONLY_WHEN_LEGAL: Final[frozenset[Trigger]] = frozenset({Trigger.SESSION_RUNNING})
"""Triggers the poller checks before applying, rather than letting the state machine answer.

`SESSION_RUNNING` is the one trigger here that the machine neither absorbs nor accepts twice: it
raises from every state but `SESSION_CREATED`, and `CI_FAILED → RUNNING` belongs to the worker
resuming the session, with a cycle increment this observation knows nothing about."""

ESCALATED_STATES: Final[frozenset[State]] = frozenset({State.BLOCKED, State.FAILED})
"""The two terminal states `docs/04-state-machine.md` escalates rather than ends at."""

SOURCE: Final = "poller"
"""Recorded on every event this module writes. A poller transition carries no
`webhook_delivery_id`, and neither does a worker one, so the audit trail says which."""

STALLED_STATUSES: Final[frozenset[SessionStatus]] = frozenset({SessionStatus.RUNNING})
"""The statuses on which `status_detail: waiting_for_user` means the session is stalled.

Narrower than `SessionStatus.is_working`, deliberately. `docs/05-devin-integration.md` describes the
stall as a session *running* and waiting; `claimed` has not begun work, so it cannot have asked
anything yet, and `resuming` is by definition a session the worker has just sent a message to — the
one moment a `waiting_for_user` left over from before that message would be read as a fresh
question. See `docs/adr/2026-08-08-a-stalled-session-is-blocked.md`."""

SESSION_ERROR: Final = "session_error"
"""`blocked_reason` for a session Devin reports as `error`."""

ACU_CAP_EXHAUSTED: Final = "acu_cap_exhausted"
"""`blocked_reason` for a session that spent `ACU_CAPS[issue_class]` without producing a pull
request. `docs/04-state-machine.md` tabulates "ACU cap hit" as a cause of `FAILED`, and Devin
enforces the ceiling itself through `max_acu_limit` — so nothing further will happen on that
session, whatever status it reports."""

SESSION_ENDED_WITHOUT_PULL_REQUEST: Final = "session_ended_without_pull_request"
"""`blocked_reason` for a session Devin is finished with that produced no pull request and gave no
reason. The remediation has nowhere left to go: the CI triggers need a linked pull request, and
nothing resumes a session that is not in the fix loop."""

SESSION_UNREADABLE: Final = "session_unreadable"
"""`blocked_reason` for a session whose body the v3 schemas reject — an undocumented status, or a
structured report missing a field `structured_output_required` promised."""

SESSION_WAITING_FOR_USER: Final = "session_waiting_for_user"
"""`blocked_reason` for a session stalled on a question. See
`docs/adr/2026-08-08-a-stalled-session-is-blocked.md`."""

DEVIN_REPORTED_BLOCKED: Final = "devin_reported_blocked"
"""`blocked_reason` for a report whose outcome is `blocked` but which left `blocked_reason` unset.
The failure-breakdown panel groups on this column, and a null would drop the row from its own
chart."""


@dataclass(frozen=True, slots=True)
class InFlight:
    """One remediation to poll, read out of the database before any request is made.

    Plain data rather than an ORM row on purpose: the session that selected it is closed before the
    first Devin call, so a tick does not hold a database transaction open across N requests of up to
    30 s each.
    """

    remediation_id: int
    session_id: str


@dataclass(frozen=True, slots=True)
class Tick:
    """What one pass over the in-flight remediations did.

    `unreachable` counts the sessions Devin gave no usable answer for — a request that failed, or a
    body the v3 schemas reject. `failed` counts the remediations that raised while being reconciled.
    Neither is an error the tick reports upwards: the next tick asks again, which is the
    self-healing property the poller exists for.
    """

    polled: int
    moved: int
    unreachable: int
    failed: int = 0


def blocked_reason(session: Session) -> str | None:
    """Why this observation says the remediation cannot proceed, or `None` if it does not.

    Two causes, both of which `docs/04-state-machine.md` escalates. Devin reporting
    `outcome: blocked` is the one the spec tabulates. The other is a session in one of
    `STALLED_STATUSES` reporting `waiting_for_user`: it has asked a question, nothing in an
    unattended pipeline will answer it, and without this it would look busy for ever. Which statuses
    those are matters as much as the detail — a `suspended` or `exit` session idling between fix
    cycles also waits for input, and that is the normal state of every remediation in review.
    """
    if session.status in STALLED_STATUSES and session.waiting_for_user:
        return SESSION_WAITING_FOR_USER
    report = session.structured_output
    if report is not None and report.outcome is Outcome.BLOCKED:
        return report.blocked_reason or DEVIN_REPORTED_BLOCKED
    return None


def failure_reason(session: Session, *, issue_class: str, has_pull_request: bool) -> str | None:
    """Why this observation says the session will never produce anything more, or `None`.

    All three causes are invisible to GitHub, which is what makes them the poller's to find. Two are
    conditional on there being no pull request, because the pull request is the deliverable: a
    remediation holding one is carried the rest of the way by check suites and a reviewer, and
    killing it because the session that opened it has stopped would discard finished work. A session
    Devin reports as `error` fails the remediation either way, which is what the spec tabulates.

    The ACU cap is judged from `acus_consumed` against `ACU_CAPS[issue_class]` rather than from any
    status, because Devin enforces `max_acu_limit` itself and the status it stops at is not
    documented (B8).
    """
    if not has_pull_request and session.acus_consumed >= acu_cap_for(issue_class):
        return ACU_CAP_EXHAUSTED
    if session.status is SessionStatus.ERROR:
        return SESSION_ERROR
    if session.status.is_terminal and not has_pull_request:
        return SESSION_ENDED_WITHOUT_PULL_REQUEST
    return None


def observed_triggers(
    session: Session, *, issue_class: str, pr_linked: bool
) -> tuple[tuple[Trigger, str | None], ...]:
    """The triggers one observation carries, in the order they must be applied, each with the
    `blocked_reason` it stores.

    Order is the point: a session first seen at `exit` with a pull request on it has to pass through
    `RUNNING` before `PR_OPENED` is reachable, and `BLOCKED` is applied before `FAILED` so that a
    session which reported *why* it stopped is recorded as blocked rather than as a bare failure.

    `pr_linked` is the remediation's own link. A pull request counts as existing if either side
    knows of one: the session may report it for the first time in this very observation, and an
    already-linked remediation must not be failed by a `pull_requests[]` that has since gone quiet.
    """
    has_pull_request = pr_linked or session.pull_request_url is not None
    triggers: list[tuple[Trigger, str | None]] = []
    if session.status is not SessionStatus.NEW:
        triggers.append((Trigger.SESSION_RUNNING, None))
    if session.pull_request_url is not None:
        triggers.append((Trigger.PR_OPENED, None))
    if (blocked := blocked_reason(session)) is not None:
        triggers.append((Trigger.BLOCKED, blocked))
    failed = failure_reason(session, issue_class=issue_class, has_pull_request=has_pull_request)
    if failed is not None:
        triggers.append((Trigger.FAILED, failed))
    return tuple(triggers)


async def in_flight(db: AsyncSession) -> tuple[InFlight, ...]:
    """Every remediation with a session that could still change, oldest first."""
    result = await db.execute(
        select(Remediation.id, Remediation.devin_session_id)
        .where(Remediation.state.in_(sorted(POLLED_STATES)))
        .order_by(Remediation.id)
    )
    # There is nothing to ask Devin about a remediation with no session id, and this is the only
    # place that is decided — a second `devin_session_id IS NOT NULL` in the statement above would
    # be a rule in two places that could only ever agree by luck. It costs nothing: a remediation
    # past `QUEUED` has a session id, because the worker writes both as it enters
    # `SESSION_CREATED`. It also types the id `str` for the call below.
    return tuple(
        InFlight(remediation_id=remediation_id, session_id=session_id)
        for remediation_id, session_id in result.all()
        if session_id is not None
    )


async def apply(
    db: AsyncSession,
    remediation: Remediation,
    trigger: Trigger,
    *,
    settings: Settings,
    reason: str | None = None,
    pull_request: PullRequest | None = None,
) -> Transition | None:
    """Apply one trigger to `remediation`, returning the transition if it moved.

    `None` means the observation carried nothing new — the trigger was absorbed, or `is_legal` says
    it does not apply from here. Nothing is written in that case, not even an event.
    """
    state = State(remediation.state)
    if trigger in ONLY_WHEN_LEGAL and not is_legal(state, trigger):
        return None

    result = transition(
        state,
        trigger,
        cycle=remediation.cycle,
        pr_linked=remediation.pr_url is not None,
        max_fix_cycles=settings.max_fix_cycles,
    )
    if not result.moved:
        return None

    remediation.state = result.to_state.value
    remediation.cycle = result.cycle
    if result.to_state is State.PR_OPENED and pull_request is not None:
        # Write-once, and this is the only place any of the three is written: the state machine
        # absorbs a second `PR_OPENED`, so a later poll never reaches this line.
        remediation.pr_url = pull_request.url
        remediation.pr_number = pull_request.number
        remediation.pr_opened_at = _now()
    if result.to_state in ESCALATED_STATES:
        remediation.blocked_reason = reason or result.reason
        remediation.closed_at = _now()
        await enqueue(
            db,
            kind=JobKind.ESCALATE,
            payload={"reason": remediation.blocked_reason, "state": result.to_state.value},
            remediation_id=remediation.id,
        )

    db.add(_event(remediation, result, reason))
    log.info(
        "poller.remediation.moved",
        from_state=result.from_state,
        to_state=result.to_state.value,
        trigger=result.trigger.value,
        reason=remediation.blocked_reason,
    )
    return result


async def reconcile(
    db: AsyncSession, remediation: Remediation, session: Session, *, settings: Settings
) -> tuple[Transition, ...]:
    """Bring one remediation into line with one observation of its session.

    Columns first — status, ACUs, the structured report — then the triggers the observation carries.
    Both halves are idempotent: an unchanged column is not written, and a trigger that moves nothing
    returns nothing.
    """
    _reconcile_columns(remediation, session)
    pull_request = session.pull_requests[0] if session.pull_requests else None
    observed = observed_triggers(
        session,
        issue_class=remediation.issue_class,
        pr_linked=remediation.pr_url is not None,
    )
    moved: list[Transition] = []
    for trigger, reason in observed:
        result = await apply(
            db, remediation, trigger, settings=settings, reason=reason, pull_request=pull_request
        )
        if result is not None:
            moved.append(result)
    return tuple(moved)


async def poll_once(
    devin: DevinClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
) -> Tick:
    """One tick: read every in-flight remediation, and reconcile what Devin says about it.

    Each remediation is reconciled in a transaction of its own, so one that Devin will not answer
    for does not hold up the rest of the tick or roll back their work.
    """
    async with session_factory() as db:
        pending = await in_flight(db)

    moved = 0
    unreachable = 0
    failed = 0
    for entry in pending:
        with correlate(remediation_id=entry.remediation_id, session_id=entry.session_id):
            try:
                session: Session | None = await devin.get_session(entry.session_id)
            except DevinResponseError as exc:
                # Devin answered with something v3 does not document, and will answer the same way
                # next tick, so this cannot resolve itself by waiting. Escalate it, with the reason
                # in the audit trail, rather than polling it for ever — which is what
                # `DevinResponseError` is separate from `DevinAPIError` in order to allow.
                log.warning("poller.session.unreadable", error=str(exc))
                unreachable += 1
                session = None
            except DevinError as exc:
                log.warning("poller.session.unreachable", error=str(exc))
                unreachable += 1
                continue

            try:
                if session is None:
                    transitions = await _fail(
                        session_factory, entry, SESSION_UNREADABLE, settings=settings
                    )
                else:
                    transitions = await _reconcile_one(
                        session_factory, entry, session, settings=settings
                    )
            except Exception:
                # One row must not end the tick. `in_flight` is ordered by id, so anything escaping
                # here would strand every remediation after it — and `restart: unless-stopped` would
                # then re-poison the process on the same row after every restart, which is harder to
                # diagnose than a stuck remediation. The row keeps the state it had; the next tick
                # tries again.
                log.exception("poller.remediation.failed")
                failed += 1
                continue
            moved += len(transitions)

    log.info(
        "poller.tick", polled=len(pending), moved=moved, unreachable=unreachable, failed=failed
    )
    return Tick(polled=len(pending), moved=moved, unreachable=unreachable, failed=failed)


async def run(
    devin: DevinClient,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    stop: asyncio.Event | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Poll every `POLL_INTERVAL_SECONDS` until `stop` is set, or for ever if it is not given.

    `sleep` is injectable so a test can drive the cadence without waiting for it, exactly as
    `DevinClient` does with its backoff.
    """
    while stop is None or not stop.is_set():
        await poll_once(devin, session_factory, settings=settings)
        await sleep(settings.poll_interval_seconds)


async def main(settings: Settings | None = None) -> None:
    """The `poller` process, as `docs/09-operations.md` runs it."""
    settings = get_settings() if settings is None else settings
    configure_logging(settings)
    log.info("poller.started", interval_seconds=settings.poll_interval_seconds)
    try:
        async with DevinClient(settings) as devin:
            await run(devin, get_session_factory(), settings=settings)
    finally:
        await dispose_engine()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _observed_columns(session: Session) -> dict[str, Any]:
    """The remediation columns this observation carries, under their column names."""
    columns: dict[str, Any] = {
        "devin_status": session.status.value,
        # `str` rather than the float directly: `Decimal(0.1)` is the binary expansion, and this
        # column is `numeric(10,3)` precisely so that ACUs add up exactly.
        "acus_consumed": Decimal(str(session.acus_consumed)),
    }
    if session.url is not None:
        columns["devin_session_url"] = session.url
    if session.structured_output is not None:
        columns["structured_output"] = session.structured_output.model_dump(mode="json")
    return columns


def _reconcile_columns(remediation: Remediation, session: Session) -> None:
    """Write the columns this observation changed, and only those.

    An unchanged column is left alone rather than re-assigned, so a tick that observed nothing new
    leaves the row untouched instead of issuing an `UPDATE` that sets every column to itself.
    """
    for name, value in _observed_columns(session).items():
        if getattr(remediation, name) != value:
            setattr(remediation, name, value)


def _event(remediation: Remediation, result: Transition, reason: str | None) -> RemediationEvent:
    """The one `remediation_event` a transition writes, in the caller's transaction.

    `webhook_delivery_id` is null: nothing GitHub sent caused this, which is the whole point of the
    poller existing.
    """
    detail: dict[str, Any] = {
        "source": SOURCE,
        "trigger": result.trigger.value,
        "devin_session_id": remediation.devin_session_id,
        "devin_status": remediation.devin_status,
        "reason": reason or result.reason,
    }
    return RemediationEvent(
        remediation_id=remediation.id,
        from_state=result.from_state,
        to_state=result.to_state.value,
        kind="transition",
        detail={key: value for key, value in detail.items() if value is not None},
    )


async def _reconcile_one(
    session_factory: async_sessionmaker[AsyncSession],
    entry: InFlight,
    session: Session,
    *,
    settings: Settings,
) -> tuple[Transition, ...]:
    """Reconcile one remediation in a transaction of its own.

    The row is re-read after the Devin call rather than carried across it, so a webhook that moved
    the remediation while the request was in flight is not overwritten with a state read before it.
    """
    async with session_factory() as db:
        remediation = await _load(db, entry.remediation_id)
        moved = await reconcile(db, remediation, session, settings=settings)
        await db.commit()
    return moved


async def _fail(
    session_factory: async_sessionmaker[AsyncSession],
    entry: InFlight,
    reason: str,
    *,
    settings: Settings,
) -> tuple[Transition, ...]:
    """Escalate one remediation whose session cannot be read, in a transaction of its own."""
    async with session_factory() as db:
        remediation = await _load(db, entry.remediation_id)
        result = await apply(db, remediation, Trigger.FAILED, settings=settings, reason=reason)
        await db.commit()
    return () if result is None else (result,)


async def _load(db: AsyncSession, remediation_id: int) -> Remediation:
    # `scalar_one` rather than `get`: the row was selected moments ago and `remediation` is never
    # deleted, so a miss is a fault to raise on rather than a branch to write.
    return (
        await db.execute(select(Remediation).where(Remediation.id == remediation_id))
    ).scalar_one()
