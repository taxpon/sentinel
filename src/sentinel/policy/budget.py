"""The daily ACU budget guard: what today has cost, and whether one more session fits in it.

`docs/06-event-pipeline.md#reliability-policy` compares today's spend plus the issue class's
`max_acu_limit` against `DAILY_ACU_BUDGET`, and over budget the remediation is `BLOCKED` with reason
`daily_acu_budget_exhausted` rather than quietly held back — "a cost ceiling should be visible, not
silent".

Three figures describe today's spend, and the guard uses the largest of them
(`docs/adr/2026-08-08-the-budget-guard-spends-against-the-highest-figure-any-source-reports.md`):

- **Devin's own**, from `daily_consumption()`. Authoritative when it can be read, and absent
  altogether when the organisation does not expose consumption — the endpoint degrades to
  `Unavailable` rather than raising
  (`docs/adr/2026-08-08-enterprise-degradation-is-a-returned-value.md`). It is also *unverified*
  (B8): the window it reports is undocumented, so it can silently be a figure for some other
  period.
- **`acu_ledger`**, the same endpoint's answer as the `sync_acu` job last stored it. Organisation-
  wide and day-aligned, therefore the only figure that covers Devin sessions Sentinel did not
  create — and as stale as the last sync.
- **`remediation.acus_consumed`**, summed over the remediations labelled today. Sentinel's own
  reconciliation, always available and always current, and blind to anything but its own sessions.

None of the three dominates the others, and every one of them can only be *missing* spend, never
inventing it. Taking the maximum is therefore the only combination that cannot open the gate on a
figure that is merely incomplete. A budget guard that reads low spends real money.

Reading Devin's figure is the one thing here that can fail outright — a `401`, a `503`, a network
error all raise `DevinError` from the client and out of `daily_spend()`. That is deliberate: those
are faults to fix, not capabilities to work around, and the worker's retry with backoff is the right
answer. Only the degradations the client already models as `Unavailable` drop the figure.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.config import Settings, get_settings
from sentinel.devin.playbooks import IssueClass, acu_cap_for
from sentinel.devin.schemas import Consumption, Degradable, Unavailable
from sentinel.models import AcuLedger, Remediation

BUDGET_EXHAUSTED: Final = "daily_acu_budget_exhausted"
"""`remediation.blocked_reason` when the guard refuses, spelled as `docs/06-event-pipeline.md`
spells it. The escalation comment and the dashboard's failure breakdown both read it."""


class ConsumptionSource(Protocol):
    """The one method the guard needs of `sentinel.devin.client.DevinClient`.

    Narrowed to a protocol so that this module states its dependency rather than importing the
    whole client, and so that a caller with no Devin client at hand — a report, a script — can
    supply the figure another way.
    """

    async def daily_consumption(self) -> Degradable[Consumption]: ...


@dataclass(frozen=True, slots=True)
class DailySpend:
    """What each source says today has cost, and what the guard spends against.

    All three are carried rather than collapsed on the way in, because they are what the escalation
    comment and the `remediation_event` need in order to explain a refusal: `devin` being `None` is
    how an operator reads "the consumption endpoint could not be asked" off the audit trail months
    later, and a `ledger` far below `remediations` says the `sync_acu` job has stopped running.
    """

    day: datetime.date
    devin: Decimal | None
    ledger: Decimal
    remediations: Decimal

    @property
    def acus(self) -> Decimal:
        """The figure the guard compares against the budget: the largest any source reports."""
        figures = (self.devin, self.ledger, self.remediations)
        return max(figure for figure in figures if figure is not None)

    def as_detail(self) -> dict[str, object]:
        """The spend as it is written to `remediation_event.detail` and the escalation payload."""
        return {
            "day": self.day.isoformat(),
            "acus_spent": float(self.acus),
            "acus_devin": None if self.devin is None else float(self.devin),
            "acus_ledger": float(self.ledger),
            "acus_remediations": float(self.remediations),
        }


async def daily_spend(
    session: AsyncSession, *, devin: ConsumptionSource, day: datetime.date | None = None
) -> DailySpend:
    """Today's ACU spend, according to each of the three sources.

    `day` defaults to today in UTC, which is the day the ledger is keyed by and the day
    `labeled_at` is bucketed into.
    """
    day = datetime.datetime.now(datetime.UTC).date() if day is None else day
    consumption = await devin.daily_consumption()
    reported = (
        None if isinstance(consumption, Unavailable) else _decimal(consumption.value.acus_on(day))
    )
    return DailySpend(
        day=day,
        devin=reported,
        ledger=await _ledger_total(session, day),
        remediations=await _remediation_total(session, day),
    )


def budget_allows(
    spend: DailySpend, *, issue_class: str | IssueClass, settings: Settings | None = None
) -> bool:
    """Whether a session on this issue class fits inside what is left of the daily budget.

    The class's `max_acu_limit` is what the session could cost at worst, and the spec adds it to
    the spend *before* comparing: the guard refuses a session it could not afford to see run to its
    own ceiling, rather than starting one and discovering the budget mid-flight. Spending exactly
    the budget is inside it; only more than the budget is over.

    Raises `UnknownIssueClass` for a class with no playbook — a condition the caller already has to
    handle, since the same class has no `playbook_id` to create a session with either.
    """
    settings = get_settings() if settings is None else settings
    cap = Decimal(acu_cap_for(issue_class))
    return spend.acus + cap <= _decimal(settings.daily_acu_budget)


def _decimal(value: float) -> Decimal:
    """A float figure as an exact decimal. Via `str`, so `0.1` is a tenth and not its binary
    neighbour — the comparison above is decided at the boundary by tests, and `numeric(10,3)` is
    what the two database figures already are."""
    return Decimal(str(value))


async def _ledger_total(session: AsyncSession, day: datetime.date) -> Decimal:
    """`acu_ledger` for one day. A day the sync has not written is a day it can vouch for nothing
    about, which is zero here and covered by the other two sources."""
    total: Decimal | None = (
        await session.execute(select(AcuLedger.acus).where(AcuLedger.day == day))
    ).scalar_one_or_none()
    return Decimal(0) if total is None else total


async def _remediation_total(session: AsyncSession, day: datetime.date) -> Decimal:
    """`sum(acus_consumed)` over the remediations labelled on `day`.

    Selected by `labeled_at`, as
    `docs/adr/2026-08-08-the-window-selects-remediations-by-labeled-at.md` selects them for the
    analytics window: it is the only timestamp every remediation has. The approximation it makes is
    that a session labelled yesterday and still running today is counted against yesterday — spend
    the ledger does cover, on the day it is synced.
    """
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)
    total: Decimal = (
        await session.execute(
            select(func.coalesce(func.sum(Remediation.acus_consumed), 0)).where(
                Remediation.labeled_at >= start,
                Remediation.labeled_at < start + datetime.timedelta(days=1),
            )
        )
    ).scalar_one()
    return total
