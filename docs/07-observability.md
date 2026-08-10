# Observability

> **Status:** Design · **Answers:** How is each metric defined, and what does the dashboard actually tell an engineering leader?

The question this has to answer is whether the pipeline is working — asked by someone accountable
for it rather than by someone operating it, and a count of sessions does not answer that. Every
panel below earns its place by changing what such a reader would do next.

## Metric definitions

All figures derive from `remediation` and the append-only `remediation_event` log
([03](./03-data-model.md)), scoped to a time window on `labeled_at`.

| Metric | Definition | Why a leader cares |
|---|---|---|
| **Funnel** | Counts at each stage: labelled → session created → PR opened → CI green → merged | Shows *where* work is lost, not just that it was |
| **Success rate** | `merged / labelled` | End-to-end effectiveness |
| **Merge rate** | `merged / pr_opened` | Quality of what the agent produces, isolated from whether it produced anything |
| **Time to PR** | percentile of `pr_opened_at − labeled_at`, p50 and p90 | How fast the backlog starts moving |
| **MTTR** | percentile of `merged_at − labeled_at`, p50 and p90 | The headline number: issue to fix in production-ready form |
| **Review latency** | percentile of `merged_at − ci_green_at` | Isolates the new bottleneck — human review, not implementation |
| **Throughput** | merged per day, split by `issue_class` | Sustained capacity, and whether it is concentrated in one easy class |
| **Fix cycles** | mean count of `remediation_event` rows where `to_state = RUNNING` and `from_state ∈ {CI_FAILED, CHANGES_REQUESTED}` | How much self-correction each fix needed |
| **Autonomy rate** | `merged with cycle = 0 and human_message_count = 0 / merged` | Share needing no human intervention at all — the delegation signal |
| **Failure breakdown** | count grouped by `blocked_reason`, for `state ∈ {BLOCKED, FAILED}` | Where the system's limits actually are |
| **Engineer-hours saved** | `Σ (baseline_hours[class] × merged_in_class)` | Business impact — a **stated assumption**, labelled as such |

Two deliberate choices:

- **Merge rate is separate from success rate.** Together they distinguish "the agent rarely
  finishes" from "the agent finishes but the work is not mergeable" — different problems with
  different fixes.
- **Blocked is not hidden.** Escalations appear as their own funnel outcome and their own panel.
  A system that hides its failures cannot be evaluated.

Two of the timestamps behind these figures changed meaning on 2026-08-10, so numbers recorded before
that date are not comparable with ones recorded after it:

- **`ci_green_at` now marks the first moment the whole head SHA was green** — the `devin-autofix-ci`
  check succeeded, nothing else failing, nothing still running — rather than the first check suite
  of any kind to conclude `success`
  ([ADR](./adr/2026-08-10-ci-green-is-the-aggregate-of-the-check-runs.md)). It lands later than it
  used to, so **earlier figures read flatteringly early for time to green CI, and correspondingly
  long for review latency.**
- **`merged_at` is stamped whatever state the remediation is in**, so one that escalated and was
  then merged by a human counts in the funnel, merge rate, MTTR and throughput while still appearing
  in the failure breakdown ([ADR](./adr/2026-08-10-a-merge-is-recorded-from-any-state.md)).

## Analytics API

`GET /api/analytics/summary?window=7d` — everything the dashboard needs in one response.

```jsonc
{
  "window": { "from": "2026-08-01T00:00:00Z", "to": "2026-08-08T00:00:00Z" },
  "funnel": { "labelled": 8, "session_created": 8, "pr_opened": 7, "ci_green": 6, "merged": 5 },
  "rates":  { "success": 0.625, "merge": 0.714, "autonomy": 0.60 },
  "durations_seconds": {
    "to_pr":          { "p50": 1980, "p90": 3600 },
    "to_merge":       { "p50": 6480, "p90": 14400 },
    "review_latency": { "p50": 2700, "p90": 7200 }
  },
  "cycles": { "mean": 0.8, "distribution": { "0": 3, "1": 1, "2": 1 } },
  "throughput": [ { "day": "2026-08-06", "by_class": { "security": 1, "flaky-test": 1 } } ],
  "failures":  [ { "reason": "requires_upstream_decision", "count": 1, "issues": [37] } ],
  "impact":    { "hours_saved": 21.0, "assumption": "baseline hours per issue class; see docs/05" },
  "generated_at": "2026-08-08T04:12:03Z"
}
```

Supporting endpoints:

| Endpoint | Returns |
|---|---|
| `GET /api/remediations` | Live table rows: state, class, cycle, ACUs, elapsed, Devin session URL, PR URL |
| `GET /api/remediations/{id}` | Full `remediation_event` timeline for one remediation |
| `GET /metrics` | Prometheus exposition — job queue depth, poller lag, Devin API latency and error rate. Queue depth and lag are read from the database at scrape time, not from counters the serving process kept ([ADR](./adr/2026-08-08-metrics-are-process-local.md)); `poller_lag_seconds` is the age of `poller_heartbeat.ticked_at` ([03](./03-data-model.md)), and reads `0` before the poller has ever run so that a fresh deployment does not alert |
| `GET /healthz` | Liveness, plus `acu_ledger.synced_at` age |

Every figure in the summary is Sentinel's own: computed from `remediation` and `remediation_event`,
which this repository writes and can be checked against. Nothing here is a number a vendor reported.

That is a deliberate narrowing. Spend reporting used to live in this payload — `cost.acus_total`,
`cost.usd_per_fix` and the rest — and it was removed: Devin reports consumption only in ACUs, the
account this runs on is not billed in ACUs, and so every spend figure was structurally zero however
much work had been done. A metric that cannot be measured on the system being reported on does not
belong in the report ([blockers.md](./blockers.md)). The daily ACU budget guard is unaffected — it
is policy rather than reporting, and lives in [06](./06-event-pipeline.md#reliability-policy).

## Dashboard

React + Vite SPA served by `api`, polling `/api/analytics/summary` and `/api/remediations` every
5 seconds. Freshness is shown explicitly — `generated_at` rendered as "updated Ns ago", turning
amber past 30 s. A dashboard that only updates when someone re-runs a script is not observability.

| Panel | Question it answers |
|---|---|
| KPI row — success rate, merge rate, MTTR p50 | Is this working, and how fast? |
| Funnel | Where does work stop? |
| Throughput by day, stacked by class | Is capacity sustained, and is it spread across real problem types? |
| Duration distribution — to-PR vs to-merge vs review latency | Where is the time going now? |
| Autonomy — cycle distribution and intervention rate | How much human attention does each fix still need? |
| **Failure breakdown** | What can it *not* do? |
| Impact — hours saved, with the assumption stated inline | What is this worth? |
| Live remediation table | What is happening right now, and where do I click to verify it? |

Every row in the live table links to both the Devin session and the pull request, so any claim on
the dashboard can be independently checked at source.

Layout constraints: one screen at 1440px without scrolling to reach the KPI row; no chart taller
than 240px; a single accent colour with class colours reserved for the stacked series.

## Logging

Structured JSON to stdout, one event per line.

```jsonc
{ "ts": "…", "level": "info", "event": "devin.session.created",
  "run": "8f1c…", "remediation_id": 12, "issue": 42, "class": "security",
  "session_id": "devin-…", "duration_ms": 812 }
```

`run` is the GitHub delivery id, the same value carried as the `run:` Devin tag
([06](./06-event-pipeline.md)) — one identifier joins GitHub, Sentinel and the Devin dashboard.
Tokens and webhook secrets are never logged, at any level.

Two siblings of that event come from the adopt-or-create lookup
([05](./05-devin-integration.md#adopt-or-create)), and they are what an operator reads when a
remediation's session is not the one they expected:

- **`devin.session.adopted`** — a session already existed for this remediation and was taken rather
  than a second one created. Carries `session_id`, `issue`, `is_archived`, and `matched`, `pages`
  and `seen` describing the walk. `is_archived: true` outside an incident cleanup means the
  remediation is attached to a session somebody stopped by hand, and it will not progress.
- **`devin.session.absent`** — the lookup found nothing, so a session is about to be created.
  Carries `issue`, `pages` and `seen`. It is logged precisely because it is the branch that goes on
  to create: a `seen` of zero against an organisation known to hold sessions is what a `tags` filter
  the server misparses would look like, and nothing else would say so.
