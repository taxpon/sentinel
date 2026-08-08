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
import random
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry

from conftest import DEVIN_TOKEN, Configure, FakeAPI, make_settings
from factories import DELIVERY_ID
from sentinel.config import Settings
from sentinel.devin import playbooks as pb
from sentinel.devin.client import (
    CONSUMPTION_DAILY,
    ENDPOINTS,
    ENTERPRISE_SESSION_METRICS,
    KNOWLEDGE_NOTES,
    ORGANIZATION_TAGS,
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
)
from sentinel.devin.schemas import (
    Capability,
    Outcome,
    Risk,
    SessionStatus,
    Unavailability,
    Unavailable,
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
    assert len(SPEC_ENDPOINT_ROWS) == 10
    assert len(SPEC_CREATE_FIELDS) == 10
    assert len(SPEC_DEGRADATION_ROWS) == 3
    assert len(SPEC_STATUSES) == 7
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
KNOWLEDGE_URL = KNOWLEDGE_NOTES.format(org_id=ORG)
SCHEDULES_URL = SCHEDULES.format(org_id=ORG)
CONSUMPTION_URL = CONSUMPTION_DAILY.format(org_id=ORG)

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


def a_session(**overrides: Any) -> dict[str, Any]:
    """A session body shaped as `docs/05-devin-integration.md` says v3 returns one."""
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
        "pull_requests": [{"url": "https://github.com/taxpon/superset/pull/7", "number": 7}],
        **overrides,
    }


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


def test_the_timeout_is_the_documented_thirty_seconds() -> None:
    assert TIMEOUT.connect == 30.0
    assert TIMEOUT.read == 30.0


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
    assert body["title"] == f"[sentinel] #42 {ISSUE['issue_title']}"
    assert body["tags"] == [
        "sentinel",
        "repo:taxpon/superset",
        "issue:42",
        "class:security",
        f"run:{DELIVERY_ID}",
    ]
    assert body["repos"] == ["taxpon/superset"]
    assert body["playbook_id"] == PLAYBOOK_IDS["security-fix"]
    assert body["knowledge_ids"] == list(KNOWLEDGE_IDS)
    assert body["structured_output_schema"] == pb.STRUCTURED_OUTPUT_SCHEMA
    assert body["structured_output_required"] is True
    assert body["max_acu_limit"] == 20
    assert body["resumable"] is True


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
    devin_api.responds("GET", SESSIONS_URL, 200, {"sessions": [a_session(), a_session()]})

    sessions = await client.list_sessions(tags=[pb.NAMESPACE_TAG], limit=50)

    assert len(sessions) == 2
    sent = devin_api.only("GET", SESSIONS_URL)
    assert dict(sent.url.params) == {"tags": pb.NAMESPACE_TAG, "limit": "50"}


async def test_list_sessions_accepts_a_bare_list(client: DevinClient, devin_api: FakeAPI) -> None:
    """The spec names the endpoint but not its envelope (B8), so both shapes parse."""
    devin_api.responds("GET", SESSIONS_URL, 200, [a_session()])

    assert len(await client.list_sessions()) == 1


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


async def test_create_knowledge_note_returns_the_id_config_needs(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("POST", KNOWLEDGE_URL, 201, {"id": "note-tests", "name": "Running tests"})

    note = await client.create_knowledge_note(
        name="Running tests", body="pytest -m 'not slow'", trigger_description="running the suite"
    )

    assert note.id == "note-tests"
    assert devin_api.only("POST", KNOWLEDGE_URL).json == {
        "name": "Running tests",
        "body": "pytest -m 'not slow'",
        "trigger_description": "running the suite",
    }


async def test_create_schedule_sends_the_nightly_sweep(
    client: DevinClient, devin_api: FakeAPI
) -> None:
    devin_api.responds("POST", SCHEDULES_URL, 201, {"schedule_id": "sched-1"})

    schedule = await client.create_schedule(
        name="sentinel-nightly-vuln-sweep",
        prompt="Run pip-audit and npm audit on the target repo.",
        frequency="0 3 * * *",
        tags=[pb.NAMESPACE_TAG],
    )

    assert schedule.id == "sched-1"
    assert devin_api.only("POST", SCHEDULES_URL).json == {
        "name": "sentinel-nightly-vuln-sweep",
        "schedule_type": "recurring",
        "frequency": "0 3 * * *",
        "prompt": "Run pip-audit and npm audit on the target repo.",
        "tags": ["sentinel"],
        "notify_on": "failure",
    }


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


async def test_consumption_parses_the_daily_spend(client: DevinClient, devin_api: FakeAPI) -> None:
    devin_api.responds(
        "GET",
        CONSUMPTION_URL,
        200,
        {
            "consumption": [
                {"date": "2026-08-06", "acus": 12.5},
                {"date": "2026-08-07", "acus": 30.0},
            ]
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
    result = await client.session_metrics()

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
    devin_api.responds(
        "GET",
        ENTERPRISE_SESSION_METRICS,
        200,
        {"sessions_with_merged_prs_count": 6, "avg_acus_per_session": 8.25, "sessions_count": 9},
    )

    result = await enterprise_client.session_metrics()

    assert result.available
    assert result.value.sessions_with_merged_prs_count == 6
    assert result.value.avg_acus_per_session == 8.25


async def test_a_missing_permission_degrades_the_metrics_panel(
    enterprise_client: DevinClient, devin_api: FakeAPI
) -> None:
    """B5: the service user may lack `ViewAccountMetrics`. The dashboard derives the figures."""
    devin_api.responds("GET", ENTERPRISE_SESSION_METRICS, 403, text="missing ViewAccountMetrics")

    result = await enterprise_client.session_metrics()

    assert isinstance(result, Unavailable)
    assert result.capability is Capability.SESSION_METRICS
    assert result.reason is Unavailability.FORBIDDEN


async def test_a_rejected_token_is_a_fault_not_a_degradation(
    enterprise_client: DevinClient, devin_api: FakeAPI
) -> None:
    """Falling back on `401` would hide a misconfigured credential behind a derived number."""
    devin_api.responds("GET", ENTERPRISE_SESSION_METRICS, 401, text="invalid token")

    with pytest.raises(DevinAPIError):
        await enterprise_client.session_metrics()


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


def test_the_token_is_not_in_the_repr(devin_settings: Settings) -> None:
    client = DevinClient(devin_settings)

    assert DEVIN_TOKEN not in repr(client)
    assert ORG in repr(client)
    assert DEVIN_TOKEN not in repr(client.__dict__)


async def test_the_token_is_sent_and_stays_out_of_the_failure_path(
    client: DevinClient, devin_api: FakeAPI, capture: Configure
) -> None:
    """It authenticates the request and appears nowhere else — not in the log of the failure, not
    in the exception the worker records in `remediation_event.detail`."""
    logs = capture()
    devin_api.responds("POST", SESSIONS_URL, 422, text='{"detail":"tag not registered"}')

    with pytest.raises(DevinAPIError) as raised:
        await client.create_session(**ISSUE)

    assert devin_api.only("POST", SESSIONS_URL).headers["authorization"] == f"Bearer {DEVIN_TOKEN}"
    assert DEVIN_TOKEN not in str(raised.value)
    assert DEVIN_TOKEN not in repr(raised.value)
    assert logs.text
    assert DEVIN_TOKEN not in logs.text


async def test_a_response_quoting_the_token_is_still_not_logged(
    client: DevinClient, devin_api: FakeAPI, capture: Configure
) -> None:
    """The client logs the status, never the body — and `observability/logging.py` scrubs the value
    independently, so neither layer alone is what keeps it out."""
    logs = capture()
    devin_api.responds("GET", SESSION_URL, 403, text=f"token {DEVIN_TOKEN} is not authorised")

    with pytest.raises(DevinAPIError):
        await client.get_session(SESSION_ID)

    assert DEVIN_TOKEN not in logs.text
