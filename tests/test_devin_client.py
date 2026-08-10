"""The Devin v3 client, checked against `docs/05-devin-integration.md` and against the wire.

Two things every test here is built around.

**The request body is the contract.** `.claude/rules/testing.md` requires the captured request to
be asserted rather than the fact that a call happened: a reviewer opens the Devin dashboard and
compares the tags, the structured-output schema, the ACU ceiling and `resumable` against what
Sentinel claims to have sent, so those are asserted field by field. The endpoint table, the
create-session field list, the seven statuses and the degradation table are *read out of the spec*
and compared, so the client cannot drift from the document without a failure here.

**Failure paths carry as much weight as the happy one.** The retry policy, the non-retryable `4xx`,
the enterprise endpoint that answers `403`, and the token that must not surface in a log line, an
exception or a `repr` are each a test rather than a paragraph.

No test reaches the network: `respx` answers, and anything unregistered raises.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from prometheus_client import CollectorRegistry

from conftest import DEVIN_TOKEN, Configure, FakeAPI, make_settings
from factories import DELIVERY_ID
from sentinel.config import Settings
from sentinel.devin import playbooks as pb
from sentinel.devin.client import (
    ALLOWED_TAGS,
    CONSUMPTION_DAILY,
    DEFAULT_RETRY,
    ENDPOINTS,
    ENTERPRISE_SESSION_METRICS,
    KNOWLEDGE_NOTES,
    ORGANIZATION_TAGS,
    PLAYBOOKS,
    SCHEDULES,
    SESSION,
    SESSION_MESSAGES,
    SESSION_TAGS,
    SESSIONS,
    TIMEOUT,
    DevinAPIError,
    DevinClient,
    DevinResponseError,
    DevinTransportError,
    RetryPolicy,
    registered_tag,
)
from sentinel.devin.schemas import (
    Capability,
    Outcome,
    Risk,
    SessionStatus,
    Unavailability,
    Unavailable,
    pull_request_number,
)
from sentinel.observability.prom import Metrics

DOCS = Path(__file__).resolve().parents[1] / "docs"
SPEC_TEXT = (DOCS / "05-devin-integration.md").read_text()
STATE_MACHINE = (DOCS / "04-state-machine.md").read_text()


# --- Spec parsing ---------------------------------------------------------------------------------


def section(heading: str, spec: str = SPEC_TEXT) -> str:
    """The body of one `## ` section of a spec document."""
    body: list[str] = []
    found = False
    for line in spec.splitlines():
        if line.startswith("## "):
            if found:
                break
            found = line.strip() == f"## {heading}"
            continue
        if found:
            body.append(line)
    assert found, f"spec has no section {heading!r}"
    return "\n".join(body)


def table_rows(text: str) -> list[list[str]]:
    """Data rows of the first Markdown table in `text`, as lists of cells."""
    rows = [line for line in text.splitlines() if line.startswith("|")]
    return [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in rows
        if not set(line) <= set("|- ")
    ][1:]


def backticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


SPEC_ENDPOINT_ROWS = table_rows(section("Endpoints used"))
SPEC_CREATE_ROWS = table_rows(section("Creating a session"))
SPEC_DEGRADATION_ROWS = table_rows(section("Degradation"))

# The spec writes the session id as `{devin_id}` — it is Devin's id for the session, seen from
# Sentinel — and the client as `{session_id}`, which is the field the response returns it in. The
# placeholder name is not part of the URL, so it is normalised before the two are compared.
SPEC_ENDPOINTS = {
    backticked(row[1])[0].replace("{devin_id}", "{session_id}") for row in SPEC_ENDPOINT_ROWS
}
SPEC_CREATE_FIELDS = [backticked(row[0])[0] for row in SPEC_CREATE_ROWS]
# Column two — what Sentinel puts in each field — which is half the mapping and was previously
# never read: the field names alone cannot tell a `title` template or a target repository apart.
SPEC_CREATE_VALUES = {backticked(row[0])[0]: row[1] for row in SPEC_CREATE_ROWS}
SPEC_SWEEP = {backticked(row[0])[0]: row[1] for row in table_rows(section("Scheduled sweep"))}


def bullet(name: str, text: str) -> str:
    """One `- **Name**: …` bullet of a spec section, on a single line."""
    match = re.search(rf"^- \*\*{name}\*\*:(.*?)(?=^- \*\*|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"spec has no {name!r} bullet"
    return " ".join(match.group(1).split())


SPEC_BEHAVIOUR = section("Client behaviour")
SPEC_RETRIES = bullet("Retries", SPEC_BEHAVIOUR)
SPEC_TIMEOUTS = bullet("Timeouts", SPEC_BEHAVIOUR)

_spec_timeout = re.search(r"([\d.]+) s connect/read", SPEC_TIMEOUTS)
assert _spec_timeout is not None, "the spec no longer states a connect/read timeout"
SPEC_TIMEOUT_SECONDS = float(_spec_timeout.group(1))

_status_sentence = re.search(r"`status` values[^:]*:(?P<listed>[^.]*)\.", SPEC_TEXT, re.DOTALL)
assert _status_sentence is not None, "spec no longer lists the session statuses"
SPEC_STATUSES = backticked(_status_sentence.group("listed"))

# `docs/04-state-machine.md` defines the `RUNNING` remediation state as the session statuses that
# mean work is happening, which is exactly what `SessionStatus.is_working` answers.
_working_phrase = re.search(r"session status ([^)]*)\)", STATE_MACHINE)
assert _working_phrase is not None, "the state machine no longer names the working statuses"
SPEC_WORKING_STATUSES = set(backticked(_working_phrase.group(1)))


def test_the_spec_tables_were_parsed() -> None:
    """A parsing bug would silently turn every table-driven test below into a no-op."""
    assert len(SPEC_ENDPOINT_ROWS) == 12
    assert len(SPEC_CREATE_FIELDS) == 10
    assert len(SPEC_DEGRADATION_ROWS) == 5
    assert len(SPEC_STATUSES) == 7
    assert len(SPEC_SWEEP) == 6
    assert SPEC_RETRIES.startswith("exponential backoff")
    assert SPEC_TIMEOUT_SECONDS == 30.0
    assert {"claimed", "running", "resuming"} == SPEC_WORKING_STATUSES


# --- Fixtures -------------------------------------------------------------------------------------

ORG = "org-abc123"
SESSION_ID = "devin-7f3a1c"
KNOWLEDGE_IDS = ("note-tests", "note-conventions")

# Keyed by playbook name, which covers all eight issue classes with four entries
# (`docs/adr/2026-08-08-playbook-ids-keyed-by-class-or-name.md`).
PLAYBOOK_IDS = {playbook.name: f"pb-{playbook.name}" for playbook in pb.PLAYBOOKS}

SESSIONS_URL = SESSIONS.format(org_id=ORG)
SESSION_URL = SESSION.format(org_id=ORG, session_id=SESSION_ID)
MESSAGES_URL = SESSION_MESSAGES.format(org_id=ORG, session_id=SESSION_ID)
SESSION_TAGS_URL = SESSION_TAGS.format(org_id=ORG, session_id=SESSION_ID)
ORG_TAGS_URL = ORGANIZATION_TAGS.format(org_id=ORG)
ALLOWED_TAGS_URL = ALLOWED_TAGS.format(org_id=ORG)
KNOWLEDGE_URL = KNOWLEDGE_NOTES.format(org_id=ORG)
SCHEDULES_URL = SCHEDULES.format(org_id=ORG)
PLAYBOOKS_URL = PLAYBOOKS.format(org_id=ORG)
CONSUMPTION_URL = CONSUMPTION_DAILY.format(org_id=ORG)

METRICS_WINDOW = {"time_after": 1_754_006_400, "time_before": 1_756_598_400}
"""The window every `session_metrics` call here states, because the reference requires one."""

ISSUE: dict[str, Any] = {
    "issue_number": 42,
    "issue_title": "Stored XSS in the dashboard filter box",
    "issue_body": "Steps to reproduce: paste `<img onerror=…>` into a filter value.",
    "issue_class": "security",
    "delivery_id": DELIVERY_ID,
}

STRUCTURED_REPORT: dict[str, Any] = {
    "outcome": "fixed",
    "root_cause": "The filter label was interpolated into innerHTML without escaping.",
    "changes": ["superset-frontend/src/filters/FilterValue.tsx"],
    "tests": {
        "added": ["superset-frontend/src/filters/FilterValue.test.tsx"],
        "command": "npm run test -- FilterValue",
        "passed": True,
    },
    "risk": "low",
    "pr_url": "https://github.com/taxpon/superset/pull/7",
    "confidence": 0.8,
}


PR_URL = "https://github.com/taxpon/superset/pull/7"
PR_NUMBER = 7


def a_session(**overrides: Any) -> dict[str, Any]:
    """A session body shaped as the live API returns one — see `OBSERVED_SESSION_LISTING`."""
    return {
        "session_id": SESSION_ID,
        "url": f"https://app.devin.ai/sessions/{SESSION_ID}",
        "status": "running",
        "status_detail": "working",
        "title": pb.session_title(ISSUE["issue_number"], ISSUE["issue_title"]),
        "tags": pb.session_tags(
            repo="taxpon/superset",
            issue_number=ISSUE["issue_number"],
            issue_class=ISSUE["issue_class"],
            delivery_id=DELIVERY_ID,
        ),
        "acus_consumed": 3.5,
        "pull_requests": [{"pr_url": PR_URL, "pr_state": "open"}],
        **overrides,
    }


OBSERVED_SESSION_LISTING: dict[str, Any] = {
    "items": [
        {
            "session_id": SESSION_ID,
            "url": f"https://app.devin.ai/sessions/{SESSION_ID}",
            "status": "running",
            "title": "[sentinel] #42 Stored XSS in the dashboard filter box",
            "tags": ["sentinel", "repo:taxpon/superset", "issue:42"],
            "playbook_id": None,
            "user_id": "user-8c1d2e3f",
            "org_id": ORG,
            "created_at": 1754784000,
            "updated_at": 1754787600,
            "is_archived": False,
            "acus_consumed": 3.5,
            "pull_requests": [{"pr_url": PR_URL, "pr_state": "open"}],
            "parent_session_id": None,
            "child_session_ids": [],
            "service_user_id": None,
            "category": "engineering",
            "subcategory": "bug_fix",
            "origin": "api",
            "automation_id": None,
            "structured_output": None,
            "devin_mode": "standard",
            "status_detail": "working",
        }
    ],
    "end_cursor": None,
    "has_next_page": False,
    "total": 1,
}
"""`GET /v3/organizations/{org_id}/sessions` as the live API answered it on 2026-08-10 — every key
and every type exactly as observed, with the values replaced by this file's own.

A regression fixture, because the shape it records is not the one the code was written against and
the difference cost the review-fix loop: `pull_requests[].pr_url` was modelled as `url` (the parse
failed outright) and `pull_requests[].number` does not exist at all (it parsed to `None`, which is
worse — `webhooks._criterion` resolves every check suite and review by `pr_number`). Values are
substituted; **key names and types are not**, and changing one here to make a test pass is changing
the fixture to disagree with the API.
"""


class Sleeps:
    """The backoff, recorded instead of waited out."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def devin_settings() -> Settings:
    """Configuration for a deployment with every playbook configured and no enterprise scope."""
    return make_settings(
        devin_org_id=ORG,
        devin_playbook_ids=PLAYBOOK_IDS,
        devin_knowledge_ids=KNOWLEDGE_IDS,
    )


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def client(
    devin_settings: Settings,
    devin_api: FakeAPI,
    metrics: Metrics,
    sleeps: Sleeps,
) -> AsyncIterator[DevinClient]:
    """The client under test: faked HTTP, recorded backoff, a seeded jitter and its own metrics."""
    async with DevinClient(
        devin_settings,
        retry=RetryPolicy(attempts=3, base_delay=0.5, max_delay=30.0),
        metrics=metrics,
        sleep=sleeps,
        rng=random.Random(1234),
    ) as devin:
        yield devin


# --- The route table ------------------------------------------------------------------------------


def test_every_endpoint_is_a_v3_endpoint() -> None:
    """`docs/adr/2026-08-07-devin-v3-only.md` asks for exactly this assertion."""
    assert ENDPOINTS
    assert all(endpoint.startswith("/v3/") for endpoint in ENDPOINTS)


def test_the_route_table_is_the_spec_table() -> None:
    """Both directions: an endpoint the client cannot reach, and one it reaches unspecified."""
    assert ENDPOINTS == SPEC_ENDPOINTS


def test_the_timeout_is_the_documented_one() -> None:
    assert TIMEOUT.connect == SPEC_TIMEOUT_SECONDS
    assert TIMEOUT.read == SPEC_TIMEOUT_SECONDS


def test_the_retry_classification_is_the_documented_one() -> None:
    """Read out of the `Client behaviour` bullet rather than restated: the shape of the backoff and
    the set of statuses that earn one are the policy, and a change to either in the document has to
    fail here."""
    assert "exponential backoff with jitter" in SPEC_RETRIES
    assert {code for code in backticked(SPEC_RETRIES) if code in {"429", "5xx"}} == {"429", "5xx"}
    assert "`4xx` other than `429` fails" in SPEC_RETRIES

    def refused(status: int) -> DevinAPIError:
        return DevinAPIError(method="GET", endpoint=SESSION, status_code=status, body="")

    assert refused(429).retryable
    assert refused(500).retryable and refused(503).retryable
    assert not refused(400).retryable and not refused(422).retryable


# --- Creating a session ---------------------------------------------------------------------------


async def test_create_session_sends_the_documented_body(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("POST", SESSIONS_URL, 201, a_session(status="new", acus_consumed=None))

    await client.create_session(**ISSUE)

    sent = devin_api.only("POST", SESSIONS_URL)
    body = sent.json
    assert sent.headers["authorization"] == f"Bearer {DEVIN_TOKEN}"
    # The field set is the create-session table, exactly: an extra field is a discrepancy a
    # reviewer comparing this against the Devin dashboard would have to chase down.
    assert list(body) == SPEC_CREATE_FIELDS
    assert body["prompt"] == pb.initial_prompt(
        issue_number=42,
        issue_title=ISSUE["issue_title"],
        issue_body=ISSUE["issue_body"],
        repo="taxpon/superset",
        base_branch="master",
    )
    title_template = backticked(SPEC_CREATE_VALUES["title"])[0]
    assert body["title"] == title_template.replace("<issue>", "42").replace(
        "<issue title>", ISSUE["issue_title"]
    )
    assert body["tags"] == [
        "sentinel",
        "repo:taxpon/superset",
        "issue:42",
        "class:security",
        f"run:{DELIVERY_ID}",
    ]
    assert body["repos"] == json.loads(backticked(SPEC_CREATE_VALUES["repos"])[0])
    assert body["playbook_id"] == PLAYBOOK_IDS["security-fix"]
    assert body["knowledge_ids"] == list(KNOWLEDGE_IDS)
    assert body["structured_output_schema"] == pb.STRUCTURED_OUTPUT_SCHEMA
    assert body["structured_output_required"] == json.loads(
        backticked(SPEC_CREATE_VALUES["structured_output_required"])[0]
    )
    assert body["max_acu_limit"] == pb.acu_cap_for("security") == 20
    assert body["resumable"] == json.loads(backticked(SPEC_CREATE_VALUES["resumable"])[0])


@pytest.mark.parametrize("issue_class", list(pb.IssueClass), ids=lambda c: c.value)
async def test_every_class_sends_registered_tags_and_its_own_ceiling(
    client: DevinClient, devin_api: FakeAPI, issue_class: pb.IssueClass
) -> None:
    """The tag set is built through T15's vocabulary, so an unregistered tag — a `422` at creation
    (B7) — cannot be constructed here, and the ACU ceiling follows the class rather than a default.
    """
    devin_api.responds("POST", SESSIONS_URL, 201, a_session())

    await client.create_session(**{**ISSUE, "issue_class": issue_class})

    body = devin_api.only("POST", SESSIONS_URL).json
    assert [pb.validate_tag(tag) for tag in body["tags"]] == body["tags"]
    assert body["tags"][3] == f"class:{issue_class.value}"
    assert body["max_acu_limit"] == pb.acu_cap_for(issue_class)
    assert body["playbook_id"] == PLAYBOOK_IDS[pb.playbook_for(issue_class).name]


async def test_create_session_returns_the_parsed_session(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("POST", SESSIONS_URL, 201, a_session(status="new", status_detail=None))

    session = await client.create_session(**ISSUE)

    assert session.session_id == SESSION_ID
    assert session.status is SessionStatus.NEW
    assert session.url == f"https://app.devin.ai/sessions/{SESSION_ID}"
    assert session.pull_request_url == "https://github.com/taxpon/superset/pull/7"


async def test_an_unhandled_class_is_refused_before_anything_is_sent(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """The worker routes this to `BLOCKED`, and a request Devin would reject is not worth making."""
    with pytest.raises(pb.UnknownIssueClass):
        await client.create_session(**{**ISSUE, "issue_class": "documentation"})

    assert devin_api.requests == []


async def test_an_unconfigured_playbook_id_is_a_deployment_fault(
    devin_api: FakeAPI, metrics: Metrics
) -> None:
    async with DevinClient(
        make_settings(devin_org_id=ORG, devin_playbook_ids={"security": "pb-sec"}), metrics=metrics
    ) as devin:
        with pytest.raises(pb.MissingPlaybookId):
            await devin.create_session(**{**ISSUE, "issue_class": "perf"})

    assert devin_api.requests == []


async def test_the_session_created_event_is_logged(
    client: DevinClient, devin_api: FakeAPI, capture: Configure
) -> None:
    """`docs/09-operations.md` greps the demo runbook for exactly this event name."""
    logs = capture()
    devin_api.responds("POST", SESSIONS_URL, 201, a_session())

    await client.create_session(**ISSUE)

    created = [record for record in logs.records if record["event"] == "devin.session.created"]
    assert len(created) == 1
    assert created[0]["session_id"] == SESSION_ID
    assert created[0]["duration_ms"] >= 0


# --- Reading a session ----------------------------------------------------------------------------


@pytest.mark.parametrize("status", SPEC_STATUSES)
async def test_every_documented_status_parses(
    client: DevinClient, devin_api: FakeAPI, status: str
) -> None:
    devin_api.responds("GET", SESSION_URL, 200, a_session(status=status))

    session = await client.get_session(SESSION_ID)

    assert session.status is SessionStatus(status)
    assert session.status.is_working == (status in SPEC_WORKING_STATUSES)
    assert session.status.is_terminal == (status in {"exit", "error"})
    assert devin_api.only("GET", SESSION_URL).headers["authorization"] == f"Bearer {DEVIN_TOKEN}"


async def test_a_finished_session_carries_the_structured_report(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds(
        "GET",
        SESSION_URL,
        200,
        a_session(status="exit", status_detail=None, structured_output=STRUCTURED_REPORT),
    )

    session = await client.get_session(SESSION_ID)

    assert session.status.is_terminal
    assert session.acus_consumed == 3.5
    report = session.structured_output
    assert report is not None
    assert report.outcome is Outcome.FIXED
    assert report.risk is Risk.LOW
    assert report.tests.added == ("superset-frontend/src/filters/FilterValue.test.tsx",)
    assert report.tests.passed is True
    assert report.confidence == 0.8
    assert report.blocked_reason is None


async def test_a_stalled_session_is_distinguishable_from_a_working_one(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """`status_detail` is the whole reason the poller can tell the two apart."""
    devin_api.responds(
        "GET", SESSION_URL, 200, a_session(status="running", status_detail="waiting_for_user")
    )

    session = await client.get_session(SESSION_ID)

    assert session.status is SessionStatus.RUNNING
    assert session.waiting_for_user


async def test_a_working_session_is_not_reported_as_waiting(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("GET", SESSION_URL, 200, a_session(status="running"))

    assert not (await client.get_session(SESSION_ID)).waiting_for_user


async def test_a_session_that_has_not_started_reports_no_consumption(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("GET", SESSION_URL, 200, a_session(status="new", acus_consumed=None))

    assert (await client.get_session(SESSION_ID)).acus_consumed == 0.0


async def test_a_status_outside_the_seven_is_a_protocol_error(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """Not coerced to something plausible: it would mean our reading of the API is wrong."""
    devin_api.responds("GET", SESSION_URL, 200, a_session(status="hibernating"))

    with pytest.raises(DevinResponseError, match="status"):
        await client.get_session(SESSION_ID)


async def test_a_report_missing_a_required_field_is_a_protocol_error(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """`structured_output_required: true` was sent, so an incomplete report is a broken contract —
    and it will be just as incomplete on the next poll, which is why this is not retryable."""
    incomplete = {key: value for key, value in STRUCTURED_REPORT.items() if key != "root_cause"}
    devin_api.responds("GET", SESSION_URL, 200, a_session(structured_output=incomplete))

    with pytest.raises(DevinResponseError) as raised:
        await client.get_session(SESSION_ID)

    assert "root_cause" in str(raised.value)
    assert not raised.value.retryable


async def test_a_body_that_is_not_json_is_a_protocol_error(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("GET", SESSION_URL, 200, text="<html>maintenance</html>")

    with pytest.raises(DevinResponseError):
        await client.get_session(SESSION_ID)


async def test_list_sessions_filters_by_tag(client: DevinClient, devin_api: FakeAPI) -> None:
    """The page size is `first` — default 100, maximum 200 — and not the `limit` this used to
    send, which `SessionsQueryParams` does not define."""
    devin_api.responds("GET", SESSIONS_URL, 200, {"items": [a_session(), a_session()]})

    sessions = await client.list_sessions(tags=[pb.NAMESPACE_TAG], limit=50)

    assert len(sessions) == 2
    sent = devin_api.only("GET", SESSIONS_URL)
    assert dict(sent.url.params) == {"tags": pb.NAMESPACE_TAG, "first": "50"}


async def test_list_sessions_accepts_a_bare_list(client: DevinClient, devin_api: FakeAPI) -> None:
    """The reference wraps the page in `items`; a bare list stays acceptable so that a fixture can
    state only the sessions it is about."""
    devin_api.responds("GET", SESSIONS_URL, 200, [a_session()])

    assert len(await client.list_sessions()) == 1


# --- The shape the API actually sends ---------------------------------------------------------


async def test_the_observed_session_listing_parses(client: DevinClient, devin_api: FakeAPI) -> None:
    """The body `GET /v3/organizations/{org_id}/sessions` returned on 2026-08-10, verbatim.

    Every field the poller reads has to survive it: the status it branches on, the ACUs the budget
    guard sums, the pull request it links, and the number `webhooks._criterion` resolves CI failures
    and reviews by — which the API does not send and which therefore has to come out of the URL.
    """
    devin_api.responds("GET", SESSIONS_URL, 200, OBSERVED_SESSION_LISTING)

    sessions = await client.list_sessions()

    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == SESSION_ID
    assert session.status is SessionStatus.RUNNING
    assert session.status_detail == "working"
    assert session.acus_consumed == 3.5
    assert session.structured_output is None
    assert session.pull_request_url == PR_URL
    assert [pr.number for pr in session.pull_requests] == [PR_NUMBER]


async def test_a_pull_request_is_read_only_under_the_name_the_api_sends(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """`pr_url` has no `url` alias, and this is what says so.

    `_unwrap` tolerates several list envelopes because the spec names none — the tolerance stands in
    for a fact nobody had. This field is not that: the name is known, so accepting the old one would
    only let a future rename pass unnoticed. A body under the old name is a protocol violation and
    fails the parse.
    """
    devin_api.responds(
        "GET",
        SESSION_URL,
        200,
        a_session(pull_requests=[{"url": PR_URL, "number": PR_NUMBER}]),
    )

    with pytest.raises(DevinResponseError):
        await client.get_session(SESSION_ID)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/taxpon/superset/pull/7", 7),
        ("https://github.com/taxpon/superset/pull/42889", 42889),
        ("https://github.com/taxpon/superset/pull/7/files", 7),
        ("https://github.com/taxpon/superset/pull/7#discussion_r1", 7),
        ("https://github.com/taxpon/superset/pull/7?w=1", 7),
        # A GitHub Enterprise install spells the path the same way, so the host is not matched.
        ("https://github.example.com/taxpon/superset/pull/7", 7),
        # Nothing here is a pull request number, and none of them may be invented.
        ("https://github.com/taxpon/superset/pulls", None),
        ("https://github.com/taxpon/superset/issues/7", None),
        ("https://github.com/taxpon/superset/pull/", None),
        ("https://github.com/taxpon/superset/pull/head", None),
        ("", None),
    ],
)
def test_a_pull_request_number_is_derived_from_its_url(url: str, expected: int | None) -> None:
    assert pull_request_number(url) == expected


# --- The review-fix loop --------------------------------------------------------------------------


async def test_send_message_posts_to_the_existing_session(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """The loop resumes the session it has; `resumable: true` is what makes that possible."""
    devin_api.responds("POST", MESSAGES_URL, 200, {"ok": True})
    message = pb.ci_failure_message(
        sha="9c1d2e3", job_name="pytest (scoped)", log="E   assert 1 == 2", cycle=2, max_cycles=3
    )

    await client.send_message(SESSION_ID, message)

    sent = devin_api.only("POST", MESSAGES_URL)
    assert sent.json == {"message": message}
    assert "This is fix cycle 2 of 3" in sent.json["message"]


async def test_tag_session_appends_lifecycle_tags(client: DevinClient, devin_api: FakeAPI) -> None:
    devin_api.responds("POST", SESSION_TAGS_URL, 200, {"ok": True})

    await client.tag_session(SESSION_ID, [pb.cycle_tag(2), pb.outcome_tag("merged")])

    assert devin_api.only("POST", SESSION_TAGS_URL).json == {"tags": ["cycle:2", "outcome:merged"]}


async def test_a_tag_outside_the_vocabulary_never_reaches_devin(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """An unregistered tag is a `422` at Devin (B7). It is refused here instead."""
    with pytest.raises(pb.UnregisteredTag):
        await client.tag_session(SESSION_ID, ["priority:high"])

    assert devin_api.requests == []


# --- Bootstrap ------------------------------------------------------------------------------------


async def test_register_tags_puts_the_whole_vocabulary(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """What `make bootstrap-devin` registers is T15's vocabulary, not a second copy of it."""
    devin_api.responds("PUT", ORG_TAGS_URL, 200, {"ok": True})
    vocabulary = [pb.NAMESPACE_TAG, *(f"{prefix}:" for prefix in pb.TAG_PREFIXES)]

    await client.register_tags(vocabulary)

    sent = devin_api.only("PUT", ORG_TAGS_URL)
    assert sent.json == {
        "tags": ["sentinel", "repo:", "issue:", "class:", "run:", "cycle:", "outcome:"]
    }


@pytest.mark.parametrize(
    "body",
    [
        {"tags": ["sentinel", "team:platform"]},
        ["sentinel", "team:platform"],
    ],
)
async def test_list_tags_reads_the_vocabulary_a_registration_would_replace(
    client: DevinClient, devin_api: FakeAPI, body: Any
) -> None:
    """The read `--dry-run` needs in order to say what the `PUT` above would take away. The
    envelope is unverified (B8), so the documented `tags` object and a bare array both parse — as
    they do for every other listing here."""
    devin_api.responds("GET", ALLOWED_TAGS_URL, 200, body)

    result = await client.list_tags()

    assert result.available
    assert result.value.tags == ("sentinel", "team:platform")
    assert devin_api.only("GET", ALLOWED_TAGS_URL).path == ALLOWED_TAGS_URL


def test_the_vocabulary_is_read_where_the_reference_documents_it() -> None:
    """And not where the registration writes. The v3 reference puts every method on the vocabulary
    under the enterprise-prefixed path and documents nothing under the organisation one, which
    `docs/05-devin-integration.md` records as open. This is what stops the read drifting onto the
    unverified path to match the write."""
    assert ALLOWED_TAGS == "/v3/enterprise/organizations/{org_id}/tags"
    assert ALLOWED_TAGS != ORGANIZATION_TAGS


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, Unavailability.FORBIDDEN), (404, Unavailability.NOT_FOUND)],
)
async def test_reading_the_vocabulary_degrades_rather_than_failing_the_preview(
    client: DevinClient, devin_api: FakeAPI, status: int, reason: Unavailability
) -> None:
    """The likely answer: the documented permission is `ManageEnterpriseSettings`, which a service
    user scoped to one organisation may not carry. A dry run that stopped here would say nothing
    about the three steps below it."""
    devin_api.responds("GET", ALLOWED_TAGS_URL, status, text="no")

    result = await client.list_tags()

    assert isinstance(result, Unavailable)
    assert result.capability is Capability.TAG_DISCOVERY
    assert result.reason is reason
    assert result.status_code == status


async def test_a_rejected_token_on_the_vocabulary_read_still_raises(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("GET", ALLOWED_TAGS_URL, 401, text="invalid token")

    with pytest.raises(DevinAPIError):
        await client.list_tags()


async def test_create_knowledge_note_returns_the_id_config_needs(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """`KnowledgeNoteResponse` names the id `note_id`, and `KnowledgeNoteCreateRequest` requires
    `trigger` — not the `trigger_description` this used to send, which the reference does not
    define at all."""
    devin_api.responds(
        "POST", KNOWLEDGE_URL, 201, {"note_id": "note-tests", "name": "Running tests"}
    )

    note = await client.create_knowledge_note(
        name="Running tests", body="pytest -m 'not slow'", trigger="running the suite"
    )

    assert note.id == "note-tests"
    assert devin_api.only("POST", KNOWLEDGE_URL).json == {
        "name": "Running tests",
        "body": "pytest -m 'not slow'",
        "trigger": "running the suite",
    }


async def test_create_schedule_sends_the_nightly_sweep_as_specified(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """Every field from the `Scheduled sweep` table, tags included.

    The tags are the point. `class:scheduled-sweep` is in that table and is *not* an issue class,
    so the session tag rule rejects it while Devin — which registered the bare `class:` prefix —
    accepts it. A test that passed only the namespace tag left the bootstrap call unbuildable.
    """
    devin_api.responds("POST", SCHEDULES_URL, 201, {"scheduled_session_id": "sched-1"})
    prompt = "Run pip-audit and npm audit on the target repo."

    schedule = await client.create_schedule(
        name=backticked(SPEC_SWEEP["name"])[0],
        prompt=prompt,
        frequency=backticked(SPEC_SWEEP["frequency"])[0],
        tags=backticked(SPEC_SWEEP["tags"]),
        schedule_type=backticked(SPEC_SWEEP["schedule_type"])[0],
        notify_on=backticked(SPEC_SWEEP["notify_on"])[0],
    )

    assert schedule.id == "sched-1"
    assert devin_api.only("POST", SCHEDULES_URL).json == {
        "name": "sentinel-nightly-vuln-sweep",
        "schedule_type": "recurring",
        "frequency": "0 3 * * *",
        "prompt": prompt,
        "tags": ["sentinel", "class:scheduled-sweep"],
        "notify_on": "failure",
    }


def test_the_two_tag_rules_differ_only_where_they_must() -> None:
    """`registered_tag` is the vocabulary Devin was given; `validate_tag` is the stricter rule a
    session needs. The sweep's class tag is exactly the case that separates them."""
    assert registered_tag("class:scheduled-sweep") == "class:scheduled-sweep"
    with pytest.raises(pb.UnregisteredTag):
        pb.validate_tag("class:scheduled-sweep")

    for tag in pb.session_tags(
        repo="taxpon/superset", issue_number=42, issue_class="security", delivery_id=DELIVERY_ID
    ):
        assert registered_tag(tag) == tag

    for outside in ("priority:high", "sentinel-ish", "class:", ":42", ""):
        with pytest.raises(pb.UnregisteredTag):
            registered_tag(outside)


async def test_a_schedule_tag_outside_the_vocabulary_never_reaches_devin(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    with pytest.raises(pb.UnregisteredTag):
        await client.create_schedule(
            name="sweep", prompt="…", frequency="0 3 * * *", tags=["priority:high"]
        )

    assert devin_api.requests == []


async def test_a_listing_filter_outside_the_vocabulary_never_reaches_devin(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    with pytest.raises(pb.UnregisteredTag):
        await client.list_sessions(tags=["priority:high"])

    assert devin_api.requests == []


async def test_a_listing_can_filter_on_the_sweep_tag(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """Searching for the sweep's own sessions is legitimate, so the filter takes the wider rule.

    `SessionsQueryParams.tags` is an array, so the tags go out as a repeated parameter. Joined with
    a comma — which is what this sent — they are one tag with a comma in its name, and match
    nothing.
    """
    devin_api.responds("GET", SESSIONS_URL, 200, {"items": []})

    await client.list_sessions(tags=[pb.NAMESPACE_TAG, "class:scheduled-sweep"])

    sent = devin_api.only("GET", SESSIONS_URL)
    assert sent.url.params.get_list("tags") == ["sentinel", "class:scheduled-sweep"]


# --- Retries --------------------------------------------------------------------------------------


async def test_a_rate_limited_request_backs_off_and_retries(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps
) -> None:
    devin_api.route("POST", SESSIONS_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(201, json=a_session())]
    )

    session = await client.create_session(**ISSUE)

    assert session.session_id == SESSION_ID
    assert len(devin_api.sent("POST", SESSIONS_URL)) == 2
    # One wait, jittered across the top half of the first ceiling (0.5 s).
    assert len(sleeps.delays) == 1
    assert 0.25 <= sleeps.delays[0] <= 0.5


async def test_retry_after_wins_over_the_computed_backoff(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps
) -> None:
    """Devin knows when its window resets and we do not."""
    devin_api.route("GET", SESSION_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=a_session()),
        ]
    )

    await client.get_session(SESSION_ID)

    assert sleeps.delays == [7.0]


async def test_an_unreadable_retry_after_falls_back_to_the_backoff(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps
) -> None:
    """The HTTP-date form needs the server's clock to agree with ours, so it is not acted on."""
    devin_api.route("GET", SESSION_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json=a_session()),
        ]
    )

    await client.get_session(SESSION_ID)

    assert 0.25 <= sleeps.delays[0] <= 0.5


async def test_server_errors_are_retried_until_the_policy_is_exhausted(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps
) -> None:
    devin_api.responds("GET", SESSION_URL, 503, text="upstream unavailable")

    with pytest.raises(DevinAPIError) as raised:
        await client.get_session(SESSION_ID)

    assert raised.value.status_code == 503
    assert raised.value.retryable
    assert raised.value.body == "upstream unavailable"
    assert len(devin_api.sent("GET", SESSION_URL)) == 3
    # Two waits, and the ceiling doubles: 0.5 s then 1 s.
    assert len(sleeps.delays) == 2
    assert 0.25 <= sleeps.delays[0] <= 0.5
    assert 0.5 <= sleeps.delays[1] <= 1.0


async def test_a_connection_that_never_answered_is_retried(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps
) -> None:
    devin_api.route("GET", SESSION_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(DevinTransportError) as raised:
        await client.get_session(SESSION_ID)

    assert raised.value.retryable
    assert len(devin_api.sent("GET", SESSION_URL)) == 3
    assert len(sleeps.delays) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_a_client_error_fails_without_retrying(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps, status: int
) -> None:
    """A body Devin rejected will not become well-formed; retrying it only spends quota."""
    devin_api.responds("POST", SESSIONS_URL, status, text='{"detail":"tag not registered"}')

    with pytest.raises(DevinAPIError) as raised:
        await client.create_session(**ISSUE)

    assert raised.value.status_code == status
    assert not raised.value.retryable
    assert raised.value.body == '{"detail":"tag not registered"}'
    assert len(devin_api.sent("POST", SESSIONS_URL)) == 1
    assert sleeps.delays == []


async def test_the_error_names_the_templated_path_not_the_session(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """The message is built from the template, which is also the Prometheus label: an id in either
    would be one time series, and one unsearchable log line, per session."""
    devin_api.responds("GET", SESSION_URL, 422, text="nope")

    with pytest.raises(DevinAPIError) as raised:
        await client.get_session(SESSION_ID)

    assert SESSION_ID not in str(raised.value)
    assert raised.value.endpoint == SESSION


async def test_every_attempt_is_measured(
    client: DevinClient, devin_api: FakeAPI, registry: CollectorRegistry
) -> None:
    devin_api.route("POST", SESSIONS_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(201, json=a_session())]
    )

    await client.create_session(**ISSUE)

    def observations(outcome: str) -> float | None:
        return registry.get_sample_value(
            "sentinel_devin_request_duration_seconds_count",
            {"method": "POST", "endpoint": SESSIONS, "outcome": outcome},
        )

    assert observations("rate_limited") == 1.0
    assert observations("success") == 1.0


def test_a_retry_policy_must_allow_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(attempts=0)


def test_the_default_policy_is_the_one_production_runs() -> None:
    """Every other test here injects a policy, so these four numbers — the ones a deployment
    actually uses — would otherwise be pinned by nothing.

    They are not arbitrary. A job holds its lease for `JOB_LEASE_TIMEOUT_SECONDS`, and the worst
    case for one call is every attempt timing out plus the whole backoff budget. That has to stay
    comfortably inside the lease, or a worker loses a job it is still working on.
    """
    assert DEFAULT_RETRY.attempts == 3
    assert DEFAULT_RETRY.base_delay == 0.5
    assert DEFAULT_RETRY.max_delay == 30.0
    assert DEFAULT_RETRY.max_total_delay == 60.0

    worst_case = DEFAULT_RETRY.attempts * SPEC_TIMEOUT_SECONDS + DEFAULT_RETRY.max_total_delay
    assert worst_case == 150.0
    assert worst_case < make_settings().job_lease_timeout_seconds


async def test_a_long_retry_after_is_capped(
    client: DevinClient, devin_api: FakeAPI, sleeps: Sleeps
) -> None:
    """A header we misread — or a genuine hour-long window — must not stall a worker."""
    devin_api.route("GET", SESSION_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200, json=a_session()),
        ]
    )

    await client.get_session(SESSION_ID)

    assert sleeps.delays == [30.0]


async def test_the_total_backoff_of_one_call_is_bounded(
    devin_settings: Settings, devin_api: FakeAPI, metrics: Metrics, sleeps: Sleeps
) -> None:
    """`max_delay` bounds a single sleep. Without a total, raising `attempts` would silently raise
    the wall-clock cost of a call past the lease that protects it."""
    devin_api.responds("GET", SESSION_URL, 429, headers={"Retry-After": "30"})
    policy = RetryPolicy(attempts=6, base_delay=0.5, max_delay=30.0, max_total_delay=60.0)

    async with DevinClient(devin_settings, retry=policy, metrics=metrics, sleep=sleeps) as devin:
        with pytest.raises(DevinAPIError):
            await devin.get_session(SESSION_ID)

    assert len(devin_api.sent("GET", SESSION_URL)) == 6
    assert sum(sleeps.delays) == pytest.approx(60.0)
    assert sleeps.delays[-1] == 0.0


# --- Degradation ----------------------------------------------------------------------------------


def test_the_capability_vocabulary_is_the_degradation_table() -> None:
    """Including the fallback text, which is what the dashboard labels a derived figure with."""
    assert [capability.fallback for capability in Capability] == [
        row[2] for row in SPEC_DEGRADATION_ROWS
    ]


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, Unavailability.FORBIDDEN), (404, Unavailability.NOT_FOUND)],
)
async def test_consumption_degrades_rather_than_failing_the_caller(
    client: DevinClient, devin_api: FakeAPI, status: int, reason: Unavailability
) -> None:
    devin_api.responds("GET", CONSUMPTION_URL, status, text="forbidden")

    result = await client.daily_consumption()

    assert isinstance(result, Unavailable)
    assert not result.available
    assert result.capability is Capability.ACU_SPEND
    assert result.reason is reason
    assert result.status_code == status
    assert result.fallback == "Sum `acus_consumed` across sessions"


def billing_day(day: dt.date) -> int:
    """The epoch `ConsumptionByDateResponse.date` carries for `day`.

    The reference types the field `integer` and says what the number means: "Billing cycles use
    midnight PST (Pacific Standard Time) as the day boundary, which corresponds to 08:00:00 UTC".
    An 08:00 instant is what a real response sends, and it is also the case an ISO-string fixture
    could never have caught — `dt.date` refuses a timestamp with a time on it.
    """
    return int(dt.datetime.combine(day, dt.time(8), dt.UTC).timestamp())


async def test_consumption_parses_the_daily_spend(client: DevinClient, devin_api: FakeAPI) -> None:
    devin_api.responds(
        "GET",
        CONSUMPTION_URL,
        200,
        {
            "total_acus": 42.5,
            "consumption_by_date": [
                {
                    "date": billing_day(dt.date(2026, 8, 6)),
                    "acus": 12.5,
                    "acus_by_product": {"devin": 12.5, "cascade": 0.0, "terminal": 0.0},
                },
                {
                    "date": billing_day(dt.date(2026, 8, 7)),
                    "acus": 30.0,
                    "acus_by_product": {"devin": 28.0, "cascade": 2.0, "terminal": 0.0},
                },
            ],
        },
    )

    result = await client.daily_consumption()

    assert result.available
    assert result.value.total_acus == 42.5
    assert result.value.acus_on(dt.date(2026, 8, 7)) == 30.0
    assert result.value.acus_on(dt.date(2026, 1, 1)) == 0.0


async def test_enterprise_metrics_are_not_attempted_without_enterprise_scope(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """No `DEVIN_ENTERPRISE_ID` means a `403` on every dashboard refresh, for a known answer."""
    result = await client.session_metrics(**METRICS_WINDOW)

    assert isinstance(result, Unavailable)
    assert result.reason is Unavailability.NOT_CONFIGURED
    assert result.status_code is None
    assert result.fallback == "Compute from Sentinel's own `remediation` table"
    assert devin_api.requests == []


@pytest.fixture
async def enterprise_client(devin_api: FakeAPI, metrics: Metrics) -> AsyncIterator[DevinClient]:
    settings = make_settings(
        devin_org_id=ORG,
        devin_playbook_ids=PLAYBOOK_IDS,
        devin_enterprise_id="ent-9000",
    )
    async with DevinClient(settings, metrics=metrics) as devin:
        yield devin


async def test_enterprise_metrics_parse_the_two_aggregates(
    enterprise_client: DevinClient, devin_api: FakeAPI
) -> None:
    """`SessionMetricsResponse` calls the session total `sessions_created_count`; the
    `sessions_count` this fixture used to send exists nowhere in v3."""
    devin_api.responds(
        "GET",
        ENTERPRISE_SESSION_METRICS,
        200,
        {
            "sessions_created_count": 9,
            "sessions_created_by_size": {"xs": 1, "s": 2, "m": 3, "l": 2, "xl": 1},
            "sessions_created_by_origin": {"api": 9},
            "sessions_created_with_playbook_count": 9,
            "sessions_created_with_search_count": 0,
            "sessions_with_merged_prs_count": 6,
            "sessions_with_merged_prs_by_size": {"xs": 1, "s": 2, "m": 3},
            "avg_acus_per_session": 8.25,
        },
    )

    result = await enterprise_client.session_metrics(**METRICS_WINDOW)

    assert result.available
    assert result.value.sessions_with_merged_prs_count == 6
    assert result.value.avg_acus_per_session == 8.25
    assert result.value.sessions_created_count == 9


async def test_the_metrics_window_is_sent_because_the_reference_requires_it(
    enterprise_client: DevinClient, devin_api: FakeAPI
) -> None:
    """`time_before` and `time_after` are the only query parameters in this audit the reference
    marks `required: true`. Omitting them is a `422`, which `DEGRADES` does not turn into a
    fallback, so the panel would have raised rather than degraded."""
    devin_api.responds("GET", ENTERPRISE_SESSION_METRICS, 403, text="missing ViewAccountMetrics")

    await enterprise_client.session_metrics(**METRICS_WINDOW)

    assert dict(devin_api.only("GET", ENTERPRISE_SESSION_METRICS).url.params) == {
        "time_after": str(METRICS_WINDOW["time_after"]),
        "time_before": str(METRICS_WINDOW["time_before"]),
    }


async def test_a_missing_permission_degrades_the_metrics_panel(
    enterprise_client: DevinClient, devin_api: FakeAPI
) -> None:
    """B5: the service user may lack `ViewAccountMetrics`. The dashboard derives the figures."""
    devin_api.responds("GET", ENTERPRISE_SESSION_METRICS, 403, text="missing ViewAccountMetrics")

    result = await enterprise_client.session_metrics(**METRICS_WINDOW)

    assert isinstance(result, Unavailable)
    assert result.capability is Capability.SESSION_METRICS
    assert result.reason is Unavailability.FORBIDDEN


async def test_a_rejected_token_is_a_fault_not_a_degradation(
    enterprise_client: DevinClient, devin_api: FakeAPI
) -> None:
    """Falling back on `401` would hide a misconfigured credential behind a derived number."""
    devin_api.responds("GET", ENTERPRISE_SESSION_METRICS, 401, text="invalid token")

    with pytest.raises(DevinAPIError):
        await enterprise_client.session_metrics(**METRICS_WINDOW)


@pytest.mark.parametrize(
    ("capability", "path", "body"),
    [
        (Capability.ACU_SPEND, CONSUMPTION_URL, {"unexpected": "shape"}),
        (Capability.SESSION_METRICS, ENTERPRISE_SESSION_METRICS, {"unexpected": "shape"}),
    ],
    ids=["consumption", "metrics"],
)
async def test_a_body_we_cannot_read_degrades_rather_than_raising(
    enterprise_client: DevinClient,
    devin_api: FakeAPI,
    capability: Capability,
    path: str,
    body: dict[str, Any],
) -> None:
    """These two endpoints are the ones whose field names are guesses (B8), so the likeliest
    failure is a guess being wrong — and the spec's answer to an unavailable capability is a
    labelled fallback, not a dashboard that errors. A session in the same state still raises."""
    devin_api.responds("GET", path, 200, body)

    result = await (
        enterprise_client.daily_consumption()
        if capability is Capability.ACU_SPEND
        else enterprise_client.session_metrics(**METRICS_WINDOW)
    )

    assert isinstance(result, Unavailable)
    assert result.capability is capability
    assert result.reason is Unavailability.UNREADABLE
    assert result.status_code is None
    assert result.fallback == capability.fallback


async def test_listing_playbooks_reads_the_title_and_the_id(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """The one call that answers "which id do I put in `DEVIN_PLAYBOOK_IDS`?" (B6). The response is
    the v3 reference's `PaginatedResponse[PlaybookResponse]`, whose body field is deliberately not
    modelled: the playbook texts live in `docs/playbooks/`."""
    devin_api.responds(
        "GET",
        PLAYBOOKS_URL,
        200,
        {
            "items": [
                {
                    "playbook_id": "playbook-1a2b",
                    "title": "security-fix",
                    "body": "## Overview\n…",
                    "access_type": "org",
                }
            ],
            "has_next_page": False,
            "total": 1,
        },
    )

    result = await client.list_playbooks()

    assert result.available
    page = result.value
    assert [(book.title, book.playbook_id) for book in page.playbooks] == [
        ("security-fix", "playbook-1a2b")
    ]
    assert page.playbooks[0].access_type == "org"
    assert not page.has_next_page
    # Read-only, and the whole reason this endpoint is reachable at all: B6 is about the write.
    assert [request.method for request in devin_api.sent(path=PLAYBOOKS_URL)] == ["GET"]


async def test_listing_playbooks_carries_the_flag_that_there_are_more(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """One page is asked for, so `has_next_page` is what tells an operator the list is partial
    rather than the list being silently short."""
    devin_api.responds(
        "GET",
        PLAYBOOKS_URL,
        200,
        {"items": [{"playbook_id": "pb-1", "title": "security-fix"}], "has_next_page": True},
    )

    result = await client.list_playbooks()

    assert result.available
    assert result.value.has_next_page


@pytest.mark.parametrize("envelope", ["items", None])
async def test_listing_playbooks_parses_the_paginated_envelope(
    client: DevinClient, devin_api: FakeAPI, envelope: str | None
) -> None:
    """`PaginatedResponse[PlaybookResponse]` puts the playbooks in `items`."""
    books = [{"playbook_id": "pb-1", "title": "security-fix"}]
    devin_api.responds("GET", PLAYBOOKS_URL, 200, books if envelope is None else {envelope: books})

    result = await client.list_playbooks()

    assert result.available
    assert result.value.playbooks[0].playbook_id == "pb-1"


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, Unavailability.FORBIDDEN), (404, Unavailability.NOT_FOUND)],
)
async def test_listing_playbooks_degrades_when_the_permission_is_missing(
    client: DevinClient, devin_api: FakeAPI, status: int, reason: Unavailability
) -> None:
    """The expected answer if the service user does not carry the playbook permission. It is an
    answer, so it degrades to the fallback rather than failing the caller."""
    devin_api.responds("GET", PLAYBOOKS_URL, status, text="no")

    result = await client.list_playbooks()

    assert isinstance(result, Unavailable)
    assert result.capability is Capability.PLAYBOOK_DISCOVERY
    assert result.reason is reason
    assert result.status_code == status
    assert result.fallback == (
        "Open each playbook in the Devin web app and read its id from the page"
    )


async def test_a_rejected_token_on_the_playbook_listing_still_raises(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """`401` is not a missing permission, and answering it with "read them in the web app" would
    send an operator hunting for pages their token cannot open either."""
    devin_api.responds("GET", PLAYBOOKS_URL, 401, text="invalid token")

    with pytest.raises(DevinAPIError):
        await client.list_playbooks()


async def test_a_session_we_cannot_read_still_raises(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """The other half of that asymmetry: there is no fallback for a session."""
    devin_api.responds("GET", SESSION_URL, 200, {"unexpected": "shape"})

    with pytest.raises(DevinResponseError):
        await client.get_session(SESSION_ID)


@pytest.mark.parametrize("envelope", ["consumption_by_date", None])
async def test_consumption_parses_the_envelope_the_reference_names(
    client: DevinClient, devin_api: FakeAPI, envelope: str | None
) -> None:
    """`ConsumptionResponse` wraps the days in `consumption_by_date`. The four envelopes this used
    to accept — `days`, `consumption`, `daily`, `data` — were guesses, and none of them was the
    right one, so the budget guard would have used its fallback for ever."""
    day = [{"date": billing_day(dt.date(2026, 8, 7)), "acus": 30.0}]
    devin_api.responds("GET", CONSUMPTION_URL, 200, day if envelope is None else {envelope: day})

    result = await client.daily_consumption()

    assert result.available
    assert result.value.total_acus == 30.0
    assert result.value.acus_on(dt.date(2026, 8, 7)) == 30.0


@pytest.mark.parametrize("unit", [1, 1000], ids=["seconds", "milliseconds"])
async def test_a_billing_day_is_read_from_its_epoch_in_either_unit(
    client: DevinClient, devin_api: FakeAPI, unit: int
) -> None:
    """The reference types `date` `integer` without saying which unit, and both readings fail
    silently — a value read in the wrong one lands in 1970 and `acus_on(today)` returns 0.0."""
    day = billing_day(dt.date(2026, 8, 7)) * unit
    devin_api.responds(
        "GET", CONSUMPTION_URL, 200, {"consumption_by_date": [{"date": day, "acus": 12.5}]}
    )

    result = await client.daily_consumption()

    assert result.available
    assert result.value.acus_on(dt.date(2026, 8, 7)) == 12.5


@pytest.mark.parametrize("envelope", ["items", None])
async def test_a_listing_parses_the_paginated_envelope(
    client: DevinClient, devin_api: FakeAPI, envelope: str | None
) -> None:
    """`PaginatedResponse[SessionResponse]` puts the sessions in `items`."""
    page = [a_session()]
    devin_api.responds("GET", SESSIONS_URL, 200, page if envelope is None else {envelope: page})

    assert len(await client.list_sessions()) == 1


async def test_a_knowledge_note_id_is_read_from_note_id(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("POST", KNOWLEDGE_URL, 201, {"note_id": "note-tests"})

    note = await client.create_knowledge_note(name="Running tests", body="pytest", trigger="tests")

    assert note.id == "note-tests"


async def test_a_schedule_id_is_read_from_scheduled_session_id(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("POST", SCHEDULES_URL, 201, {"scheduled_session_id": "sched-1"})

    schedule = await client.create_schedule(
        name="sweep", prompt="…", frequency="0 3 * * *", tags=[pb.NAMESPACE_TAG]
    )

    assert schedule.id == "sched-1"


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (KNOWLEDGE_URL, {"id": "note-tests"}),
        (KNOWLEDGE_URL, {"knowledge_id": "note-tests"}),
        (SCHEDULES_URL, {"id": "sched-1"}),
        (SCHEDULES_URL, {"schedule_id": "sched-1"}),
    ],
    ids=["note.id", "note.knowledge_id", "schedule.id", "schedule.schedule_id"],
)
async def test_a_bootstrap_id_under_a_name_v3_does_not_use_is_a_fault(
    client: DevinClient, devin_api: FakeAPI, url: str, body: dict[str, str]
) -> None:
    """These four spellings were what the models accepted before this audit, and all four were
    invented. Accepting them again would let a fixture drift back to a shape the API never sends
    and take the suite green with it — which is how the bootstrap shipped unable to record either
    id it creates.
    """
    devin_api.responds("POST", url, 201, body)

    with pytest.raises(DevinResponseError):
        if url == KNOWLEDGE_URL:
            await client.create_knowledge_note(name="n", body="b", trigger="t")
        else:
            await client.create_schedule(
                name="sweep", prompt="…", frequency="0 3 * * *", tags=[pb.NAMESPACE_TAG]
            )


async def test_an_unavailable_capability_is_logged_with_its_fallback(
    client: DevinClient, devin_api: FakeAPI, capture: Configure
) -> None:
    logs = capture()
    devin_api.responds("GET", CONSUMPTION_URL, 403, text="forbidden")

    await client.daily_consumption()

    unavailable = [
        record for record in logs.records if record["event"] == "devin.capability.unavailable"
    ]
    assert len(unavailable) == 1
    assert unavailable[0]["capability"] == "acu_spend"
    assert unavailable[0]["reason"] == "forbidden"


# --- The token ------------------------------------------------------------------------------------

# What the client is allowed to put on a log line. A whitelist rather than a search for the token:
# the redaction processor in `observability/logging.py` would scrub a token this module handed to
# structlog, so a test that reads rendered output proves something about *that* module and nothing
# about this one. These assertions run on the event dict as it was passed, before any processor.
LOGGABLE_KEYS = frozenset(
    {
        "event",
        "log_level",
        "method",
        "endpoint",
        "status",
        "attempt",
        "attempts",
        "duration_ms",
        "delay_ms",
        "session_id",
        "issue",
        "capability",
        "reason",
        "detail",
        "fallback",
    }
)


def assert_nothing_sensitive_was_logged(records: list[dict[str, Any]]) -> None:
    assert records, "the client logged nothing, so this proves nothing"
    for record in records:
        extra = set(record) - LOGGABLE_KEYS
        assert not extra, f"{record['event']} logged {sorted(extra)}"
        assert DEVIN_TOKEN not in json.dumps(record, default=str)


def test_the_token_is_not_in_the_repr(devin_settings: Settings) -> None:
    client = DevinClient(devin_settings)

    assert DEVIN_TOKEN not in repr(client)
    assert ORG in repr(client)
    assert DEVIN_TOKEN not in repr(client.__dict__)


async def test_no_header_or_body_is_handed_to_the_logger(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """Every log call site of a successful path, checked before redaction runs."""
    devin_api.route("POST", SESSIONS_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(201, json=a_session())]
    )

    with structlog.testing.capture_logs() as records:
        await client.create_session(**ISSUE)

    assert {record["event"] for record in records} == {
        "devin.request",
        "devin.request.retry",
        "devin.session.created",
    }
    assert_nothing_sensitive_was_logged(records)


async def test_nothing_sensitive_is_logged_on_the_failure_paths(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """The failure and the degradation call sites, including a response that quotes the token."""
    devin_api.responds("POST", SESSIONS_URL, 422, text=f"token {DEVIN_TOKEN} may not use this tag")
    devin_api.responds("GET", CONSUMPTION_URL, 403, text=f"token {DEVIN_TOKEN} is not authorised")

    with structlog.testing.capture_logs() as records:
        with pytest.raises(DevinAPIError):
            await client.create_session(**ISSUE)
        await client.daily_consumption()

    assert "devin.request.failed" in {record["event"] for record in records}
    assert "devin.capability.unavailable" in {record["event"] for record in records}
    assert_nothing_sensitive_was_logged(records)


async def test_the_token_is_sent_and_stays_out_of_the_exception(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """It authenticates the request and appears nowhere else — including in the exception the
    worker records in `remediation_event.detail`."""
    devin_api.responds("POST", SESSIONS_URL, 422, text='{"detail":"tag not registered"}')

    with pytest.raises(DevinAPIError) as raised:
        await client.create_session(**ISSUE)

    assert devin_api.only("POST", SESSIONS_URL).headers["authorization"] == f"Bearer {DEVIN_TOKEN}"
    assert DEVIN_TOKEN not in str(raised.value)
    assert DEVIN_TOKEN not in repr(raised.value)


async def test_a_transport_error_is_rendered_as_its_class(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    """`httpx` renders the URL it failed on, and a proxy URL carries `user:password@`. Only the
    exception's class name is kept, so there is nothing to leak in the first place."""
    devin_api.route("GET", SESSION_URL).mock(
        side_effect=httpx.ConnectError("cannot reach https://sentinel:hunter2@proxy.internal:8080")
    )

    with (
        structlog.testing.capture_logs() as records,
        pytest.raises(DevinTransportError) as raised,
    ):
        await client.get_session(SESSION_ID)

    assert str(raised.value) == f"GET {SESSION}: ConnectError"
    assert "hunter2" not in str(raised.value)
    assert "hunter2" not in json.dumps(records, default=str)


async def test_the_redactor_is_the_second_line_of_defence(
    client: DevinClient, devin_api: FakeAPI, capture: Configure
) -> None:
    """The rendered output, for the layer this module does not own: even a token echoed back by
    Devin does not survive `observability/logging.py`."""
    logs = capture()
    devin_api.responds("GET", SESSION_URL, 403, text=f"token {DEVIN_TOKEN} is not authorised")

    with pytest.raises(DevinAPIError):
        await client.get_session(SESSION_ID)

    assert logs.text
    assert DEVIN_TOKEN not in logs.text
