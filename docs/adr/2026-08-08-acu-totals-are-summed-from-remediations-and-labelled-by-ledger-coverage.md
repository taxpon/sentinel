---
title: ACU totals are summed from the window's remediations, and cost.source reports ledger coverage
status: superseded
date: 2026-08-08
type: architecture
areas: [analytics, devin]
tasks: [T16]
files: [src/sentinel/analytics/metrics.py]
specs: [docs/07-observability.md, docs/05-devin-integration.md]
supersedes:
---

# ACU totals are summed from the window's remediations, and cost.source reports ledger coverage

## Context

Two places hold ACU figures. `remediation.acus_consumed` is reconciled per session by the poller;
`acu_ledger` is a daily total synced from `GET /v3/organizations/{org_id}/consumption/daily`, which
also feeds the budget guard. The metric table in [07](../07-observability.md) defines ACU spend as
`sum(acus_consumed)` "and daily series from `acu_ledger`", and the degradation table in
[05](../05-devin-integration.md#degradation) names summing `acus_consumed` as the *fallback* for the
consumption endpoint — so the two documents point at different rows for the same number.

The summary schema forces the question to be answered rather than deferred. It carries a single
`cost.acus_total`, no daily ACU series at all, and a `cost.source` of `devin_consumption_api` or
`derived` that the cost panel is required to label the figures with.

The two sources are not interchangeable. `acu_ledger` is a whole-day total for the whole
organisation: it includes Devin sessions Sentinel did not create, and it cannot be cut at a window
boundary that is not midnight. `acus_per_merged_fix` and `usd_per_fix` divide the total by this
window's merges, so an organisation-wide numerator would inflate the cost of every fix by whatever
else the organisation ran that week.

## Decision

`cost.acus_total` is `sum(acus_consumed)` over the remediations the window selects — the figure that
is actually scoped to the work being reported on. `acus_per_merged_fix` and `usd_per_fix` divide
that unrounded total by `funnel.merged` and are rounded once, at the end.

`cost.source` reports provenance, not a second arithmetic path. It is `devin_consumption_api` when
`acu_ledger` holds a row for **every** day the window touches, and `derived` otherwise. A ledger
with a gap cannot vouch for the window, and a ledger that was never synced — no enterprise scope, a
sync job that has not run — makes the totals Sentinel's own reconciliation and nothing more.

## Alternatives considered

| Option | Why not |
|---|---|
| Sum `acu_ledger` over the window's days when it is complete, and fall back to the remediations | The two totals answer different questions, so the cost per fix would jump the moment the sync caught up — and the ledger's answer is the wrong one, being organisation-wide and day-aligned |
| Always report `derived`, since the totals always come from Sentinel's rows | `cost.source` would carry no information, and the reader could no longer tell a window whose consumption data is current from one whose sync has been dead for a week |
| Report `devin_consumption_api` whenever any day of the window is synced | A single synced day would vouch for six unsynced ones. The label is a claim about the window, so it has to hold across the window |

## Consequences

Cost per fix is the cost of the remediations being reported on, and it does not move when unrelated
Devin usage does. The provenance line on the cost panel is a real signal: `derived` means the
consumption sync is not covering this window, which is also what
[09](../09-operations.md#troubleshooting) tells an operator to go and check. The cost is that
`cost.source` is a statement about the ledger's freshness rather than about where `acus_total` was
read from, so an operator who assumes the label switches the arithmetic will be surprised — hence
this record.

**What would tell us this was wrong:** the two totals diverging materially for a window where the
ledger is complete, which would mean the poller's per-session reconciliation is missing spend that
Devin is billing for.
