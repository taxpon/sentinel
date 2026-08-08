---
title: The budget guard spends against the highest figure any source reports
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline, devin]
tasks: [T21]
files: [src/sentinel/policy/budget.py]
specs: [docs/06-event-pipeline.md, docs/05-devin-integration.md]
supersedes:
---

# The budget guard spends against the highest figure any source reports

## Context

[06](../06-event-pipeline.md#reliability-policy) has the budget guard compare "today's `acu_ledger`
total plus the class cap" against `DAILY_ACU_BUDGET`, and escalate rather than proceed when it is
over. It is the last check before Sentinel spends money that a person will be billed for.

Three figures describe what today has cost, and each is incomplete in a different way:

- `daily_consumption()` — Devin's own. It degrades to `Unavailable` when the organisation does not
  expose consumption, which is a returned value rather than an exception
  ([ADR](./2026-08-08-enterprise-degradation-is-a-returned-value.md)), so the guard has to have an
  answer for its absence. It is also unverified ([B8](../blockers.md)): no window parameters are
  documented and none are sent, so what comes back may be a figure for some period other than
  today, and that failure is silent.
- `acu_ledger` — the same endpoint's answer, as the `sync_acu` job last stored it. Organisation-
  wide and day-aligned, so it is the only source that sees Devin sessions Sentinel did not create;
  as stale as the last sync, and empty on a deployment where consumption was never available to
  sync from.
- `sum(remediation.acus_consumed)` — Sentinel's own reconciliation, written by the poller from the
  session responses. Always available and always current, and blind to everything but Sentinel's
  own sessions. `docs/05-devin-integration.md#degradation` names it as the fallback for the
  consumption endpoint.

Both local sources are in Postgres and cannot be unavailable. Every one of the three can only be
*missing* spend — none of them can invent it.

## Decision

Today's spend is the **maximum** of the figures available, and `Unavailable` removes a source
rather than relaxing the comparison. The guard refuses when `max(sources) + max_acu_limit` for the
issue class exceeds `DAILY_ACU_BUDGET`; spending exactly the budget is inside it.

The class cap is reserved before the comparison, as the spec says: the guard will not start a
session it could not afford to see run to its own ceiling.

Failures that are not degradations stay failures. A `401`, a `503` or a network error raises out of
the guard and fails the job, which the queue retries with backoff. Only the `403`, `404` and
unreadable-body cases the client already models as `Unavailable` drop the figure.

## Alternatives considered

| Option | Why not |
|---|---|
| Prefer Devin's figure, fall back to the local ones only when it is unavailable | Devin's window is undocumented (B8). A response covering the last hour would read as a nearly empty day and open the gate, and nothing in the response says which period it describes |
| Use `acu_ledger` alone, as the spec's sentence reads literally | A deployment with no consumption scope never syncs the ledger, so the guard would compare a permanent zero against the budget and never refuse anything. The spec's other half — the degradation table — names the remediation sum as the fallback for exactly this |
| Admit when the figure cannot be read, and rely on Devin's per-session `max_acu_limit` | The per-session cap bounds one session, not a day of them. Sixteen sessions of six ACUs each stay inside every per-session ceiling and pass a hundred-ACU daily budget without anything refusing |
| Refuse everything when Devin's figure is unavailable | Fails safe on money and unsafe on the demo: with no enterprise scope the pipeline would block its first issue and every one after it, which is the state B8 says the deployment may well be in |
| Sum the figures | They overlap — the ledger already includes Sentinel's own sessions — so a synced day would count its own spend twice and refuse work that is comfortably inside the budget |

## Consequences

There is no arrangement of missing data that makes the guard more permissive than a complete one
would be: the worst an absent source can do is leave the decision to the other two, and the worst a
stale one can do is under-report while a fresher source over-rules it. The escalation payload and
`remediation_event.detail` carry all three figures, so an operator reading a refusal months later
can see that `acus_devin` was `null` because the endpoint could not be asked, or that the ledger
was far below Sentinel's own total because the sync had stopped.

The cost is that the guard is deliberately conservative: an organisation whose ledger includes a
large unrelated Devin workload will have Sentinel refuse work that its own sessions could afford.
That is the right direction to be wrong in for a cost ceiling, and it is visible when it happens —
the remediation is `BLOCKED` with the figures attached, not silently held back.

Each refusal costs one consumption call, made before the concurrency gate is consulted
([ADR](./2026-08-08-the-policy-releases-the-job-it-refuses.md)).

**What would tell us this was wrong:** the three figures agreeing closely once B8 is resolved and
credentials exist, which would make the maximum an expensive way to read one number; or
remediations blocked while the day's *Sentinel* spend is well inside the budget, which would mean
the organisation-wide ledger is the wrong denominator for a per-pipeline ceiling and the budget
should be compared against Sentinel's own total alone.
