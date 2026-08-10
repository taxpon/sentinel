"""Every figure in `docs/07-observability.md#metric-definitions`, computed from the database.

`summary()` returns the exact JSON body of `GET /api/analytics/summary`: the same field names,
nesting and types as the schema in that document, which the dashboard is already typed against
(`dashboard/src/api.ts`). The API layer serves the dictionary; it computes nothing.

Four properties of the numbers, because a metrics module is only useful if a reader knows what a
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
- **Every figure is independent of the order the rows arrive in.** Each is a count, a sum, a
  `Counter` or a percentile over an internally sorted sample, and each list in the payload sorts
  itself before it is returned. That is worth stating rather than leaving to be re-derived: the
  usual reason to care about ordering here is that `remediation_event.created_at` is
  `transaction_timestamp()`, so rows written in one transaction are indistinguishable by time
  (`docs/06-event-pipeline.md`) — and the risk that creates for this module is a tied row being
  *collapsed*, never one being read out of order. Neither query orders its rows, because no
  reduction below would notice if it did.

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
from typing import Final, TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.devin.playbooks import baseline_hours_for
from sentinel.models import Remediation, RemediationEvent
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

MAX_WINDOW: Final = datetime.timedelta(days=365)
"""The longest window that can be asked for.

A ceiling is needed at all because the count in a window spec is unbounded digits arriving straight
from a query string, and `datetime` raises `OverflowError` — not `ValueError` — somewhere past
`2739726d`, which the API would surface as a 500 rather than as the client error a malformed
parameter deserves. A year is where it is set because that is the range the dashboard's daily series
can plot and rather more than the retention anyone has asked for.
"""

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

DISPLAY_DIGITS: Final = 1
"""Per-remediation figures, rounded once here so that no two panels disagree."""


# --- The window ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    """The half-open interval `[start, end)` a summary covers, on `remediation.labeled_at`."""

    start: datetime.datetime
    end: datetime.datetime


def parse_window(spec: str = DEFAULT_WINDOW, *, now: datetime.datetime | None = None) -> Window:
    """`"7d"` or `"12h"` into the window ending now that it names.

    Raises `ValueError` — and only `ValueError` — for anything else, so the API answers a malformed
    query parameter with a client error rather than with a window nobody asked for. That includes
    the counts too large to be a `timedelta` at all, which is why the ceiling below is compared in
    units rather than by building the interval and looking at it.
    """
    match = WINDOW_SPEC.fullmatch(spec)
    if match is None:
        raise ValueError(
            f"window must be a count of days or hours, such as {DEFAULT_WINDOW!r}, got {spec!r}"
        )
    count, unit = int(match[1]), WINDOW_UNITS[match[2]]
    if not count:
        raise ValueError(f"window must cover more than no time at all, got {spec!r}")
    if count > MAX_WINDOW // unit:
        raise ValueError(f"window must be at most {MAX_WINDOW.days} days, got {spec!r}")
    end = _now(now)
    return Window(start=end - count * unit, end=end)


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
    *,
    now: datetime.datetime | None = None,
) -> SummaryJson:
    """The whole `GET /api/analytics/summary` body for `window`."""
    remediations = await _remediations_in(session, window)
    fix_cycles = await _fix_cycle_counts(session, [row.id for row in remediations])

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
        "cycles": _cycles(remediations, fix_cycles),
        "throughput": _throughput(merged),
        "failures": _failures(remediations),
        "impact": _impact(merged),
        "generated_at": _format(_now(now)),
    }


# --- Queries -------------------------------------------------------------------------------------


async def _remediations_in(session: AsyncSession, window: Window) -> Sequence[Remediation]:
    """Every remediation labelled in the window, in no particular order.

    Unordered deliberately: every reduction below is a count, a sum or a sort of its own, so an
    `ORDER BY` here would be a cost with no observable effect — see the module docstring.
    """
    return (
        await session.scalars(
            select(Remediation).where(
                Remediation.labeled_at >= window.start, Remediation.labeled_at < window.end
            )
        )
    ).all()


async def _fix_cycle_counts(session: AsyncSession, ids: Sequence[int]) -> Counter[int]:
    """How many fix cycles each remediation needed, keyed by remediation id.

    **Every** row of the log is counted, including rows sharing a `created_at` to the microsecond:
    `remediation_event.created_at` is `transaction_timestamp()` and `docs/06-event-pipeline.md`
    writes the transition, the event and the job together, so two laps of the loop recorded in one
    transaction are indistinguishable by time. Counting rows rather than reading the log in order is
    what makes that harmless — a tie can only hurt a reader that deduplicates or orders on the
    timestamp, and this one does neither.

    `remediation.cycle` is not consulted. The column is the state machine's own counter and the
    metric is defined over the log, so a disagreement between the two shows up here rather than
    being papered over.
    """
    events = await session.scalars(
        select(RemediationEvent).where(
            RemediationEvent.remediation_id.in_(ids),
            RemediationEvent.to_state == State.RUNNING.value,
            RemediationEvent.from_state.in_(LOOP_FROM_STATES),
        )
    )
    return Counter(event.remediation_id for event in events)


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

    Note what that makes review latency's population: merged **and** green, which is not one of the
    funnel counts, while the dashboard's duration panel uses `funnel.merged` to decide whether the
    figure exists. The two agree because `MERGED` is reachable only through `IN_REVIEW`, which is
    entered only from `CI_PASSED` — a merge without a `ci_green_at` cannot be produced by the state
    machine in `docs/04-state-machine.md`. If one ever were, this returns `0` for its window and the
    panel would render that zero as a measured review latency.
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
    """Merges per UTC day, split by issue class. Days with no merge do not appear.

    `astimezone` is belt-and-braces rather than a conversion this path exercises: asyncpg returns
    every `timestamptz` already normalised to UTC, so through the database the call cannot change a
    date. It stays because the offset is what decides which day a merge falls on, and that should
    not depend on a driver's choice of representation.
    """
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
    """`sum(baseline_hours[class] * merged_in_class)`, from the playbook table in `docs/05`.

    `baseline_hours_for` raises `UnknownIssueClass` rather than defaulting, so this line rests on an
    invariant: a class with no playbook never merges. `docs/04-state-machine.md` routes an
    unrecognised class `QUEUED -> BLOCKED` at session creation, which is a terminal state, and only
    merged remediations are summed here. The invariant is worth naming because the blast radius is
    the whole payload — one unmergeable row would take out every panel, not the impact one.
    """
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


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) if now is None else now


def _format(moment: datetime.datetime) -> str:
    """Seconds precision and a `Z`, the shape the schema's own examples are written in."""
    return moment.astimezone(datetime.UTC).strftime(TIMESTAMP_FORMAT)
