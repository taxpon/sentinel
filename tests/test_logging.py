"""Observability: the JSON log lines and the Prometheus exposition.

Both subjects are process-global — `structlog.configure` installs one pipeline for the interpreter,
and `prometheus_client.REGISTRY` is a module-level singleton — so nothing here touches either
without owning it. Logging is configured into a buffer and reset on the way out; metrics are built
against a registry created per test, never the default one.

`tests/conftest.py` does not exist yet (T04). The fixtures below are written to be lifted into it
unchanged.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from structlog.processors import NAME_TO_LEVEL

from sentinel.config import Settings
from sentinel.observability.logging import (
    REDACTED,
    bind_correlation,
    clear_correlation,
    configure_logging,
    correlate,
    get_logger,
    secret_values,
)
from sentinel.observability.prom import (
    DEVIN_LATENCY_BUCKETS,
    DevinOutcome,
    Metrics,
    build_metrics,
    render_exposition,
)

# Credentials shaped like the real ones, because the redaction patterns are shaped around them.
DEVIN_TOKEN = "cog_live_9f3a1c7d2b4e6f8a0c5d"
GITHUB_TOKEN = "github_pat_11ABCDEFG0abcdefghijkl_9Zx8Yw7Vu6Ts5Rq4Po3Nm"
WEBHOOK_SECRET = "whsec_7c1e9b40f6d84a2ba5c3e7f19d0b2a68"
DATABASE_URL = "postgresql+asyncpg://sentinel:s3cr3t-db-password@db:5432/sentinel"

CONFIGURATION: dict[str, Any] = {
    "devin_api_token": DEVIN_TOKEN,
    "devin_org_id": "org-abc123",
    "devin_playbook_ids": {"security": "playbook-sec"},
    "github_token": GITHUB_TOKEN,
    "github_webhook_secret": WEBHOOK_SECRET,
    "database_url": DATABASE_URL,
    "log_level": "debug",
}

LEVELS = ("debug", "info", "warning", "error", "critical")


def make_settings(**overrides: Any) -> Settings:
    """A complete configuration, independent of the developer's environment.

    Every field the model requires is passed explicitly, and initialiser arguments win over the
    environment and over `.env`, so nothing here depends on where the suite is run from.
    """
    return Settings(**{**CONFIGURATION, **overrides})


SECRET_FIELDS = tuple(
    name for name in Settings.model_fields if isinstance(getattr(make_settings(), name), SecretStr)
)


@dataclass(frozen=True)
class Capture:
    """The output of a configured logger, with the configuration that produced it."""

    stream: io.StringIO
    settings: Settings

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @property
    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]

    @property
    def last(self) -> dict[str, Any]:
        return self.records[-1]


Configure = Callable[..., Capture]


@pytest.fixture
def capture() -> Iterator[Configure]:
    """Configure logging into a buffer, and put structlog back as it was afterwards.

    The reset is the point: `structlog.configure` is process-wide, so a test that leaves its buffer
    installed makes every later test in the session log into a dead `StringIO`. Bound context
    variables are cleared for the same reason — they outlive the test that bound them.
    """

    def _configure(**overrides: Any) -> Capture:
        settings = make_settings(**overrides)
        stream = io.StringIO()
        configure_logging(settings, stream=stream)
        return Capture(stream=stream, settings=settings)

    yield _configure
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def registry() -> CollectorRegistry:
    """A registry of this test's own, so assertions cannot see another test's observations."""
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> Metrics:
    return build_metrics(registry)


# --------------------------------------------------------------------------- the line itself


def test_each_call_writes_one_json_object_on_one_line(capture: Configure) -> None:
    logs = capture()
    log = get_logger(__name__)

    log.info("webhook.received")
    log.info("job.claimed", kind="create_session")

    assert len(logs.lines) == 2
    assert [record["event"] for record in logs.records] == ["webhook.received", "job.claimed"]


def test_a_message_containing_a_newline_is_still_one_line(capture: Configure) -> None:
    logs = capture()

    get_logger().info("devin.response", body="first\nsecond")

    assert len(logs.lines) == 1
    assert logs.last["body"] == "first\nsecond"


def test_the_line_carries_the_fields_the_spec_shows(capture: Configure) -> None:
    logs = capture()

    with correlate(
        run="8f1c", remediation_id=12, issue=42, issue_class="security", session_id="devin-abc"
    ):
        get_logger().info("devin.session.created", duration_ms=812)

    record = logs.last
    assert record == {
        "ts": record["ts"],
        "level": "info",
        "event": "devin.session.created",
        "run": "8f1c",
        "remediation_id": 12,
        "issue": 42,
        "class": "security",
        "session_id": "devin-abc",
        "duration_ms": 812,
    }


def test_the_timestamp_is_iso_8601_in_utc(capture: Configure) -> None:
    logs = capture()

    get_logger().info("webhook.received")

    assert datetime.fromisoformat(logs.last["ts"]).tzinfo == UTC


def test_the_runbook_grep_finds_the_event(capture: Configure) -> None:
    # docs/09-operations.md greps for exactly this string and pipes the line to jq. A renderer that
    # puts a space after the colon passes every other test in this file and breaks the demo.
    logs = capture()

    get_logger().info("devin.session.created", session_id="devin-abc")

    assert '"event":"devin.session.created"' in logs.text


def test_ordinary_values_keep_their_json_types(capture: Configure) -> None:
    logs = capture()

    get_logger().info(
        "analytics.computed",
        issue=42,
        merged=True,
        acus=12.5,
        blocked_reason=None,
        by_class={"security": [1, 2]},
    )

    record = logs.last
    assert record["issue"] == 42
    assert record["merged"] is True
    assert record["acus"] == 12.5
    assert record["blocked_reason"] is None
    assert record["by_class"] == {"security": [1, 2]}


@pytest.mark.parametrize("configured", LEVELS)
def test_the_configured_level_decides_what_is_written(capture: Configure, configured: str) -> None:
    logs = capture(log_level=configured)
    log = get_logger()

    for level in LEVELS:
        getattr(log, level)(f"at.{level}")

    assert [record["event"] for record in logs.records] == [
        f"at.{level}" for level in LEVELS if NAME_TO_LEVEL[level] >= NAME_TO_LEVEL[configured]
    ]
    assert [record["level"] for record in logs.records] == [
        level for level in LEVELS if NAME_TO_LEVEL[level] >= NAME_TO_LEVEL[configured]
    ]


# --------------------------------------------------------------------------- correlation


def test_correlation_appears_on_every_line_inside_the_block(capture: Configure) -> None:
    logs = capture()
    # Taken before the binding, as a module-level logger would be: the ids reach it all the same.
    log = get_logger(__name__)

    with correlate(run="8f1c", remediation_id=12):
        log.info("job.claimed")
        log.info("devin.session.created")

    assert [(record["run"], record["remediation_id"]) for record in logs.records] == [
        ("8f1c", 12),
        ("8f1c", 12),
    ]


def test_correlation_does_not_outlive_the_block(capture: Configure) -> None:
    logs = capture()

    with correlate(run="8f1c"):
        get_logger().info("job.claimed")
    get_logger().info("worker.idle")

    assert logs.records[0]["run"] == "8f1c"
    assert "run" not in logs.records[1]


def test_correlation_nests_without_dropping_what_is_already_bound(capture: Configure) -> None:
    logs = capture()

    with correlate(run="8f1c", issue=42):
        with correlate(session_id="devin-abc"):
            get_logger().info("devin.session.created")
        get_logger().info("job.finished")

    created, finished = logs.records
    assert (created["run"], created["issue"], created["session_id"]) == ("8f1c", 42, "devin-abc")
    assert finished["run"] == "8f1c"
    assert "session_id" not in finished


async def test_correlation_reaches_a_task_started_inside_the_block(capture: Configure) -> None:
    logs = capture()

    async def poll() -> None:
        get_logger().info("poller.session.checked")

    with correlate(run="8f1c", session_id="devin-abc"):
        await asyncio.gather(poll(), poll())

    assert [(record["run"], record["session_id"]) for record in logs.records] == [
        ("8f1c", "devin-abc"),
        ("8f1c", "devin-abc"),
    ]


def test_a_correlation_id_that_is_not_known_yet_is_not_bound_as_null(capture: Configure) -> None:
    logs = capture()

    with correlate(run="8f1c"):
        get_logger().info("job.claimed")

    assert logs.last["run"] == "8f1c"
    assert set(logs.last) == {"ts", "level", "event", "run"}


def test_bind_correlation_holds_until_it_is_cleared(capture: Configure) -> None:
    logs = capture()
    log = get_logger()

    bind_correlation(run="8f1c", remediation_id=12)
    log.info("job.claimed")
    bind_correlation(session_id="devin-abc")
    log.info("devin.session.created")
    clear_correlation()
    log.info("worker.idle")

    claimed, created, idle = logs.records
    assert (claimed["run"], claimed["remediation_id"]) == ("8f1c", 12)
    assert "session_id" not in claimed
    assert (created["run"], created["session_id"]) == ("8f1c", "devin-abc")
    assert set(idle) == {"ts", "level", "event"}


# --------------------------------------------------------------------------- redaction


class _Row:
    """Something with no JSON representation of its own, whose `str` carries a credential."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def __str__(self) -> str:
        return f"<Row dsn={self.dsn}>"


def _as_a_secretstr(log: Any, settings: Settings) -> None:
    log.info("configuration.loaded", value=settings.devin_api_token)


def _interpolated_into_the_message(log: Any, settings: Settings) -> None:
    log.info(f"calling devin with {settings.devin_api_token.get_secret_value()}")


def _as_a_plain_string(log: Any, settings: Settings) -> None:
    log.info("configuration.loaded", value=settings.github_token.get_secret_value())


def _inside_a_nested_dict(log: Any, settings: Settings) -> None:
    log.info(
        "devin.request",
        request={"headers": {"x-forwarded": settings.devin_api_token.get_secret_value()}},
    )


def _inside_a_list(log: Any, settings: Settings) -> None:
    log.info("devin.request", attempts=[{"note": settings.github_token.get_secret_value()}])


def _as_a_dict_key(log: Any, settings: Settings) -> None:
    log.info("policy.evaluated", counts={settings.github_webhook_secret.get_secret_value(): 1})


def _in_bytes(log: Any, settings: Settings) -> None:
    log.info("webhook.received", body=settings.devin_api_token.get_secret_value().encode())


def _in_an_exception(log: Any, settings: Settings) -> None:
    try:
        raise RuntimeError(f"could not connect to {settings.database_url.get_secret_value()}")
    except RuntimeError:
        log.exception("database.connect.failed")


def _in_an_object_with_no_json_form(log: Any, settings: Settings) -> None:
    log.info("query.failed", row=_Row(settings.database_url.get_secret_value()))


def _in_bound_context(log: Any, settings: Settings) -> None:
    with structlog.contextvars.bound_contextvars(actor=settings.github_token.get_secret_value()):
        log.info("job.claimed")


ROUTES = [
    pytest.param(_as_a_secretstr, id="secretstr"),
    pytest.param(_interpolated_into_the_message, id="interpolated-into-the-message"),
    pytest.param(_as_a_plain_string, id="plain-string"),
    pytest.param(_inside_a_nested_dict, id="nested-dict"),
    pytest.param(_inside_a_list, id="list"),
    pytest.param(_as_a_dict_key, id="dict-key"),
    pytest.param(_in_bytes, id="bytes"),
    pytest.param(_in_an_exception, id="exception"),
    pytest.param(_in_an_object_with_no_json_form, id="object-without-a-json-form"),
    pytest.param(_in_bound_context, id="bound-context"),
]


@pytest.mark.parametrize("emit", ROUTES)
def test_a_credential_never_reaches_the_output(
    capture: Configure, emit: Callable[[Any, Settings], None]
) -> None:
    logs = capture()

    emit(get_logger(), logs.settings)

    # The line was written and it was redacted — not dropped, which would pass vacuously.
    assert logs.records
    assert REDACTED in logs.text
    for secret in secret_values(logs.settings):
        assert secret not in logs.text


@pytest.mark.parametrize("field_name", SECRET_FIELDS)
def test_every_credential_in_the_configuration_is_scrubbed_by_its_value(
    capture: Configure, field_name: str
) -> None:
    # Read off the model rather than listed here, so a SecretStr field added to Settings later is
    # covered by this test on the day it is added.
    logs = capture()
    secret = getattr(logs.settings, field_name).get_secret_value()

    get_logger().info(f"quoting {secret} in the message", value=secret)

    assert secret not in logs.text
    assert logs.last["value"] == REDACTED
    assert REDACTED in logs.last["event"]


def test_the_configuration_holds_the_credentials_this_file_expects() -> None:
    # Guards the test above: if the model stopped using SecretStr, it would pass with no cases.
    assert set(SECRET_FIELDS) == {
        "devin_api_token",
        "github_token",
        "github_webhook_secret",
        "database_url",
    }


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("cog_live_0123456789abcdef", id="devin-service-user-token"),
        pytest.param("ghp_0123456789abcdefghijklmnopqrstuvwxyz", id="github-classic-pat"),
        pytest.param("github_pat_11AAAA0aaaaaaaaaa", id="github-fine-grained-pat"),
        pytest.param("postgresql://user:hunter2@db:5432/other", id="dsn-with-a-password"),
    ],
)
def test_a_credential_shaped_string_is_redacted_even_when_it_is_not_ours(
    capture: Configure, value: str
) -> None:
    # A token read back from an API response, or another installation's DSN quoted in an error, is
    # not in this process's configuration and cannot be scrubbed by value.
    logs = capture()

    get_logger().info("devin.response", detail=value)

    assert value not in logs.text
    assert REDACTED in logs.last["detail"]


@pytest.mark.parametrize(
    "key",
    ["token", "api_key", "apiKey", "authorization", "password", "webhook_secret", "database_url"],
)
def test_a_field_whose_name_says_credential_loses_its_value(capture: Configure, key: str) -> None:
    logs = capture()

    get_logger().info("devin.request", **{key: "an-opaque-value-of-unknown-shape"})

    assert "an-opaque-value-of-unknown-shape" not in logs.text
    assert logs.last[key] == REDACTED


@pytest.mark.parametrize("key", ["session_id", "signature_result", "issue_class"])
def test_a_field_an_operator_needs_is_not_mistaken_for_a_credential(
    capture: Configure, key: str
) -> None:
    logs = capture()

    get_logger().info("webhook.verified", **{key: "visible"})

    assert logs.last[key] == "visible"


def test_a_short_secret_is_scrubbed_even_out_of_ordinary_text(capture: Configure) -> None:
    # Nothing stops an operator configuring a four-character webhook secret in development. Its
    # occurrences elsewhere are redacted with it: a mangled line is the safe direction to fail in.
    logs = capture(github_webhook_secret="abcd")

    get_logger().info("issue.labelled", label="abcdefg")

    assert "abcd" not in logs.text
    assert logs.last["label"] == f"{REDACTED}efg"


@pytest.mark.parametrize("level", LEVELS)
def test_a_credential_is_redacted_at_every_level(capture: Configure, level: str) -> None:
    logs = capture(log_level="debug")

    getattr(get_logger(), level)("configuration.loaded", value=DEVIN_TOKEN)

    assert DEVIN_TOKEN not in logs.text
    assert logs.last["value"] == REDACTED


# --------------------------------------------------------------------------- metrics


def test_the_exposition_names_exactly_the_metrics_the_spec_lists(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    # docs/07-observability.md: "job queue depth, poller lag, Devin API latency and error rate".
    # The product numbers come from /api/analytics/summary and are deliberately not duplicated.
    assert {metric.name for metric in registry.collect()} == {
        "sentinel_job_queue_depth",
        "sentinel_poller_lag_seconds",
        "sentinel_devin_request_duration_seconds",
    }


def test_the_queue_gauge_is_sliced_by_kind_and_status(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    metrics.publish_job_queue_depth({("create_session", "pending"): 3, ("sync_acu", "deferred"): 1})

    assert (
        registry.get_sample_value(
            "sentinel_job_queue_depth", {"kind": "create_session", "status": "pending"}
        )
        == 3
    )
    assert (
        registry.get_sample_value(
            "sentinel_job_queue_depth", {"kind": "sync_acu", "status": "deferred"}
        )
        == 1
    )


def test_a_drained_queue_reports_zero_rather_than_the_depth_it_last_had(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    metrics.publish_job_queue_depth({("create_session", "pending"): 3})

    metrics.publish_job_queue_depth({("create_session", "running"): 1})

    assert (
        registry.get_sample_value(
            "sentinel_job_queue_depth", {"kind": "create_session", "status": "pending"}
        )
        == 0
    )
    assert (
        registry.get_sample_value(
            "sentinel_job_queue_depth", {"kind": "create_session", "status": "running"}
        )
        == 1
    )


def test_a_pair_that_comes_back_reports_its_new_depth(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    metrics.publish_job_queue_depth({("create_session", "pending"): 3})
    metrics.publish_job_queue_depth({})

    metrics.publish_job_queue_depth({("create_session", "pending"): 2})

    assert (
        registry.get_sample_value(
            "sentinel_job_queue_depth", {"kind": "create_session", "status": "pending"}
        )
        == 2
    )


def test_the_poller_lag_is_published_in_seconds(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    metrics.set_poller_lag(42.5)

    assert registry.get_sample_value("sentinel_poller_lag_seconds") == 42.5


def test_the_devin_histogram_counts_and_sums_what_it_observed(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    labels = {"method": "POST", "endpoint": "/v3/organizations/{org_id}/sessions"}

    for seconds in (0.2, 0.4):
        metrics.observe_devin_request(**labels, outcome=DevinOutcome.SUCCESS, seconds=seconds)
    metrics.observe_devin_request(**labels, outcome=DevinOutcome.SERVER_ERROR, seconds=30.0)

    success = {**labels, "outcome": "success"}
    failure = {**labels, "outcome": "server_error"}
    assert registry.get_sample_value("sentinel_devin_request_duration_seconds_count", success) == 2
    assert registry.get_sample_value(
        "sentinel_devin_request_duration_seconds_sum", success
    ) == pytest.approx(0.6)
    assert registry.get_sample_value("sentinel_devin_request_duration_seconds_count", failure) == 1
    # The error rate is read off the same series: one failure in three requests.
    assert registry.get_sample_value("sentinel_devin_request_duration_seconds_sum", failure) == 30.0


def test_the_histogram_buckets_reach_beyond_the_library_default(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    labels = {"method": "POST", "endpoint": "/v3/organizations/{org_id}/sessions"}
    metrics.observe_devin_request(**labels, outcome=DevinOutcome.SUCCESS, seconds=45.0)

    exposed = {
        sample.labels["le"]
        for sample in next(
            metric
            for metric in registry.collect()
            if metric.name == "sentinel_devin_request_duration_seconds"
        ).samples
        if sample.name.endswith("_bucket")
    }

    assert exposed == {*(repr(bound) for bound in DEVIN_LATENCY_BUCKETS), "+Inf"}
    # A 45-second call is distinguishable from a hung one, which the 10 s default ceiling is not.
    assert (
        registry.get_sample_value(
            "sentinel_devin_request_duration_seconds_bucket",
            {**labels, "outcome": "success", "le": "30.0"},
        )
        == 0
    )
    assert (
        registry.get_sample_value(
            "sentinel_devin_request_duration_seconds_bucket",
            {**labels, "outcome": "success", "le": "60.0"},
        )
        == 1
    )


@pytest.mark.parametrize(
    "status, outcome",
    [
        (200, DevinOutcome.SUCCESS),
        (201, DevinOutcome.SUCCESS),
        (204, DevinOutcome.SUCCESS),
        (400, DevinOutcome.CLIENT_ERROR),
        (404, DevinOutcome.CLIENT_ERROR),
        (422, DevinOutcome.CLIENT_ERROR),
        (429, DevinOutcome.RATE_LIMITED),
        (500, DevinOutcome.SERVER_ERROR),
        (503, DevinOutcome.SERVER_ERROR),
    ],
)
def test_a_response_is_classified_the_way_the_retry_policy_splits_it(
    status: int, outcome: DevinOutcome
) -> None:
    # docs/06-event-pipeline.md retries 429 and 5xx and fails other 4xx immediately, so those are
    # the three cases the exposition has to keep apart.
    assert DevinOutcome.from_status(status) is outcome


def test_two_registries_do_not_see_each_other(registry: CollectorRegistry) -> None:
    first = build_metrics(registry)
    second = build_metrics(CollectorRegistry())

    first.set_poller_lag(10.0)
    second.set_poller_lag(20.0)

    assert registry.get_sample_value("sentinel_poller_lag_seconds") == 10.0


def test_the_exposition_is_the_prometheus_text_format(
    registry: CollectorRegistry, metrics: Metrics
) -> None:
    metrics.set_poller_lag(1.0)

    body, content_type = render_exposition(registry)

    assert content_type.startswith("text/plain")
    assert "# HELP sentinel_poller_lag_seconds" in body.decode()
    assert "sentinel_poller_lag_seconds 1.0" in body.decode()


def test_the_process_metrics_are_in_the_registry_that_serves_the_endpoint() -> None:
    # `render_exposition()` defaults to the default registry, and `GET /metrics` calls it with no
    # argument: the process-wide METRICS has to be registered there or the endpoint exposes
    # nothing. Read-only — this test writes no samples into the shared registry.
    exposition = render_exposition()[0].decode()

    for name in (
        "sentinel_job_queue_depth",
        "sentinel_poller_lag_seconds",
        "sentinel_devin_request_duration_seconds",
    ):
        assert f"# HELP {name}" in exposition
