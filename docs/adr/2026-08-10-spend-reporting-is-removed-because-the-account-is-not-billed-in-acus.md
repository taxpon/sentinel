---
title: Spend reporting is removed, because the account it reports on is not billed in ACUs
status: accepted
date: 2026-08-10
type: architecture
areas: [analytics, dashboard, devin, config]
tasks: [T32]
files:
  - src/sentinel/analytics/metrics.py
  - src/sentinel/config.py
  - dashboard/src/api.ts
  - dashboard/src/panels/Kpi.tsx
specs:
  - docs/07-observability.md
  - docs/09-operations.md
  - docs/05-devin-integration.md
supersedes: 2026-08-08-acu-totals-are-summed-from-remediations-and-labelled-by-ledger-coverage
---

# Spend reporting is removed, because the account it reports on is not billed in ACUs

## Context

Every consumption endpoint in the Devin v3 reference is denominated in ACUs. There is no
dollar-denominated endpoint: `GET /v3/organizations/{org_id}/consumption/daily` returns `total_acus`
and a per-day `acus`, and a session returns `acus_consumed`.

Measured against the live API on this account:

- a session the vendor's own UI prices at $3.50 returns `acus_consumed: 0.0`;
- `consumption/daily` returns `{"total_acus": 0.0, "consumption_by_date": []}`.

ACUs appear to be the enterprise contract unit. This is a self-serve account, billed in quota and
on-demand credits, and it reports no ACUs at all.

`cost.acus_total` was `sum(acus_consumed)` over the window, and `acus_per_merged_fix` and
`usd_per_fix` divided that total by the merge count. All three were therefore structurally zero
however much work had been done — and `usd_per_fix` formatted as `$0.00` at the top of the KPI row.
`cost.source` did not detect this: it reports whether `acu_ledger` covers the window, so it read
`devin_consumption_api` over a total of zero.

Cost was never in the requirements. It was added because it seemed to belong beside the other unit
economics.

## Decision

Remove the capability rather than blank it. `cost` leaves the summary payload, the cost panel and
the KPI row's fourth tile are deleted, and `ACU_UNIT_COST_USD` is no longer configuration.

The daily ACU budget guard stays. It is admission policy rather than reporting, it is separately
tested, and whether an inert guard should remain is a decision nobody has made
([the budget guard spends against the highest figure any source
reports](./2026-08-08-the-budget-guard-spends-against-the-highest-figure-any-source-reports.md)).

`remediation.acus_consumed` stays too. It is an observation the poller records from the session
payload, the budget guard reads it, and the live table shows it as the recorded fact it is. A figure
being zero is not a reason to stop recording what the vendor said.

## Alternatives considered

| Option | Why not |
|---|---|
| Blank the figures when no spend is reported, and say why | Built and then withdrawn. A panel whose permanent state is an explanation of its own emptiness is worse than no panel, and the explanation would have been on screen for the whole demo |
| Price the work from a dollar figure inferred elsewhere | There is no such figure in the API. Inferring one from quota or credit consumption would be a number we made up and presented as measured |
| Keep the ACU figures and drop only the dollars | The ACU figures are the ones reported as zero. Dropping the conversion would leave `0.0 ACU per merged fix` beside eight merged fixes |
| Wait for an enterprise contract | The talk is on a date; the account is what it is on that date |

## Consequences

The dashboard now reports only figures Sentinel computes from its own `remediation` and
`remediation_event` rows. Nothing on it is a number a vendor supplied, which is a stronger claim
than the one `cost.source` was making.

`impact.hours_saved` becomes the economic figure in the talk. It rests on stated baselines rather
than on a measurement, and is labelled as an assumption wherever it appears — a weaker claim than
cost per fix pretended to be, and an honest one.

The cost accepted: unit economics are no longer reported at all, so "is this getting cheaper per
fix?" cannot be answered from the dashboard. On an ACU-denominated contract it could be, and this
decision would be worth revisiting.

Two records that governed the removed figures are retired with it: the ACU-total arithmetic this one
supersedes, and [the cost panel's provenance
labelling](./2026-08-08-cost-panel-labels-acu-and-dollar-provenance-separately.md), now `deprecated`
because the panel it constrains no longer exists. Two neighbouring records stay in force and are
unchanged — [an undefined figure is an em dash](./2026-08-08-an-undefined-figure-is-an-em-dash.md)
and [blank a figure whose denominator is
empty](./2026-08-08-blank-a-figure-whose-denominator-is-empty.md) — though the cost figures they use
as worked examples are now historical.

**What would tell us this was wrong:** the account moves to an ACU-denominated contract and
`consumption/daily` starts returning non-zero totals, at which point the figures become measurable
and their absence is a gap rather than a refusal to guess. A reader asking "what did it cost?" and
having no answer at all — rather than a wrong one — is the expected cost of this decision, not
evidence against it.
