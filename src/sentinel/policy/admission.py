"""What the worker decides before it spends anything: may this `create_session` job run?

`admit_session()` is the single call `pipeline/handlers.py` makes at the top of the `create_session`
handler. It returns a `Decision`, and **it has already released the job for every decision except
`ADMITTED`** — deferred, or completed, according to what the decision means. The handler's contract
is one line:

    decision = await admit_session(session, job=job, remediation=remediation, devin=devin)
    if not decision.admitted:
        return

Nothing here commits, exactly as in `sentinel.queue`: the refusal, the state change, the
`remediation_event` and the escalation job are written in the caller's transaction, so a `BLOCKED`
remediation and the job that escalates it are durable together or not at all.

`docs/adr/2026-08-08-the-policy-releases-the-job-it-refuses.md` records why the policy performs the
ending rather than handing a verdict back, and what it costs.

The four ways in are checked in a fixed order, and the order is the point:

1. **The remediation is already terminal.** A `create_session` job outlives the issue being closed
   or unlabelled; the work was cancelled while the job sat in the queue. Nothing to spend on.
2. **A session already exists.** `docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md`
   says the lease fences the row and not the work, so a slow worker's job can be reclaimed and its
   handler run twice — two `POST /v3/…/sessions` for one issue, two ACU budgets, one orphan Devin
   will never tell us about. This is the check that ADR requires of every handler with an external
   effect, and it is also the domain deduplication layer arriving at its real destination: a second
   event about one issue does not open a second session.

   **It narrows that window; it does not close it.** The check reads committed state, so it can
   only see a session the first worker has already committed. A worker that posts, writes
   `devin_session_id` and *then* calls `complete()` learns there that its lease expired:
   `LeaseLost` rolls its transaction back, the id never commits, and the reclaimer reads `None` and
   posts again. **The handler must therefore commit `devin_session_id` on its own, as soon as the
   `POST` returns and before it does anything else.** How wide the remaining window is, is decided
   by that commit and by nothing in this module.
3. **The daily ACU budget.** A *terminal* verdict — waiting does not make an exhausted budget
   affordable — so it is decided before the temporary one. The other order would let a saturated
   queue postpone the escalation indefinitely, leaving the issue silently `QUEUED` while an
   operator has nothing to look at. The cost is one consumption call per deferral round.
4. **The concurrency cap.** A *temporary* verdict: the work is still wanted and nothing about it
   has failed, so the job is deferred rather than failed and `attempts` is left alone
   (`docs/06-event-pipeline.md#reliability-policy` is explicit, and `sentinel.queue.defer()` is
   what implements it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import queue
from sentinel.config import Settings, get_settings
from sentinel.models import Remediation, RemediationEvent
from sentinel.observability.logging import get_logger
from sentinel.pipeline.state import TERMINAL_STATES, State, Trigger, transition
from sentinel.policy.budget import BUDGET_EXHAUSTED, ConsumptionSource, budget_allows, daily_spend
from sentinel.policy.concurrency import capacity
from sentinel.queue import ClaimedJob, JobKind

log = get_logger(__name__)

SESSION_ALREADY_CREATED: Final = "session_already_created"
"""The remediation already carries a `devin_session_id`. Not an error — the second run of a
handler whose first run succeeded, or a second event about an issue already being worked on."""

REMEDIATION_TERMINAL: Final = "remediation_terminal"
"""The remediation reached `MERGED`, `BLOCKED` or `FAILED` while the job waited in the queue."""

CONCURRENCY_CAP_REACHED: Final = "max_concurrent_sessions_reached"
"""Held back by the concurrency gate. Recorded on the decision, never on the remediation: nothing
about the remediation has changed, and it is still `QUEUED`."""

POLICY_EVENT: Final = "policy"
"""`remediation_event.kind` for a transition a policy limit forced, from the vocabulary in
`docs/03-data-model.md`."""


class Verdict(StrEnum):
    """What the policy decided, and — for everything but `ADMITTED` — what it already did."""

    ADMITTED = "admitted"
    """Go ahead. The job is still held by this worker, which must complete or fail it as usual."""

    DUPLICATE = "duplicate"
    """A session already exists for this remediation. The job has been completed; do not post."""

    CANCELLED = "cancelled"
    """The remediation is terminal. The job has been completed; there is nothing to do."""

    DEFERRED = "deferred"
    """At the concurrency cap. The job has been deferred and will be claimed again in a minute,
    with its retry budget untouched."""

    BLOCKED = "blocked"
    """Over the daily ACU budget. The remediation is `BLOCKED`, an `escalate` job is enqueued and
    the job has been completed."""


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict, why, and the figures it was reached on — for the log line and the tests."""

    verdict: Verdict
    reason: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def admitted(self) -> bool:
        return self.verdict is Verdict.ADMITTED


async def admit_session(
    session: AsyncSession,
    *,
    job: ClaimedJob,
    remediation: Remediation,
    devin: ConsumptionSource,
    settings: Settings | None = None,
) -> Decision:
    """Decide whether this `create_session` job may create a session, and act on the decision.

    `remediation` is loaded in `session` and is mutated in place when the budget guard refuses.
    Raises `LeaseLost` if the job has been reclaimed by another worker in the meantime, and
    `UnknownIssueClass` for a class with no playbook — the caller has to handle that one anyway,
    since such a class has no `playbook_id` to create a session with.
    """
    settings = get_settings() if settings is None else settings

    if State(remediation.state) in TERMINAL_STATES:
        cancelled = Decision(Verdict.CANCELLED, REMEDIATION_TERMINAL, {"state": remediation.state})
        return await _retire(session, job, remediation, cancelled)

    if remediation.devin_session_id is not None:
        duplicate = Decision(
            Verdict.DUPLICATE,
            SESSION_ALREADY_CREATED,
            {"devin_session_id": remediation.devin_session_id},
        )
        return await _retire(session, job, remediation, duplicate)

    spend = await daily_spend(session, devin=devin)
    if not budget_allows(spend, issue_class=remediation.issue_class, settings=settings):
        detail = {**spend.as_detail(), "acu_budget": settings.daily_acu_budget}
        await block(session, remediation=remediation, reason=BUDGET_EXHAUSTED, detail=detail)
        blocked = Decision(Verdict.BLOCKED, BUDGET_EXHAUSTED, detail)
        return await _retire(session, job, remediation, blocked)

    gate = await capacity(session, settings=settings)
    if not gate.available:
        detail = {"in_flight": gate.in_flight, "max_concurrent_sessions": gate.limit}
        await queue.defer(session, job)
        return _decided(
            remediation, job, Decision(Verdict.DEFERRED, CONCURRENCY_CAP_REACHED, detail)
        )

    return _decided(remediation, job, Decision(Verdict.ADMITTED))


async def block(
    session: AsyncSession,
    *,
    remediation: Remediation,
    reason: str,
    detail: Mapping[str, Any] | None = None,
) -> bool:
    """Move a remediation to `BLOCKED` for a policy reason and enqueue its escalation.

    The state column, the `remediation_event` and the `escalate` job are written in the caller's
    transaction, which is invariant 4 of `docs/04-state-machine.md` — the log can never disagree
    with the column. `BLOCKED` escalates rather than terminating quietly, so the `escalate` job
    carrying `reason` is part of the transition and not a separate decision by the caller.

    Returns whether the remediation moved. A terminal remediation absorbs the trigger, as it
    absorbs every other, and nothing is written.

    Public because `BLOCKED` has more than one cause: an issue class with no playbook and a session
    reporting `outcome: blocked` reach the same state by the same route, and the writes it takes to
    get there should not be spelled out twice.
    """
    outcome = transition(State(remediation.state), Trigger.BLOCKED)
    if not outcome.moved:
        return False
    remediation.state = str(outcome.to_state)
    remediation.blocked_reason = reason
    session.add(
        RemediationEvent(
            remediation_id=remediation.id,
            from_state=outcome.from_state,
            to_state=str(outcome.to_state),
            kind=POLICY_EVENT,
            detail={"reason": reason, **(detail or {})},
        )
    )
    await queue.enqueue(
        session,
        kind=JobKind.ESCALATE,
        payload={"reason": reason, **(detail or {})},
        remediation_id=remediation.id,
    )
    return True


async def _retire(
    session: AsyncSession, job: ClaimedJob, remediation: Remediation, decision: Decision
) -> Decision:
    """Complete a job that will never run: the decision is final, so there is nothing to retry."""
    await queue.complete(session, job)
    return _decided(remediation, job, decision)


def _decided(remediation: Remediation, job: ClaimedJob, decision: Decision) -> Decision:
    log.info(
        "policy.decision",
        verdict=decision.verdict.value,
        reason=decision.reason,
        remediation_id=remediation.id,
        issue_number=remediation.issue_number,
        job_id=job.id,
        **decision.detail,
    )
    return decision
