"""`sentinel.api.analytics` — the three read endpoints of `docs/07-observability.md#analytics-api`.

Every test here calls the router in this process through `httpx.ASGITransport`, against the real
Postgres, through the real `db.session_scope()`. Nothing about the metrics is re-implemented: what
is under test is the boundary — the shape that leaves it, the status codes it chooses, and the
window it hands the metrics module.

**The payload is a contract with shipped code.** `dashboard/src/api.ts` types nine panels against
the schema and `dashboard/src/fixtures/summary.ts` is what their component tests are written on, so
a renamed key or a re-nested figure breaks a dashboard this suite cannot see. Two things hold the
boundary to it: `SUMMARY_KEYS` is read out of `api.ts` itself, so the top level cannot drift at all;
and the fixtures below are transcriptions of `summary.ts` whose full nesting and types the responses
are compared against.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import ClientFactory
from factories import a_remediation, a_remediation_event, an_acu_ledger_entry
from sentinel import db
from sentinel.api import analytics
from sentinel.config import Settings, get_settings

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard" / "src"

SUMMARY_URL = "/api/analytics/summary"
REMEDIATIONS_URL = "/api/remediations"

# The window every seeded remediation below sits inside, and the one the endpoint defaults to.
DEFAULT_WINDOW_DAYS = 7


# ------------------------------------------------------------------ the dashboard's own contract


def dashboard_summary_keys() -> list[str]:
    """The keys `parseSummary` in `dashboard/src/api.ts` refuses a response for missing.

    Read from the file rather than copied, because it is the list the dashboard actually checks:
    a key added there and not here would otherwise pass this suite and fail in the browser.
    """
    source = (DASHBOARD / "api.ts").read_text()
    block = re.search(r"const SUMMARY_KEYS = \[(.*?)\] as const", source, re.S)
    assert block is not None, "dashboard/src/api.ts no longer declares SUMMARY_KEYS"
    return re.findall(r"'([^']+)'", block[1])


# `dashboard/src/fixtures/summary.ts`, transcribed. Only the keys and the types are read; the
# figures are the spec's example and are not what any assertion here compares against.
SUMMARY_FIXTURE: dict[str, Any] = {
    "window": {"from": "2026-08-01T00:00:00Z", "to": "2026-08-08T00:00:00Z"},
    "funnel": {"labelled": 8, "session_created": 8, "pr_opened": 7, "ci_green": 6, "merged": 5},
    "rates": {"success": 0.625, "merge": 0.714, "autonomy": 0.6},
    "durations_seconds": {
        "to_pr": {"p50": 1980, "p90": 3600},
        "to_merge": {"p50": 6480, "p90": 14400},
        "review_latency": {"p50": 2700, "p90": 7200},
    },
    "cost": {
        "acus_total": 61.4,
        "acus_per_merged_fix": 12.3,
        "usd_per_fix": 27.6,
        "unit_cost_usd": 2.25,
        "source": "devin_consumption_api",
    },
    "cycles": {"mean": 0.8, "distribution": {"0": 3, "1": 1, "2": 1}},
    "throughput": [{"day": "2026-08-06", "by_class": {"security": 1, "flaky-test": 1}}],
    "failures": [{"reason": "requires_upstream_decision", "count": 1, "issues": [37]}],
    "impact": {"hours_saved": 21.0, "assumption": "baseline hours per issue class; see docs/05"},
    "generated_at": "2026-08-08T04:12:03Z",
}

EMPTY_SUMMARY_FIXTURE: dict[str, Any] = {
    **SUMMARY_FIXTURE,
    "funnel": {"labelled": 0, "session_created": 0, "pr_opened": 0, "ci_green": 0, "merged": 0},
    "rates": {"success": 0, "merge": 0, "autonomy": 0},
    "durations_seconds": {
        "to_pr": {"p50": 0, "p90": 0},
        "to_merge": {"p50": 0, "p90": 0},
        "review_latency": {"p50": 0, "p90": 0},
    },
    "cost": {
        "acus_total": 0,
        "acus_per_merged_fix": 0,
        "usd_per_fix": 0,
        "unit_cost_usd": 2.25,
        "source": "derived",
    },
    "cycles": {"mean": 0, "distribution": {}},
    "throughput": [],
    "failures": [],
    "impact": {"hours_saved": 0, "assumption": "baseline hours per issue class; see docs/05"},
}

REMEDIATION_ROW_FIXTURE_KEYS = frozenset(
    {
        "id",
        "repo",
        "issue_number",
        "issue_class",
        "state",
        "cycle",
        "acus_consumed",
        "elapsed_seconds",
        "devin_session_url",
        "pr_number",
        "pr_url",
        "blocked_reason",
        "labeled_at",
    }
)
"""The fields of `RemediationRow` in `dashboard/src/fixtures/summary.ts`, transcribed."""

REMEDIATION_EVENT_FIXTURE_KEYS = frozenset(
    {"id", "remediation_id", "from_state", "to_state", "kind", "detail", "created_at"}
)
"""The fields of `RemediationEvent` in `dashboard/src/fixtures/summary.ts`, transcribed."""

# Keyed by data rather than by the schema — a cycle count and an issue class. Their keys are not
# part of the shape; their values' types are.
OPEN_ENDED_KEYS = frozenset({"distribution", "by_class"})


def shape(value: object, *, open_ended: bool = False) -> str:
    """The recursive keys-and-types skeleton of a JSON value, as a comparable string.

    JSON has one number type and TypeScript's `number` covers both, so `21` and `21.0` are the same
    shape; a difference there is a rounding question, not a contract one. Every element of a list
    must agree, which is itself part of the contract — the dashboard types them as one interface.
    """
    if isinstance(value, dict):
        if open_ended:
            return "{*: " + _one(shape(item) for item in value.values()) + "}"
        fields = ", ".join(
            f"{key}: {shape(item, open_ended=key in OPEN_ENDED_KEYS)}"
            for key, item in sorted(value.items())
        )
        return "{" + fields + "}"
    if isinstance(value, list):
        return "[" + _one(shape(item) for item in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    return type(value).__name__


def _one(shapes: Any) -> str:
    """The single shape a collection's elements share. An empty collection has none."""
    distinct = sorted(set(shapes))
    assert len(distinct) <= 1, f"elements do not share one shape: {distinct}"
    return distinct[0] if distinct else "<empty>"


# ----------------------------------------------------------------------------------- the client


@pytest.fixture
async def client(
    asgi_client: ClientFactory,
    settings: Settings,
    process_engine: None,
) -> Any:
    """The router mounted on an app of its own, over the test database.

    `process_engine` points `db.session_scope()` — which the router's session dependency uses
    unaltered — at the test database, so a request takes a connection of its own and sees only what
    a test has committed. `get_settings` is the one override: it would otherwise read the
    developer's environment.
    """
    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[get_settings] = lambda: settings
    yield await asgi_client(app)
    await db.dispose_engine()


# ------------------------------------------------------------------------------------- the seed

NOW = datetime.datetime.now(datetime.UTC)


def hours(count: float) -> datetime.timedelta:
    return datetime.timedelta(hours=count)


async def seed(session: AsyncSession) -> None:
    """Five remediations inside the default window, and one older than it.

    Between them they reach every stage of the funnel, merge in two different issue classes on two
    different days, escalate once with a reason, and loop once — so every list and every map in the
    payload is non-empty and a shape comparison has something to compare.
    """
    merged_security = a_remediation(
        issue_number=101,
        issue_class="security",
        state="MERGED",
        labeled_at=NOW - hours(72),
        session_created_at=NOW - hours(71),
        pr_opened_at=NOW - hours(70),
        ci_green_at=NOW - hours(69),
        merged_at=NOW - hours(68),
        acus_consumed=Decimal("12.500"),
        pr_number=117,
        pr_url="https://github.com/taxpon/superset/pull/117",
        devin_session_id="devin-4a1b",
        devin_session_url="https://app.devin.ai/sessions/devin-4a1b",
    )
    merged_flaky = a_remediation(
        issue_number=102,
        issue_class="flaky-test",
        state="MERGED",
        labeled_at=NOW - hours(48),
        session_created_at=NOW - hours(47),
        pr_opened_at=NOW - hours(44),
        ci_green_at=NOW - hours(42),
        merged_at=NOW - hours(40),
        acus_consumed=Decimal("8.250"),
        cycle=1,
        human_message_count=2,
        pr_number=118,
        pr_url="https://github.com/taxpon/superset/pull/118",
        devin_session_id="devin-7c31",
        devin_session_url="https://app.devin.ai/sessions/devin-7c31",
    )
    in_review = a_remediation(
        issue_number=103,
        issue_class="typing",
        state="PR_OPENED",
        labeled_at=NOW - hours(24),
        session_created_at=NOW - hours(23),
        pr_opened_at=NOW - hours(22),
        acus_consumed=Decimal("4.000"),
        pr_number=119,
        pr_url="https://github.com/taxpon/superset/pull/119",
        devin_session_id="devin-9f2c",
        devin_session_url="https://app.devin.ai/sessions/devin-9f2c",
    )
    blocked = a_remediation(
        issue_number=104,
        issue_class="security",
        state="BLOCKED",
        labeled_at=NOW - hours(12),
        blocked_reason="requires_upstream_decision",
        acus_consumed=Decimal("1.250"),
        closed_at=NOW - hours(11),
    )
    queued = a_remediation(issue_number=105, issue_class="perf", labeled_at=NOW - hours(1))
    older_than_the_window = a_remediation(
        issue_number=106,
        issue_class="security",
        state="MERGED",
        labeled_at=NOW - hours(24 * 30),
        merged_at=NOW - hours(24 * 30 - 2),
        acus_consumed=Decimal("99.000"),
    )

    session.add_all(
        [merged_security, merged_flaky, in_review, blocked, queued, older_than_the_window]
    )
    await session.flush()

    # One lap of the review-fix loop, which is what puts a second bucket in the cycle distribution.
    session.add(
        a_remediation_event(
            remediation_id=merged_flaky.id, from_state="CI_FAILED", to_state="RUNNING"
        )
    )
    await session.commit()


async def get_summary(client: httpx.AsyncClient, **params: str) -> dict[str, Any]:
    response = await client.get(SUMMARY_URL, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------- the summary payload


async def test_summary_has_exactly_the_keys_the_dashboard_refuses_a_response_without(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)

    body = await get_summary(client)

    assert list(body) == dashboard_summary_keys()


async def test_summary_has_the_nesting_and_types_of_the_dashboard_fixture(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Every panel indexes into this payload; a re-nested or retyped figure is a broken panel."""
    await seed(session)

    body = await get_summary(client)

    assert shape(body) == shape(SUMMARY_FIXTURE)


async def test_a_summary_missing_a_key_never_leaves_the_process(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`metrics.SummaryJson` is the response model, so an incomplete payload fails to serialise.

    The nine panels index into this body and `parseSummary` can only report that a key is missing
    after the fact. Failing the request is the deliberate trade recorded in
    `docs/adr/2026-08-08-the-metrics-module-is-the-published-schema.md`.
    """
    await seed(session)
    complete = analytics.metrics.summary

    async def incomplete(*args: Any, **kwargs: Any) -> Any:
        body = await complete(*args, **kwargs)
        del body["failures"]  # type: ignore[misc]
        return body

    monkeypatch.setattr(analytics.metrics, "summary", incomplete)

    with pytest.raises(ResponseValidationError):
        await client.get(SUMMARY_URL)


async def test_summary_serves_the_figures_the_metrics_module_computed(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The seeded window, figure by figure — so the endpoint cannot be serving a constant.

    The window is what makes these the numbers: the thirtieth-day remediation merged 99 ACUs' worth
    of work and appears in none of them.
    """
    await seed(session)

    body = await get_summary(client)

    assert body["funnel"] == {
        "labelled": 5,
        "session_created": 3,
        "pr_opened": 3,
        "ci_green": 2,
        "merged": 2,
    }
    assert body["rates"] == {"success": 0.4, "merge": 0.667, "autonomy": 0.5}
    assert body["cost"]["acus_total"] == 26.0
    assert body["cost"]["acus_per_merged_fix"] == 13.0
    # 13.0 ACUs at $2.25 is $29.25, published at the one decimal place every per-fix figure is
    # rounded to — once, in the metrics module, so that no two panels disagree.
    assert body["cost"]["usd_per_fix"] == 29.2
    assert body["cost"]["unit_cost_usd"] == 2.25
    assert body["cycles"] == {"mean": 0.2, "distribution": {"0": 4, "1": 1}}
    assert body["failures"] == [
        {"reason": "requires_upstream_decision", "count": 1, "issues": [104]}
    ]
    # 6.0 hours for the security fix, 3.0 for the flaky test. docs/05, via the playbook table.
    assert body["impact"]["hours_saved"] == 9.0
    assert [day["by_class"] for day in body["throughput"]] == [
        {"security": 1},
        {"flaky-test": 1},
    ]


async def test_summary_durations_are_the_seconds_between_the_seeded_timestamps(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)

    body = await get_summary(client)

    assert body["durations_seconds"] == {
        # Labelled to PR: 2h, 4h and 2h — p50 is the middle observation, p90 the largest.
        "to_pr": {"p50": 2 * 3600, "p90": 4 * 3600},
        "to_merge": {"p50": 4 * 3600, "p90": 8 * 3600},
        "review_latency": {"p50": 1 * 3600, "p90": 2 * 3600},
    }


async def test_cost_is_labelled_devin_s_only_when_the_ledger_covers_every_day(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """`cost.source` is provenance the cost panel renders; an unsynced day makes it `derived`."""
    await seed(session)
    assert (await get_summary(client))["cost"]["source"] == "derived"

    covered = NOW.date() - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)
    session.add_all(
        an_acu_ledger_entry(day=covered + datetime.timedelta(days=offset))
        for offset in range(DEFAULT_WINDOW_DAYS + 1)
    )
    await session.commit()

    assert (await get_summary(client))["cost"]["source"] == "devin_consumption_api"


# ------------------------------------------------------------------------------ the empty window


async def test_an_empty_database_answers_with_a_complete_payload_of_zeros(
    client: httpx.AsyncClient,
) -> None:
    """Not a 404, not a null, not a `NaN` — the schema types every figure as a number.

    The dashboard decides from `funnel.labelled` whether a figure exists at all and renders an em
    dash where it does not, so the payload has to be complete for it to make that decision.
    """
    body = await get_summary(client)

    assert list(body) == dashboard_summary_keys()
    assert shape(body) == shape(EMPTY_SUMMARY_FIXTURE)
    assert body["funnel"] == {
        "labelled": 0,
        "session_created": 0,
        "pr_opened": 0,
        "ci_green": 0,
        "merged": 0,
    }
    assert body["rates"] == {"success": 0.0, "merge": 0.0, "autonomy": 0.0}
    assert body["durations_seconds"] == {
        "to_pr": {"p50": 0, "p90": 0},
        "to_merge": {"p50": 0, "p90": 0},
        "review_latency": {"p50": 0, "p90": 0},
    }
    assert body["cost"] == {
        "acus_total": 0.0,
        "acus_per_merged_fix": 0.0,
        "usd_per_fix": 0.0,
        "unit_cost_usd": 2.25,
        "source": "derived",
    }
    assert body["cycles"] == {"mean": 0.0, "distribution": {}}
    assert body["throughput"] == []
    assert body["failures"] == []
    assert body["impact"]["hours_saved"] == 0.0


# ------------------------------------------------------------------------------------ the window


async def test_the_default_window_is_the_one_the_dashboard_asks_for(
    client: httpx.AsyncClient,
) -> None:
    """No `window` parameter is seven days — the same default `dashboard/src/api.ts` sends."""
    window = (await get_summary(client))["window"]

    covered = _parse(window["to"]) - _parse(window["from"])
    assert covered == datetime.timedelta(days=DEFAULT_WINDOW_DAYS)


@pytest.mark.parametrize(
    ("spec", "expected_seconds"),
    [("1d", 86_400), ("12h", 43_200), ("365d", 365 * 86_400)],
)
async def test_a_window_spec_selects_the_interval_it_names(
    client: httpx.AsyncClient, spec: str, expected_seconds: int
) -> None:
    window = (await get_summary(client, window=spec))["window"]

    assert (_parse(window["to"]) - _parse(window["from"])).total_seconds() == expected_seconds


async def test_the_window_decides_which_remediations_are_counted(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)

    # None of these lands on a seeded `labeled_at`: the window ends at the moment the request is
    # served, which is later than the clock the rows were built from.
    assert (await get_summary(client, window="6h"))["funnel"]["labelled"] == 1
    assert (await get_summary(client, window="36h"))["funnel"]["labelled"] == 3
    assert (await get_summary(client, window="365d"))["funnel"]["labelled"] == 6


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "7",
        "d",
        "7w",
        "week",
        "-1d",
        "7.5d",
        "7d ",
        " 7d",
        "7 d",
        "1e3d",
    ],
)
async def test_a_malformed_window_is_a_client_error_naming_the_parameter(
    client: httpx.AsyncClient, spec: str
) -> None:
    """`parse_window` raises `ValueError` so that this can be a 400. A 500 would be a defect."""
    response = await client.get(SUMMARY_URL, params={"window": spec})

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "days or hours" in detail
    assert repr(spec) in detail


async def test_a_window_of_no_time_at_all_is_rejected(client: httpx.AsyncClient) -> None:
    """`Window` is a public dataclass and `_cost_source` guards against a degenerate one; the API
    never builds one, because `0d` never reaches `Window` at all."""
    response = await client.get(SUMMARY_URL, params={"window": "0d"})

    assert response.status_code == 400, response.text
    assert "more than no time at all" in response.json()["detail"]


@pytest.mark.parametrize("spec", ["366d", "8761h", "9999999999d", "999999999999999999999d"])
async def test_a_window_beyond_the_ceiling_is_rejected_and_the_ceiling_is_named(
    client: httpx.AsyncClient, spec: str
) -> None:
    """The largest of these overflows `timedelta` rather than merely exceeding the cap.

    `parse_window` compares in units precisely so that the `OverflowError` never escapes, and the
    error a client gets says what the limit is instead of being a stack trace.
    """
    response = await client.get(SUMMARY_URL, params={"window": spec})

    assert response.status_code == 400, response.text
    assert "at most 365 days" in response.json()["detail"]


def _parse(moment: str) -> datetime.datetime:
    return datetime.datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)


# ----------------------------------------------------------------------------- the live table


async def test_remediations_returns_every_row_the_live_table_needs(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """One row asserted whole, because the live table links out of it to Devin and to GitHub."""
    await seed(session)

    response = await client.get(REMEDIATIONS_URL)

    assert response.status_code == 200, response.text
    rows = {row["issue_number"]: row for row in response.json()}
    assert set(rows[101]) == REMEDIATION_ROW_FIXTURE_KEYS
    assert rows[101] == {
        "id": rows[101]["id"],
        "repo": "taxpon/superset",
        "issue_number": 101,
        "issue_class": "security",
        "state": "MERGED",
        "cycle": 0,
        "acus_consumed": 12.5,
        "elapsed_seconds": rows[101]["elapsed_seconds"],
        "devin_session_url": "https://app.devin.ai/sessions/devin-4a1b",
        "pr_number": 117,
        "pr_url": "https://github.com/taxpon/superset/pull/117",
        "blocked_reason": None,
        "labeled_at": rows[101]["labeled_at"],
    }
    # The looping row, for the two columns the merged one leaves at their defaults.
    assert rows[102]["cycle"] == 1
    assert rows[102]["acus_consumed"] == 8.25


async def test_remediations_is_not_windowed(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The table answers "what is happening right now" and drops nothing, including the row the
    seven-day summary excludes."""
    await seed(session)

    rows = (await client.get(REMEDIATIONS_URL)).json()

    assert [row["issue_number"] for row in rows] == [105, 104, 103, 102, 101, 106]


async def test_a_remediation_that_reached_nothing_reports_nulls_rather_than_omitting_the_fields(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The live table renders a broken link if a field it expects arrives as `undefined`."""
    await seed(session)

    rows = {row["issue_number"]: row for row in (await client.get(REMEDIATIONS_URL)).json()}

    assert set(rows[105]) == REMEDIATION_ROW_FIXTURE_KEYS
    assert rows[105]["devin_session_url"] is None
    assert rows[105]["pr_number"] is None
    assert rows[105]["pr_url"] is None
    assert rows[105]["blocked_reason"] is None
    assert rows[105]["acus_consumed"] == 0.0
    assert rows[105]["cycle"] == 0


async def test_a_blocked_row_carries_the_reason_the_table_prints_under_its_state(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)

    rows = {row["issue_number"]: row for row in (await client.get(REMEDIATIONS_URL)).json()}

    assert rows[104]["state"] == "BLOCKED"
    assert rows[104]["blocked_reason"] == "requires_upstream_decision"


async def test_elapsed_seconds_is_the_age_of_the_remediation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Seconds since `labeled_at`, so the table does not subtract clocks itself."""
    await seed(session)

    rows = {row["issue_number"]: row for row in (await client.get(REMEDIATIONS_URL)).json()}

    assert 3600 <= rows[105]["elapsed_seconds"] < 3600 + 300
    assert 12 * 3600 <= rows[104]["elapsed_seconds"] < 12 * 3600 + 300


async def test_timestamps_are_formatted_the_way_the_schema_writes_them(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Seconds precision and a `Z`. `Date.parse` in the dashboard reads these."""
    await seed(session)

    rows = (await client.get(REMEDIATIONS_URL)).json()

    for row in rows:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["labeled_at"]), row


async def test_remediations_of_an_empty_database_is_an_empty_list(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(REMEDIATIONS_URL)

    assert response.status_code == 200
    assert response.json() == []


# ------------------------------------------------------------------------------- the timeline


async def test_the_timeline_is_the_append_only_log_of_one_remediation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)
    subject = a_remediation(issue_number=200, issue_class="security")
    session.add(subject)
    await session.flush()
    session.add(
        a_remediation_event(
            remediation_id=subject.id,
            to_state="SESSION_CREATED",
            from_state="QUEUED",
            kind="devin_call",
            detail={"status": 201, "session_id": "devin-9f2c"},
        )
    )
    await session.commit()

    response = await client.get(f"{REMEDIATIONS_URL}/{subject.id}")

    assert response.status_code == 200, response.text
    [event] = response.json()
    assert set(event) == REMEDIATION_EVENT_FIXTURE_KEYS
    assert event["remediation_id"] == subject.id
    assert event["from_state"] == "QUEUED"
    assert event["to_state"] == "SESSION_CREATED"
    assert event["kind"] == "devin_call"
    assert event["detail"] == {"status": 201, "session_id": "devin-9f2c"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", event["created_at"])


async def test_the_timeline_holds_only_that_remediation_s_events(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed(session)
    subject = a_remediation(issue_number=201, issue_class="security")
    other = a_remediation(issue_number=202, issue_class="security")
    session.add_all([subject, other])
    await session.flush()
    session.add_all(
        [
            a_remediation_event(remediation_id=subject.id),
            a_remediation_event(remediation_id=other.id),
        ]
    )
    await session.commit()

    events = (await client.get(f"{REMEDIATIONS_URL}/{subject.id}")).json()

    assert [event["remediation_id"] for event in events] == [subject.id]


async def test_the_timeline_is_ordered_oldest_first_and_tie_broken_on_id(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """`created_at` is `transaction_timestamp()`, so rows written together are tied on it.

    The panel sorts what it receives, but a tie the API resolves differently on every poll would
    still make the timeline flicker. `id` is a `bigserial` — the insertion order the timestamp is
    too coarse to express.

    This assertion pins the requirement rather than catching a regression, and knowingly: `id` is
    assigned in insertion order, which for a scan of a handful of rows is the order Postgres returns
    them in anyway, so dropping the tiebreak from the query does not change this result. Making it
    observable would take an `UPDATE`, which is exactly what an append-only log never does.
    """
    subject = a_remediation(issue_number=203, issue_class="security")
    session.add(subject)
    await session.flush()
    written_together = datetime.datetime(2026, 8, 8, 3, 40, 12, tzinfo=datetime.UTC)
    session.add_all(
        [
            a_remediation_event(
                remediation_id=subject.id, to_state="RUNNING", created_at=written_together
            ),
            a_remediation_event(
                remediation_id=subject.id,
                to_state="QUEUED",
                created_at=written_together - hours(1),
            ),
            a_remediation_event(
                remediation_id=subject.id,
                to_state="SESSION_CREATED",
                created_at=written_together,
            ),
        ]
    )
    await session.commit()

    events = (await client.get(f"{REMEDIATIONS_URL}/{subject.id}")).json()

    assert [event["to_state"] for event in events] == ["QUEUED", "RUNNING", "SESSION_CREATED"]
    tied = [event["id"] for event in events[1:]]
    assert tied == sorted(tied)


async def test_a_timeline_for_a_remediation_that_does_not_exist_is_a_404(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Not an empty list: every remediation is created with the event that queued it, so an empty
    log would read to the panel as a remediation that has done nothing."""
    await seed(session)

    response = await client.get(f"{REMEDIATIONS_URL}/999999")

    assert response.status_code == 404, response.text
    assert "999999" in response.json()["detail"]
