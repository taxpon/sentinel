"""Every figure in `docs/07-observability.md#metric-definitions`, computed from the database.

`summary()` returns the exact JSON body of `GET /api/analytics/summary`: the same field names,
nesting and types as the schema in that document, which the dashboard is already typed against
(`dashboard/src/api.ts`). The API layer serves the dictionary; it computes nothing.

Three properties of the numbers, because a metrics module is only useful if a reader knows what a
figure is a figure *of*:

- **The window selects remediations by `labeled_at`**, half-open `[start, end)`, and every figure is
  computed over that one set — including `throughput`, whose days come from `merged_at` and may
  therefore fall outside the window
  (`docs/adr/2026-08-08-the-window-selects-remediations-by-labeled-at.md`).
- **A figure whose denominator is empty is `0`,** not a null and not a `NaN`: the schema types every
  figure as a number, and the dashboard decides from the funnel counts — which are in the same
  payload — whether a figure exists at all, rendering an em dash where it does not. A zero here is
  therefore never read as a measurement.
- **A percentile is an observed duration,** the value at nearest rank, never interpolated
  (`docs/adr/2026-08-08-percentiles-are-nearest-rank-observations.md`).

Durations come from the denormalised timestamps on `remediation`, and fix cycles from the
append-only `remediation_event` log, exactly as
`docs/adr/2026-08-07-transitions-are-append-only-events.md` intends. The rows of one window are read
into memory and reduced in Python rather than aggregated in SQL: the workload is tens of
remediations, and every formula in the definitions table then reads the way it is written there,
which is the property the hand-computed tests are checking.
"""

from __future__ import annotations

import datetime
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.config import Settings
from sentinel.devin.playbooks import baseline_hours_for
from sentinel.models import AcuLedger, Remediation, RemediationEvent
from sentinel.pipeline.state import State

# --- The response schema -------------------------------------------------------------------------

# `from` is a keyword, so this one is written functionally. The key is part of the published schema
# and the dashboard reads it by that name.
WindowJson = TypedDict("WindowJson", {"from": str, "to": str})


class FunnelJson(TypedDict):
    labelled: int
    session_created: int
    pr_opened: int
    ci_green: int
    merged: int


class RatesJson(TypedDict):
    success: float
    merge: float
    autonomy: float


class PercentilesJson(TypedDict):
    p50: int
    p90: int


class DurationsJson(TypedDict):
    to_pr: PercentilesJson
    to_merge: PercentilesJson
    review_latency: PercentilesJson


CostSource = Literal["devin_consumption_api", "derived"]
"""Whether Devin's own consumption figures cover the window, or Sentinel derived the ACU totals."""


class CostJson(TypedDict):
    acus_total: float
    acus_per_merged_fix: float
    usd_per_fix: float
    unit_cost_usd: float
    source: CostSource


class CyclesJson(TypedDict):
    mean: float
    distribution: dict[str, int]


class ThroughputDayJson(TypedDict):
    day: str
    by_class: dict[str, int]


class FailureBucketJson(TypedDict):
    reason: str
    count: int
    issues: list[int]


class ImpactJson(TypedDict):
    hours_saved: float
    assumption: str


class SummaryJson(TypedDict):
    window: WindowJson
    funnel: FunnelJson
    rates: RatesJson
    durations_seconds: DurationsJson
    cost: CostJson
    cycles: CyclesJson
    throughput: list[ThroughputDayJson]
    failures: list[FailureBucketJson]
    impact: ImpactJson
    generated_at: str


# --- Constants -----------------------------------------------------------------------------------

DEFAULT_WINDOW: Final = "7d"
"""What the dashboard asks for when nothing else is specified."""

WINDOW_SPEC: Final = re.compile(r"(\d+)([dh])")
WINDOW_UNITS: Final[dict[str, datetime.timedelta]] = {
    "d": datetime.timedelta(days=1),
    "h": datetime.timedelta(hours=1),
}

# The two edges of the review-fix loop in `docs/04-state-machine.md`. A fix cycle is one traversal
# of either, which the log records as an entry into RUNNING from the state that triggered it.
LOOP_FROM_STATES: Final = (State.CI_FAILED.value, State.CHANGES_REQUESTED.value)

FAILURE_STATES: Final = frozenset({State.BLOCKED.value, State.FAILED.value})

UNSPECIFIED_FAILURE_REASON: Final = "unspecified"
"""Bucket for a terminal remediation with no `blocked_reason`.

Escalation always stores one, so this bucket appearing on the dashboard is itself the finding: a
failure nobody attributed. It is shown rather than dropped, for the reason
`docs/07-observability.md` gives for showing escalations at all.
"""

IMPACT_ASSUMPTION: Final = "baseline hours per issue class; see docs/05"
"""Rendered inline by the impact panel. Hours saved is a stated assumption, not a measurement."""

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"

RATE_DIGITS: Final = 3
"""Rates are fractions the dashboard renders as whole percents; three digits is a tenth of one."""

ACU_DIGITS: Final = 3
"""`numeric(10,3)`. Rounding the window's ACU total at the column's own precision loses nothing."""

DISPLAY_DIGITS: Final = 1
"""Per-fix and per-remediation figures, rounded once here so that no two panels disagree."""


# --- The window ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    """The half-open interval `[start, end)` a summary covers, on `remediation.labeled_at`."""

    start: datetime.datetime
    end: datetime.datetime


def parse_window(spec: str = DEFAULT_WINDOW, *, now: datetime.datetime | None = None) -> Window:
    """`"7d"` or `"12h"` into the window ending now that it names.

    Raises `ValueError` for anything else, so the API answers a malformed query parameter with a
    client error rather than with a window nobody asked for.
    """
    match = WINDOW_SPEC.fullmatch(spec)
    if match is None:
        raise ValueError(
            f"window must be a count of days or hours, such as {DEFAULT_WINDOW!r}, got {spec!r}"
        )
    length = int(match[1]) * WINDOW_UNITS[match[2]]
    if not length:
        raise ValueError(f"window must cover more than no time at all, got {spec!r}")
    end = _now(now)
    return Window(start=end - length, end=end)


# --- Percentiles ---------------------------------------------------------------------------------


def percentile(values: Sequence[float], rank: int) -> float:
    """The `rank`-th percentile of `values` by nearest rank — an observed value, never interpolated.

    The value at position `ceil(rank / 100 * n)` of the sorted sample, so at least `rank` percent of
    the observations are at or below what is reported. Empty input is `0.0`, the schema's
    representation of a figure that does not exist.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    position = math.ceil(rank / 100 * len(ordered))
    return ordered[max(position, 1) - 1]


# --- The summary ---------------------------------------------------------------------------------


async def summary(
    session: AsyncSession,
    window: Window,
    settings: Settings,
    *,
    now: datetime.datetime | None = None,
) -> SummaryJson:
    """The whole `GET /api/analytics/summary` body for `window`."""
    remediations = await _remediations_in(session, window)
    fix_cycles = await _fix_cycle_counts(session, [row.id for row in remediations])
    ledger_days = await _synced_ledger_days(session, window)

    # Paired with the timestamp that selected it, so that the merged-only figures below are typed as
    # having one rather than re-testing for null at every use.
    merged = [(row, row.merged_at) for row in remediations if row.merged_at is not None]

    funnel: FunnelJson = {
        "labelled": len(remediations),
        "session_created": sum(row.session_created_at is not None for row in remediations),
        "pr_opened": sum(row.pr_opened_at is not None for row in remediations),
        "ci_green": sum(row.ci_green_at is not None for row in remediations),
        "merged": len(merged),
    }

    return {
        "window": {"from": _format(window.start), "to": _format(window.end)},
        "funnel": funnel,
        "rates": _rates(funnel, merged),
        "durations_seconds": _durations(remediations),
        "cost": _cost(remediations, funnel["merged"], settings, ledger_days, window),
        "cycles": _cycles(remediations, fix_cycles),
        "throughput": _throughput(merged),
        "failures": _failures(remediations),
        "impact": _impact(merged),
        "generated_at": _format(_now(now)),
    }


# --- Queries -------------------------------------------------------------------------------------


async def _remediations_in(session: AsyncSession, window: Window) -> Sequence[Remediation]:
    """Every remediation labelled in the window, oldest first."""
    return (
        await session.scalars(
            select(Remediation)
            .where(Remediation.labeled_at >= window.start, Remediation.labeled_at < window.end)
            .order_by(Remediation.labeled_at, Remediation.id)
        )
    ).all()


async def _fix_cycle_counts(session: AsyncSession, ids: Sequence[int]) -> Counter[int]:
    """How many fix cycles each remediation needed, keyed by remediation id.

    Every row of the log is counted, including rows sharing a `created_at` to the microsecond:
    `remediation_event.created_at` is `transaction_timestamp()` and `docs/06-event-pipeline.md`
    writes the transition, the event and the job together, so ties are routine rather than
    exceptional. The order is fixed on `(created_at, id)` for the same reason — timestamps alone do
    not order rows written in one transaction.
    """
    events = await session.scalars(
        select(RemediationEvent)
        .where(
            RemediationEvent.remediation_id.in_(ids),
            RemediationEvent.to_state == State.RUNNING.value,
            RemediationEvent.from_state.in_(LOOP_FROM_STATES),
        )
        .order_by(RemediationEvent.created_at, RemediationEvent.id)
    )
    return Counter(event.remediation_id for event in events)


async def _synced_ledger_days(session: AsyncSession, window: Window) -> set[datetime.date]:
    """The days of the window for which `acu_ledger` holds Devin's consumption figures."""
    days = _window_days(window)
    if not days:
        return set()
    rows = await session.scalars(
        select(AcuLedger.day).where(AcuLedger.day >= days[0], AcuLedger.day <= days[-1])
    )
    return set(rows)


# --- Figures -------------------------------------------------------------------------------------

MergedRemediations = Sequence[tuple[Remediation, datetime.datetime]]


def _rates(funnel: FunnelJson, merged: MergedRemediations) -> RatesJson:
    """`merged / labelled`, `merged / pr_opened`, and the share of merges nobody touched."""
    autonomous = sum(row.cycle == 0 and row.human_message_count == 0 for row, _ in merged)
    return {
        "success": _ratio(funnel["merged"], funnel["labelled"], RATE_DIGITS),
        "merge": _ratio(funnel["merged"], funnel["pr_opened"], RATE_DIGITS),
        "autonomy": _ratio(autonomous, funnel["merged"], RATE_DIGITS),
    }


def _durations(remediations: Sequence[Remediation]) -> DurationsJson:
    """Time to PR, MTTR and review latency, over the remediations that reached each stage.

    A remediation contributes to a duration only where both of its timestamps exist, so a window in
    which nothing merged has no MTTR rather than an MTTR built from the ones that did not.
    """
    return {
        "to_pr": _percentiles(
            [
                _seconds(row.pr_opened_at, row.labeled_at)
                for row in remediations
                if row.pr_opened_at is not None
            ]
        ),
        "to_merge": _percentiles(
            [
                _seconds(row.merged_at, row.labeled_at)
                for row in remediations
                if row.merged_at is not None
            ]
        ),
        "review_latency": _percentiles(
            [
                _seconds(row.merged_at, row.ci_green_at)
                for row in remediations
                if row.merged_at is not None and row.ci_green_at is not None
            ]
        ),
    }


def _cost(
    remediations: Sequence[Remediation],
    merged: int,
    settings: Settings,
    ledger_days: set[datetime.date],
    window: Window,
) -> CostJson:
    """ACU spend, ACUs per merged fix, and the dollar conversion of the second.

    Both per-fix figures are taken from the unrounded total and rounded once, so `usd_per_fix` is
    the cost of the ACUs actually spent rather than the product of two already-rounded numbers.
    """
    total = sum((row.acus_consumed for row in remediations), Decimal(0))
    per_fix = float(total) / merged if merged else 0.0
    return {
        "acus_total": float(round(total, ACU_DIGITS)),
        "acus_per_merged_fix": round(per_fix, DISPLAY_DIGITS),
        "usd_per_fix": round(per_fix * settings.acu_unit_cost_usd, DISPLAY_DIGITS),
        "unit_cost_usd": settings.acu_unit_cost_usd,
        "source": _cost_source(window, ledger_days),
    }


def _cost_source(window: Window, ledger_days: set[datetime.date]) -> CostSource:
    """Whether Devin's consumption figures cover every day of the window.

    A ledger missing a day cannot vouch for the window, so the totals are labelled as Sentinel's own
    — which is what `docs/05-devin-integration.md#degradation` asks the dashboard to say.
    """
    days = _window_days(window)
    return "devin_consumption_api" if days and set(days) <= ledger_days else "derived"


def _cycles(remediations: Sequence[Remediation], fix_cycles: Counter[int]) -> CyclesJson:
    """Mean fix cycles per remediation, and the distribution behind the mean.

    Over every remediation in the window, not only the merged ones: a remediation that looped three
    times and was then abandoned is exactly the case the mean exists to surface.
    """
    counts = [fix_cycles[row.id] for row in remediations]
    distribution = Counter(counts)
    return {
        "mean": _ratio(sum(counts), len(counts), DISPLAY_DIGITS),
        "distribution": {str(count): distribution[count] for count in sorted(distribution)},
    }


def _throughput(merged: MergedRemediations) -> list[ThroughputDayJson]:
    """Merges per UTC day, split by issue class. Days with no merge do not appear."""
    by_day: defaultdict[datetime.date, Counter[str]] = defaultdict(Counter)
    for row, merged_at in merged:
        by_day[merged_at.astimezone(datetime.UTC).date()][row.issue_class] += 1
    return [
        {"day": day.isoformat(), "by_class": dict(sorted(counts.items()))}
        for day, counts in sorted(by_day.items())
    ]


def _failures(remediations: Sequence[Remediation]) -> list[FailureBucketJson]:
    """`BLOCKED` and `FAILED` grouped by reason, largest bucket first, then by reason."""
    issues: defaultdict[str, list[int]] = defaultdict(list)
    for row in remediations:
        if row.state in FAILURE_STATES:
            issues[row.blocked_reason or UNSPECIFIED_FAILURE_REASON].append(row.issue_number)
    buckets: list[FailureBucketJson] = [
        {"reason": reason, "count": len(numbers), "issues": sorted(numbers)}
        for reason, numbers in issues.items()
    ]
    buckets.sort(key=lambda bucket: (-bucket["count"], bucket["reason"]))
    return buckets


def _impact(merged: MergedRemediations) -> ImpactJson:
    """`sum(baseline_hours[class] * merged_in_class)`, from the playbook table in `docs/05`."""
    hours = sum(baseline_hours_for(row.issue_class) for row, _ in merged)
    return {"hours_saved": round(hours, DISPLAY_DIGITS), "assumption": IMPACT_ASSUMPTION}


# --- Arithmetic ----------------------------------------------------------------------------------


def _ratio(numerator: float, denominator: float, digits: int) -> float:
    """A figure that does not exist is `0`; see the module docstring for why that is safe."""
    return round(numerator / denominator, digits) if denominator else 0.0


def _percentiles(seconds: Sequence[float]) -> PercentilesJson:
    return {"p50": round(percentile(seconds, 50)), "p90": round(percentile(seconds, 90))}


def _seconds(later: datetime.datetime, earlier: datetime.datetime) -> float:
    return (later - earlier).total_seconds()


def _window_days(window: Window) -> list[datetime.date]:
    """The UTC dates the half-open window touches. An end at midnight does not touch that day."""
    if window.end <= window.start:
        return []
    first = window.start.astimezone(datetime.UTC).date()
    last = (window.end - datetime.timedelta(microseconds=1)).astimezone(datetime.UTC).date()
    return [first + datetime.timedelta(days=offset) for offset in range((last - first).days + 1)]


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) if now is None else now


def _format(moment: datetime.datetime) -> str:
    """Seconds precision and a `Z`, the shape the schema's own examples are written in."""
    return moment.astimezone(datetime.UTC).strftime(TIMESTAMP_FORMAT)
