"""Prometheus metrics, exposed as `GET /metrics` (`docs/07-observability.md#analytics-api`).

The exposition is the **operational** view, and only that: how deep the queue is, how far behind
the poller is, and how slow and how reliable the Devin API is being. The numbers a leader reads —
funnel, MTTR, autonomy, cost — are computed from `remediation` and `remediation_event` and served
by `/api/analytics/summary`. They are deliberately absent here, because the same figure from two
sources is a figure a reader has to reconcile before trusting either.

Latency and error rate are one histogram rather than a histogram plus an error counter: the
`outcome` label already splits the observations, so the error rate is
`rate(..._count{outcome!="success"}[5m]) / rate(..._count[5m])` and the two can never disagree
about how many requests there were.

A registry is process-local, and `api`, `worker` and `poller` are separate processes, so whichever
process serves `/metrics` can only expose what it observed itself. Cross-process figures — queue
depth, poller lag — are published from a database snapshot at scrape time, which is why they are
setters here rather than counters incremented where the work happens. See
`docs/adr/2026-08-08-metrics-are-process-local.md`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Gauge,
    Histogram,
    generate_latest,
)

NAMESPACE: Final = "sentinel"

DEVIN_LATENCY_BUCKETS: Final = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
"""Bucket bounds in seconds. The library's defaults stop at 10 s, which is the wrong shape for this
client: session creation regularly takes longer than that, and every slow call — the only kind
worth alerting on — would land in `+Inf` indistinguishable from a two-minute hang."""


class DevinOutcome(StrEnum):
    """The `outcome` label on a Devin request.

    One member per branch of the reliability policy in `docs/06-event-pipeline.md`, so that "how
    often do we retry, and why" is answerable from the exposition alone. A fixed vocabulary also
    keeps the label bounded: raw status codes would grow a time series per code.
    """

    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"

    @classmethod
    def from_status(cls, status: int) -> DevinOutcome:
        """Classify a response. A request that never got one is `NETWORK_ERROR`, set by the
        caller."""
        if status < 400:
            return cls.SUCCESS
        if status == 429:
            return cls.RATE_LIMITED
        if status < 500:
            return cls.CLIENT_ERROR
        return cls.SERVER_ERROR


@dataclass(frozen=True, slots=True)
class Metrics:
    """The metric objects registered in one registry, with the label vocabulary applied for you.

    Held together rather than exposed as module-level globals so that a test — or a second process
    role in the same interpreter — can build an independent set against its own registry, instead
    of asserting on numbers the rest of the suite is also writing to.
    """

    job_queue_depth: Gauge
    poller_lag_seconds: Gauge
    devin_request_duration_seconds: Histogram
    _published_queue_labels: set[tuple[str, str]] = field(default_factory=set)

    def publish_job_queue_depth(self, depths: Mapping[tuple[str, str], int]) -> None:
        """Publish a whole snapshot of the queue, keyed by `(kind, status)`.

        A snapshot rather than a per-pair setter, because a gauge holds the last value written to a
        label pair for ever: a kind that drains out of the queue would go on reporting the depth it
        had when it was last seen, and an alert on queue depth would never clear. Every pair
        published before is zeroed unless this snapshot names it again.
        """
        for kind, status in self._published_queue_labels - depths.keys():
            self.job_queue_depth.labels(kind=kind, status=status).set(0)
        for (kind, status), depth in depths.items():
            self.job_queue_depth.labels(kind=kind, status=status).set(depth)
        self._published_queue_labels.clear()
        self._published_queue_labels.update(depths)

    def set_poller_lag(self, seconds: float) -> None:
        """Publish how far behind the poller is, in seconds."""
        self.poller_lag_seconds.set(seconds)

    def observe_devin_request(
        self, *, method: str, endpoint: str, outcome: DevinOutcome, seconds: float
    ) -> None:
        """Record one call to the Devin API.

        `endpoint` is the templated path — `/v3/organizations/{org_id}/sessions` — never the one
        with the ids substituted in, which would be a new time series per session.
        """
        self.devin_request_duration_seconds.labels(
            method=method, endpoint=endpoint, outcome=outcome.value
        ).observe(seconds)


def build_metrics(registry: CollectorRegistry) -> Metrics:
    """Register the metrics in `registry`. Calling this twice on one registry is an error."""
    return Metrics(
        job_queue_depth=Gauge(
            "job_queue_depth",
            "Jobs in the queue, by kind and status.",
            ("kind", "status"),
            namespace=NAMESPACE,
            registry=registry,
        ),
        poller_lag_seconds=Gauge(
            "poller_lag_seconds",
            "Age of the oldest session the poller has not yet reconciled, in seconds.",
            namespace=NAMESPACE,
            registry=registry,
        ),
        devin_request_duration_seconds=Histogram(
            "devin_request_duration_seconds",
            "Devin API request duration in seconds, by outcome.",
            ("method", "endpoint", "outcome"),
            namespace=NAMESPACE,
            registry=registry,
            buckets=DEVIN_LATENCY_BUCKETS,
        ),
    )


METRICS: Final = build_metrics(REGISTRY)
"""The metrics of this process, in the default registry `render_exposition` reads."""


def render_exposition(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """The body and `Content-Type` for `GET /metrics`."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
