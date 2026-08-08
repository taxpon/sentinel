---
title: Keep Prometheus metrics process-local and publish cross-process figures as snapshots
status: accepted
date: 2026-08-08
type: architecture
areas: [ops, api]
tasks: [T17, T25]
files: [src/sentinel/observability/prom.py]
specs: [docs/07-observability.md, docs/09-operations.md]
supersedes:
---

# Keep Prometheus metrics process-local and publish cross-process figures as snapshots

## Context

[`07`](../07-observability.md) puts four things behind `GET /metrics`: job queue depth, poller lag,
Devin API latency and Devin API error rate. The endpoint is served by `api`.

`api`, `worker` and `poller` are separate containers off one image ([`09`](../09-operations.md)). A
`prometheus_client` registry is an object in one process: a counter the worker increments is not
visible to the api's registry, and no amount of naming discipline changes that. Two of the four
figures are facts about the database rather than about any process — queue depth is a `job` query,
poller lag is the age of the oldest unreconciled session — while the Devin timings only exist in
whichever process made the call.

The registry is also a module-level singleton, which makes metrics defined at import time
untestable in parallel with the rest of the suite: two tests asserting on the same counter see each
other's observations.

## Decision

`build_metrics(registry)` constructs the whole set against an injected registry; `METRICS` is one
instance of it in the default registry, and `render_exposition()` reads that registry.

Queue depth and poller lag are **published**, not accumulated: whichever process serves the
exposition reads them from the database and sets them. `publish_job_queue_depth` takes a whole
snapshot and zeroes any `(kind, status)` pair it published before and does not name now — a gauge
holds its last value for ever, so a drained kind would otherwise report the depth it had when it
was last seen and an alert on queue depth could never clear.

Devin latency and error rate are one histogram labelled by outcome, observed in the process that
made the call. There is no separate error counter: the histogram's `_count` split by `outcome`
already gives the error rate, and one series cannot disagree with itself about how many requests
there were.

## Alternatives considered

| Option | Why not |
|---|---|
| `prometheus_client` multiprocess mode | Needs a shared directory between processes that are separate containers, and gauges require an aggregation mode chosen per metric |
| A pushgateway | Another component to run and to explain, for four numbers |
| Counters in the database, read at scrape time | That table exists — `remediation_event` — and `/api/analytics/summary` already serves it. Duplicating it in Prometheus is the second source of truth this spec warns against |
| Module-level metric globals | Cannot be built twice, so tests share one set of numbers and cannot assert on any of them independently |
| Raw status code as a label | Unbounded label values; the retry policy only distinguishes four outcomes anyway |

## Consequences

The api's `/metrics` is honest about what that process saw, and queue depth and poller lag cost one
query on the scrape path. The worker and the poller can expose their own exposition when there is a
reason to scrape them; nothing has to be re-plumbed for that. Tests build their own registry and
assert on exact sample values.

The cost is that no figure is summed across processes, and the Devin histogram in the api's output
covers only the api's own calls — a reader who wants all of them must scrape all three services.

**What would tell us this was wrong:** needing to add across processes to answer an operational
question. That number belongs in the database, or Prometheus should be scraping every service.
