"""Every metric in `docs/07-observability.md`, against values computed by hand from the fixture.

`docs/08-testing.md` asks for exactly that — "funnel, rates, p50/p90, cycle counts, cost, autonomy
rate over a fixture event log with hand-computed expected values" — and the reason is worth stating
where the tests are: these are the figures an engineering leader is asked to trust, so a test that
asserts whatever the implementation returned would confirm nothing at all. Every expectation below
is written out with the arithmetic that produced it, from the definitions table in that document.

`FIXTURE` is one seven-day window holding a remediation at each stage of the funnel: four merged,
one awaiting review, one looping on CI, one blocked, one failed. Two more remediations sit outside
the window on either side, so a change that widens the scope fails here rather than on a dashboard.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from factories import a_remediation, a_remediation_event, an_acu_ledger_entry
from sentinel.analytics.metrics import (
    DEFAULT_WINDOW,
    IMPACT_ASSUMPTION,
    SummaryJson,
    Window,
    parse_window,
    percentile,
    summary,
)
from sentinel.config import Settings
from sentinel.models import RemediationEvent

UTC = datetime.UTC

WINDOW = Window(
    start=datetime.datetime(2026, 8, 1, tzinfo=UTC),
    end=datetime.datetime(2026, 8, 8, tzinfo=UTC),
)

GENERATED_AT = datetime.datetime(2026, 8, 8, 4, 12, 3, tzinfo=UTC)

WINDOW_JSON = {"from": "2026-08-01T00:00:00Z", "to": "2026-08-08T00:00:00Z"}

# `ACU_UNIT_COST_USD` defaults to 2.25 (`docs/09-operations.md`), which is what the `settings`
# fixture carries and what every dollar figure below is computed at.
UNIT_COST = 2.25


def at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    """A moment in August 2026, UTC."""
    return datetime.datetime(2026, 8, day, hour, minute, tzinfo=UTC)


# --- The fixture ---------------------------------------------------------------------------------

# Eight remediations labelled inside the window, one per row of the funnel, plus the loop events
# behind their `cycle` counts. Read the columns as the definitions table reads them: a stage is
# reached when its timestamp exists, and a fix cycle is one entry into RUNNING from CI_FAILED or
# CHANGES_REQUESTED.
FIXTURE: tuple[dict[str, Any], ...] = (
    {
        # Merged, first attempt, nobody intervened — the only autonomous fix in the window.
        "issue_number": 101,
        "issue_class": "security",
        "state": "MERGED",
        "labeled_at": at(2, 9),
        "session_created_at": at(2, 9, 5),
        "pr_opened_at": at(2, 9, 33),
        "ci_green_at": at(2, 10),
        "merged_at": at(2, 10, 48),
        "acus_consumed": Decimal("12.000"),
    },
    {
        # Merged after one CI failure.
        "issue_number": 102,
        "issue_class": "flaky-test",
        "state": "MERGED",
        "labeled_at": at(3, 9),
        "session_created_at": at(3, 9, 2),
        "pr_opened_at": at(3, 10),
        "ci_green_at": at(3, 11),
        "merged_at": at(3, 13),
        "cycle": 1,
        "acus_consumed": Decimal("8.500"),
    },
    {
        # Merged with no fix cycle but two human messages — not autonomous.
        "issue_number": 103,
        "issue_class": "typing",
        "state": "MERGED",
        "labeled_at": at(4, 9),
        "session_created_at": at(4, 9, 1),
        "pr_opened_at": at(4, 9, 20),
        "ci_green_at": at(4, 10),
        "merged_at": at(4, 11, 30),
        "human_message_count": 2,
        "acus_consumed": Decimal("5.250"),
    },
    {
        # Merged after both loop edges: a CI failure and a review requesting changes.
        "issue_number": 104,
        "issue_class": "security-dep",
        "state": "MERGED",
        "labeled_at": at(4, 12),
        "session_created_at": at(4, 12, 1),
        "pr_opened_at": at(4, 12, 10),
        "ci_green_at": at(4, 12, 40),
        "merged_at": at(4, 13),
        "cycle": 2,
        "acus_consumed": Decimal("6.250"),
    },
    {
        # Green and waiting on a reviewer: counts in the funnel to `ci_green`, has no MTTR.
        "issue_number": 105,
        "issue_class": "perf",
        "state": "IN_REVIEW",
        "labeled_at": at(5, 9),
        "session_created_at": at(5, 9, 2),
        "pr_opened_at": at(5, 9, 45),
        "ci_green_at": at(5, 10, 15),
        "acus_consumed": Decimal("4.000"),
    },
    {
        # Still looping: a pull request but no green CI, so no review latency either.
        "issue_number": 106,
        "issue_class": "bug",
        "state": "CI_FAILED",
        "labeled_at": at(6, 9),
        "session_created_at": at(6, 9, 1),
        "pr_opened_at": at(6, 9, 30),
        "cycle": 1,
        "acus_consumed": Decimal("3.000"),
    },
    {
        # Escalated: a session, no pull request, and a reason the failure panel groups on.
        "issue_number": 107,
        "issue_class": "deprecation",
        "state": "BLOCKED",
        "labeled_at": at(6, 10),
        "session_created_at": at(6, 10, 1),
        "blocked_reason": "requires_upstream_decision",
        "acus_consumed": Decimal("1.000"),
    },
    {
        # Failed before a session existed: labelled, and nothing else.
        "issue_number": 108,
        "issue_class": "frontend-dep",
        "state": "FAILED",
        "labeled_at": at(7, 8),
        "blocked_reason": "cycle_limit_exhausted",
    },
)

# Labelled either side of the window. Both merged, both carrying ACUs large enough that including
# one would move every figure in the payload.
OUTSIDE: tuple[dict[str, Any], ...] = (
    {
        "issue_number": 100,
        "issue_class": "security",
        "state": "MERGED",
        # One hour before the window opens.
        "labeled_at": datetime.datetime(2026, 7, 31, 23, tzinfo=UTC),
        "session_created_at": datetime.datetime(2026, 7, 31, 23, 5, tzinfo=UTC),
        "pr_opened_at": datetime.datetime(2026, 8, 1, 1, tzinfo=UTC),
        "ci_green_at": datetime.datetime(2026, 8, 1, 2, tzinfo=UTC),
        "merged_at": datetime.datetime(2026, 8, 1, 3, tzinfo=UTC),
        "acus_consumed": Decimal("99.000"),
    },
    {
        "issue_number": 109,
        "issue_class": "security",
        "state": "MERGED",
        # Exactly the window's end. The interval is half-open, so this one is the next window's.
        "labeled_at": WINDOW.end,
        "session_created_at": at(8, 1),
        "pr_opened_at": at(8, 2),
        "ci_green_at": at(8, 3),
        "merged_at": at(8, 4),
        "acus_consumed": Decimal("77.000"),
    },
)

# Loop events, keyed by issue number: `(from_state, to_state)` pairs appended to the log.
#
# Issue 104 carries both loop edges, and the seeding helper writes the whole log in one transaction
# — so those two rows share a `created_at` to the microsecond, exactly as the pipeline produces
# them. The last two entries are decoys: an entry into RUNNING that is not a fix cycle, and a
# CI_FAILED row that leaves the loop rather than re-entering it.
EVENTS: tuple[tuple[int, str, str], ...] = (
    (102, "CI_FAILED", "RUNNING"),
    (104, "CI_FAILED", "RUNNING"),
    (104, "CHANGES_REQUESTED", "RUNNING"),
    (106, "CI_FAILED", "RUNNING"),
    (101, "SESSION_CREATED", "RUNNING"),
    (108, "CI_FAILED", "FAILED"),
)


async def seed(
    session: AsyncSession,
    remediations: tuple[dict[str, Any], ...] = (),
    events: tuple[tuple[int, str, str], ...] = (),
) -> None:
    """Write the rows and hand the test a session that has forgotten them.

    `expunge_all` matters: without it the query inside `summary` would be answered from the identity
    map with the very objects the test built, columns filled by the database — `cycle`,
    `human_message_count`, `acus_consumed` — still unset on them. Every figure would then be
    computed from a row that does not exist in Postgres.
    """
    rows = [a_remediation(**fields) for fields in remediations]
    session.add_all(rows)
    await session.flush()

    by_issue = {row.issue_number: row.id for row in rows}
    session.add_all(
        a_remediation_event(
            remediation_id=by_issue[issue], from_state=from_state, to_state=to_state
        )
        for issue, from_state, to_state in events
    )
    await session.commit()
    session.expunge_all()


@pytest.fixture
async def seeded(session: AsyncSession) -> AsyncSession:
    """The window of `FIXTURE`, plus the two remediations either side of it."""
    await seed(session, FIXTURE + OUTSIDE, EVENTS)
    return session


async def summarise(session: AsyncSession, settings: Settings, **kwargs: Any) -> SummaryJson:
    return await summary(
        session, kwargs.pop("window", WINDOW), settings, now=GENERATED_AT, **kwargs
    )


# --- The whole payload ---------------------------------------------------------------------------


async def test_summary_matches_the_hand_computed_window(
    seeded: AsyncSession, settings: Settings
) -> None:
    """The complete response body, every figure derived below and none of them from the code.

    funnel        labelled 8; session_created 7 (108 never got one); pr_opened 6 (107, 108 did not);
                  ci_green 5 (106 is still red); merged 4 (101-104).
    rates         success 4/8 = 0.5; merge 4/6 = 0.667; autonomy 1/4 = 0.25 — of the four merges
                  only 101 had cycle 0 and no human message (102 and 104 looped, 103 was messaged).
    to_pr         600, 1200, 1800, 1980, 2700, 3600 s. n=6: p50 at ceil(3) = 1800, p90 at
                  ceil(5.4) = 6th = 3600.
    to_merge      3600, 6480, 9000, 14400 s. n=4: p50 at ceil(2) = 6480, p90 at ceil(3.6) = 14400.
    review        1200, 2880, 5400, 7200 s — merged_at - ci_green_at for the four merges.
    cost          12 + 8.5 + 5.25 + 6.25 + 4 + 3 + 1 + 0 = 40.0 ACU; 40.0/4 = 10.0 per merged fix;
                  10.0 x 2.25 = 22.5 USD. No `acu_ledger` rows, so the totals are Sentinel's own.
    cycles        loop events per remediation: 102 one, 104 two, 106 one, the rest none. Mean over
                  all eight labelled = 4/8 = 0.5.
    throughput    the four merges by the UTC day of `merged_at`; 103 and 104 merged on the same day.
    failures      107 blocked and 108 failed, one each, ordered by reason once the counts tie.
    impact        security 6.0 + flaky-test 3.0 + typing 3.0 + security-dep 2.0 = 14.0 hours.
    """
    assert await summarise(seeded, settings) == {
        "window": WINDOW_JSON,
        "funnel": {
            "labelled": 8,
            "session_created": 7,
            "pr_opened": 6,
            "ci_green": 5,
            "merged": 4,
        },
        "rates": {"success": 0.5, "merge": 0.667, "autonomy": 0.25},
        "durations_seconds": {
            "to_pr": {"p50": 1800, "p90": 3600},
            "to_merge": {"p50": 6480, "p90": 14400},
            "review_latency": {"p50": 2880, "p90": 7200},
        },
        "cost": {
            "acus_total": 40.0,
            "acus_per_merged_fix": 10.0,
            "usd_per_fix": 22.5,
            "unit_cost_usd": UNIT_COST,
            "source": "derived",
        },
        "cycles": {"mean": 0.5, "distribution": {"0": 5, "1": 2, "2": 1}},
        "throughput": [
            {"day": "2026-08-02", "by_class": {"security": 1}},
            {"day": "2026-08-03", "by_class": {"flaky-test": 1}},
            {"day": "2026-08-04", "by_class": {"security-dep": 1, "typing": 1}},
        ],
        "failures": [
            {"reason": "cycle_limit_exhausted", "count": 1, "issues": [108]},
            {"reason": "requires_upstream_decision", "count": 1, "issues": [107]},
        ],
        "impact": {"hours_saved": 14.0, "assumption": IMPACT_ASSUMPTION},
        "generated_at": "2026-08-08T04:12:03Z",
    }


async def test_the_window_is_half_open_on_labeled_at(
    seeded: AsyncSession, settings: Settings
) -> None:
    """The two remediations either side are excluded, and neither is excluded by accident.

    Issue 100 is labelled an hour early and merges *inside* the window; issue 109 is labelled at the
    exact instant the window ends. Widening the interval to either would raise `labelled` to 9 and
    add 99 or 77 ACUs, so this asserts on the funnel and the ACU total together.
    """
    payload = await summarise(seeded, settings)
    assert payload["funnel"]["labelled"] == 8
    assert payload["cost"]["acus_total"] == 40.0

    shifted = Window(start=WINDOW.start, end=WINDOW.end + datetime.timedelta(microseconds=1))
    assert (await summarise(seeded, settings, window=shifted))["funnel"]["labelled"] == 9


async def test_the_window_owns_its_first_instant_and_not_its_last(
    session: AsyncSession, settings: Settings
) -> None:
    """A remediation labelled at exactly `start` is in the window; one at exactly `end` is not.

    Consecutive windows tile the timeline, so each boundary belongs to exactly one of them. Both
    remediations are escalated with a reason naming where they sit, which is how the assertion says
    *which* of the two was counted rather than only how many were.
    """
    await seed(
        session,
        (
            {
                "issue_number": 110,
                "state": "BLOCKED",
                "labeled_at": WINDOW.start,
                "blocked_reason": "labelled_at_the_first_instant",
            },
            {
                "issue_number": 111,
                "state": "BLOCKED",
                "labeled_at": WINDOW.end,
                "blocked_reason": "labelled_at_the_last_instant",
            },
        ),
    )

    payload = await summarise(session, settings)
    assert payload["funnel"]["labelled"] == 1
    assert payload["failures"] == [
        {"reason": "labelled_at_the_first_instant", "count": 1, "issues": [110]}
    ]


async def test_an_empty_window_is_a_complete_payload_of_zeros(
    session: AsyncSession, settings: Settings
) -> None:
    """Nothing labelled: every figure is `0`, and the payload still has every field.

    The schema types each figure as a number and the dashboard decides from the funnel whether one
    exists, so `0` here is the representation of "undefined" rather than a measurement. A `null` or
    a `NaN` would reach the panels as a missing field or as `NaN%`.
    """
    assert await summarise(session, settings) == {
        "window": WINDOW_JSON,
        "funnel": {
            "labelled": 0,
            "session_created": 0,
            "pr_opened": 0,
            "ci_green": 0,
            "merged": 0,
        },
        "rates": {"success": 0.0, "merge": 0.0, "autonomy": 0.0},
        "durations_seconds": {
            "to_pr": {"p50": 0, "p90": 0},
            "to_merge": {"p50": 0, "p90": 0},
            "review_latency": {"p50": 0, "p90": 0},
        },
        "cost": {
            "acus_total": 0.0,
            "acus_per_merged_fix": 0.0,
            "usd_per_fix": 0.0,
            "unit_cost_usd": UNIT_COST,
            "source": "derived",
        },
        "cycles": {"mean": 0.0, "distribution": {}},
        "throughput": [],
        "failures": [],
        "impact": {"hours_saved": 0.0, "assumption": IMPACT_ASSUMPTION},
        "generated_at": "2026-08-08T04:12:03Z",
    }


async def test_a_window_where_nothing_reached_a_pull_request(
    session: AsyncSession, settings: Settings
) -> None:
    """Labelled work that stalled: a real 0% success rate, and no durations at all.

    This is the case the spec separates success rate from merge rate for. Success is `0/3` — a
    measured zero — while merge rate divides by an empty `pr_opened` and is undefined.
    """
    await seed(
        session,
        tuple(
            {
                "issue_number": 200 + index,
                "state": "QUEUED",
                "labeled_at": at(3, 9 + index),
                "session_created_at": at(3, 9 + index, 1),
            }
            for index in range(3)
        ),
    )

    payload = await summarise(session, settings)
    assert payload["funnel"] == {
        "labelled": 3,
        "session_created": 3,
        "pr_opened": 0,
        "ci_green": 0,
        "merged": 0,
    }
    assert payload["rates"] == {"success": 0.0, "merge": 0.0, "autonomy": 0.0}
    assert payload["durations_seconds"]["to_pr"] == {"p50": 0, "p90": 0}
    assert payload["cycles"] == {"mean": 0.0, "distribution": {"0": 3}}
    assert payload["throughput"] == []
    assert payload["impact"]["hours_saved"] == 0.0


# --- Percentiles ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "rank", "expected"),
    [
        # No sample at all: the figure does not exist, and 0 is how the schema says so.
        ((), 50, 0.0),
        ((), 90, 0.0),
        # One sample is both percentiles of itself.
        ((900.0,), 50, 900.0),
        ((900.0,), 90, 900.0),
        # Two samples: ceil(0.5 x 2) = 1st, ceil(0.9 x 2) = 2nd. The p50 is the lower observation,
        # not the mean of the two — an interpolating definition would answer 1200.0 here.
        ((900.0, 1500.0), 50, 900.0),
        ((900.0, 1500.0), 90, 1500.0),
        # Unsorted input, and an even count: ceil(0.5 x 4) = 2nd of 1, 2, 3, 4.
        ((4.0, 2.0, 1.0, 3.0), 50, 2.0),
        ((4.0, 2.0, 1.0, 3.0), 90, 4.0),
        # An odd count: ceil(0.5 x 5) = 3rd, ceil(0.9 x 5) = ceil(4.5) = 5th.
        ((1.0, 2.0, 3.0, 4.0, 5.0), 50, 3.0),
        ((1.0, 2.0, 3.0, 4.0, 5.0), 90, 5.0),
        # Ten samples, where the rank lands exactly: ceil(0.9 x 10) = 9th of ten.
        (tuple(float(n) for n in range(1, 11)), 90, 9.0),
    ],
)
def test_percentile_is_an_observed_value_at_the_nearest_rank(
    values: tuple[float, ...], rank: int, expected: float
) -> None:
    assert percentile(values, rank) == expected


def test_percentile_never_returns_a_value_nobody_observed() -> None:
    """Whatever the sample, the answer is one of its members — the property the ADR rests on."""
    values = [1.0, 2.0, 100.0, 101.0, 5000.0, 5001.0, 5002.0]
    for rank in (50, 90):
        assert percentile(values, rank) in values


async def test_percentiles_over_a_single_merged_remediation(
    session: AsyncSession, settings: Settings
) -> None:
    """One merge in the window: p50 and p90 are both its own duration, not an average of nothing."""
    await seed(
        session,
        (
            {
                "issue_number": 301,
                "state": "MERGED",
                "labeled_at": at(3, 9),
                "session_created_at": at(3, 9, 1),
                "pr_opened_at": at(3, 9, 30),
                "ci_green_at": at(3, 10),
                "merged_at": at(3, 11),
            },
        ),
    )

    assert (await summarise(session, settings))["durations_seconds"] == {
        "to_pr": {"p50": 1800, "p90": 1800},
        "to_merge": {"p50": 7200, "p90": 7200},
        "review_latency": {"p50": 3600, "p90": 3600},
    }


async def test_the_published_percentiles_are_the_50th_and_the_90th(
    session: AsyncSession, settings: Settings
) -> None:
    """Ten merges, one to ten minutes: p50 is the 5th observation and p90 the 9th.

    Ten is the smallest sample on which the two ranks the schema publishes are distinguishable from
    their neighbours — with four or six samples `ceil(0.9n)` and `ceil(0.95n)` are the same row, so
    a p90 that had quietly become a p95, or a maximum, would go unnoticed.
    """
    labelled = at(3, 9)
    await seed(
        session,
        tuple(
            {
                "issue_number": 1000 + minutes,
                "state": "MERGED",
                "labeled_at": labelled,
                "merged_at": labelled + datetime.timedelta(minutes=minutes),
            }
            for minutes in range(1, 11)
        ),
    )

    assert (await summarise(session, settings))["durations_seconds"]["to_merge"] == {
        "p50": 300,
        "p90": 540,
    }


# --- Fix cycles ----------------------------------------------------------------------------------


async def test_fix_cycles_count_rows_that_share_a_created_at(
    seeded: AsyncSession, settings: Settings
) -> None:
    """Issue 104's two loop events carry the same timestamp, and both are counted.

    `remediation_event.created_at` is `now()`, which is `transaction_timestamp()`, and
    `docs/06-event-pipeline.md` writes a transition and its event in one transaction — so a
    remediation that loops twice in one window can hold rows indistinguishable by time. The seeding
    helper commits once, which reproduces that: the assertion below checks the tie is real before
    the count is trusted.
    """
    timestamps = (
        await seeded.scalars(select(RemediationEvent.created_at).order_by(RemediationEvent.id))
    ).all()
    assert len(set(timestamps)) == 1, "the fixture no longer produces the tie this test is about"

    payload = await summarise(seeded, settings)
    assert payload["cycles"]["distribution"]["2"] == 1
    assert payload["cycles"]["mean"] == 0.5


async def test_fix_cycles_ignore_entries_into_running_that_are_not_loop_edges(
    session: AsyncSession, settings: Settings
) -> None:
    """Only `CI_FAILED` and `CHANGES_REQUESTED` into `RUNNING` count.

    The log holds four rows for one remediation and exactly one of them is a fix cycle: the first
    start is not self-correction, a CI failure that ends the remediation is not a return to work,
    and an event on another remediation belongs to that one's count.
    """
    await seed(
        session,
        (
            {"issue_number": 401, "labeled_at": at(3, 9)},
            {"issue_number": 402, "labeled_at": at(3, 10)},
        ),
        (
            (401, "SESSION_CREATED", "RUNNING"),
            (401, "CI_FAILED", "RUNNING"),
            (401, "CI_FAILED", "FAILED"),
            (401, "CHANGES_REQUESTED", "FAILED"),
            (402, "CHANGES_REQUESTED", "RUNNING"),
        ),
    )

    assert (await summarise(session, settings))["cycles"] == {
        "mean": 1.0,
        "distribution": {"1": 2},
    }


async def test_fix_cycles_are_averaged_over_every_remediation_not_only_the_merged_ones(
    session: AsyncSession, settings: Settings
) -> None:
    """A remediation that looped three times and was abandoned still counts.

    Averaging over merges alone would report 0.0 here — the most flattering possible reading of a
    window in which one fix needed three attempts and never landed.
    """
    await seed(
        session,
        (
            {
                "issue_number": 501,
                "state": "MERGED",
                "labeled_at": at(3, 9),
                "merged_at": at(3, 10),
            },
            {"issue_number": 502, "state": "FAILED", "labeled_at": at(3, 11), "cycle": 3},
        ),
        (
            (502, "CI_FAILED", "RUNNING"),
            (502, "CI_FAILED", "RUNNING"),
            (502, "CHANGES_REQUESTED", "RUNNING"),
        ),
    )

    assert (await summarise(session, settings))["cycles"] == {
        "mean": 1.5,
        "distribution": {"0": 1, "3": 1},
    }


# --- Cost ----------------------------------------------------------------------------------------


async def test_cost_per_fix_multiplies_before_rounding(
    session: AsyncSession, settings: Settings
) -> None:
    """The spec's own sample figures: 61.4 ACU over five merges at $2.25.

    61.4/5 = 12.28 ACU per merged fix, published rounded to 12.3, and 12.28 x 2.25 = 27.63 USD,
    published as 27.6. Rounding the ACU figure first and multiplying that would give 27.7 — which
    is why `docs/07-observability.md` can show 12.3 and 27.6 side by side without contradicting
    itself.
    """
    await seed(
        session,
        tuple(
            {
                "issue_number": 600 + index,
                "state": "MERGED",
                "labeled_at": at(3, 9 + index),
                "merged_at": at(3, 10 + index),
                "acus_consumed": Decimal("12.280"),
            }
            for index in range(5)
        ),
    )

    assert (await summarise(session, settings))["cost"] == {
        "acus_total": 61.4,
        "acus_per_merged_fix": 12.3,
        "usd_per_fix": 27.6,
        "unit_cost_usd": UNIT_COST,
        "source": "derived",
    }


async def test_the_unit_cost_comes_from_configuration(
    seeded: AsyncSession, settings: Settings
) -> None:
    """`ACU_UNIT_COST_USD` scales the dollar figure and nothing else.

    10.0 ACU per merged fix at $4.00 is $40.00; the ACU figures are unchanged.
    """
    payload = await summarise(seeded, settings.model_copy(update={"acu_unit_cost_usd": 4.0}))
    assert payload["cost"]["unit_cost_usd"] == 4.0
    assert payload["cost"]["usd_per_fix"] == 40.0
    assert payload["cost"]["acus_per_merged_fix"] == 10.0
    assert payload["cost"]["acus_total"] == 40.0


async def test_cost_is_devin_s_when_the_ledger_covers_every_day_of_the_window(
    seeded: AsyncSession, settings: Settings
) -> None:
    """All seven days synced from the consumption API, so the ACU counts are attributable to it."""
    seeded.add_all(
        an_acu_ledger_entry(day=datetime.date(2026, 8, day), acus=Decimal("5.000"))
        for day in range(1, 8)
    )
    await seeded.commit()

    assert (await summarise(seeded, settings))["cost"]["source"] == "devin_consumption_api"


async def test_cost_is_derived_when_the_ledger_misses_a_day(
    seeded: AsyncSession, settings: Settings
) -> None:
    """One unsynced day and the ledger cannot vouch for the window, whichever day it is.

    The label is the whole point of `cost.source`: a reader must be able to tell a figure Devin
    reported from one Sentinel worked out for itself.
    """
    seeded.add_all(
        an_acu_ledger_entry(day=datetime.date(2026, 8, day), acus=Decimal("5.000"))
        for day in (1, 2, 3, 5, 6, 7)
    )
    await seeded.commit()

    assert (await summarise(seeded, settings))["cost"]["source"] == "derived"


async def test_the_ledger_day_after_the_window_does_not_cover_it(
    seeded: AsyncSession, settings: Settings
) -> None:
    """The window ends at midnight on the 8th, so the 8th is not one of its days — and the 8th
    being synced does not make up for the 4th not being."""
    seeded.add_all(
        an_acu_ledger_entry(day=datetime.date(2026, 8, day), acus=Decimal("5.000"))
        for day in (1, 2, 3, 5, 6, 7, 8)
    )
    await seeded.commit()

    assert (await summarise(seeded, settings))["cost"]["source"] == "derived"


# --- Failures ------------------------------------------------------------------------------------


async def test_failures_are_ordered_by_size_then_reason(
    session: AsyncSession, settings: Settings
) -> None:
    """Largest bucket first, and only `BLOCKED` and `FAILED` rows are in any bucket."""
    await seed(
        session,
        (
            {
                "issue_number": 701,
                "state": "BLOCKED",
                "labeled_at": at(3, 9),
                "blocked_reason": "requires_upstream_decision",
            },
            {
                "issue_number": 702,
                "state": "FAILED",
                "labeled_at": at(3, 10),
                "blocked_reason": "session_errored",
            },
            {
                "issue_number": 703,
                "state": "BLOCKED",
                "labeled_at": at(3, 11),
                "blocked_reason": "session_errored",
            },
            {
                "issue_number": 704,
                "state": "FAILED",
                "labeled_at": at(3, 12),
                "blocked_reason": "acu_budget_exhausted",
            },
            # In flight, and carrying a reason from an earlier escalation attempt. Not a failure.
            {
                "issue_number": 705,
                "state": "RUNNING",
                "labeled_at": at(3, 13),
                "blocked_reason": "requires_upstream_decision",
            },
        ),
    )

    assert (await summarise(session, settings))["failures"] == [
        {"reason": "session_errored", "count": 2, "issues": [702, 703]},
        {"reason": "acu_budget_exhausted", "count": 1, "issues": [704]},
        {"reason": "requires_upstream_decision", "count": 1, "issues": [701]},
    ]


async def test_a_failure_with_no_reason_is_shown_not_dropped(
    session: AsyncSession, settings: Settings
) -> None:
    """A terminal remediation nobody attributed is its own bucket — the finding, not a gap."""
    await seed(
        session,
        ({"issue_number": 801, "state": "FAILED", "labeled_at": at(3, 9)},),
    )

    assert (await summarise(session, settings))["failures"] == [
        {"reason": "unspecified", "count": 1, "issues": [801]}
    ]


# --- Throughput ----------------------------------------------------------------------------------


async def test_throughput_is_keyed_by_the_day_a_fix_merged(
    session: AsyncSession, settings: Settings
) -> None:
    """Days come from `merged_at`, so a fix merged after the window still lands on its own day.

    The window selects on `labeled_at`; the throughput series then reports when those remediations
    merged. A merge on the 9th belongs to the 9th even though the window closed on the 8th.
    """
    await seed(
        session,
        (
            {
                "issue_number": 901,
                "issue_class": "security",
                "state": "MERGED",
                "labeled_at": at(7, 23),
                "merged_at": at(9, 1),
            },
            {
                "issue_number": 902,
                "issue_class": "security",
                "state": "MERGED",
                "labeled_at": at(7, 22),
                # 23:30 in a +09:00 offset is 14:30 UTC on the same day.
                "merged_at": datetime.datetime(
                    2026, 8, 7, 23, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=9))
                ),
            },
        ),
    )

    assert (await summarise(session, settings))["throughput"] == [
        {"day": "2026-08-07", "by_class": {"security": 1}},
        {"day": "2026-08-09", "by_class": {"security": 1}},
    ]


# --- The window parameter ------------------------------------------------------------------------


def test_parse_window_defaults_to_the_dashboard_s_seven_days() -> None:
    assert DEFAULT_WINDOW == "7d"
    assert parse_window(now=GENERATED_AT) == Window(
        start=GENERATED_AT - datetime.timedelta(days=7), end=GENERATED_AT
    )


@pytest.mark.parametrize(
    ("spec", "length"),
    [
        ("1d", datetime.timedelta(days=1)),
        ("7d", datetime.timedelta(days=7)),
        ("30d", datetime.timedelta(days=30)),
        ("1h", datetime.timedelta(hours=1)),
        ("12h", datetime.timedelta(hours=12)),
    ],
)
def test_parse_window_reads_days_and_hours(spec: str, length: datetime.timedelta) -> None:
    assert parse_window(spec, now=GENERATED_AT) == Window(
        start=GENERATED_AT - length, end=GENERATED_AT
    )


@pytest.mark.parametrize("spec", ["", "7", "d", "7w", "-7d", "7 d", "7days", "0d", "0h"])
def test_parse_window_rejects_anything_else(spec: str) -> None:
    """A malformed parameter is an error the API can turn into a 4xx, not a silent default."""
    with pytest.raises(ValueError, match="window"):
        parse_window(spec, now=GENERATED_AT)
