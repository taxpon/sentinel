---
title: A session that ends with nothing to show fails the remediation
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T24]
files: [src/sentinel/pipeline/poller.py]
specs: [docs/04-state-machine.md, docs/06-event-pipeline.md]
supersedes:
---

# A session that ends with nothing to show fails the remediation

## Context

[`04`](../04-state-machine.md) tabulates `any → FAILED` on "ACU cap hit". Nothing applied it. The
daily budget guard in [`06`](../06-event-pipeline.md) is a different limit — it compares
`acu_ledger` against `DAILY_ACU_BUDGET` before a session is created, and its verdict is
`QUEUED → BLOCKED`. The per-session ceiling is `max_acu_limit = ACU_CAPS[issue_class]`, sent to
Devin at creation and enforced by Devin, and no component observed it being reached.

The general case is wider than the cap. A Devin session can finish having produced no pull request
at all — it exits, reports `outcome: fixed` on a change it never pushed, runs out of budget, or
simply stops. GitHub sends nothing when that happens, because nothing happened on GitHub. The
remediation is left in `RUNNING`:

- `RUNNING` is in the poller's polled set, so it costs one Devin request every
  `POLL_INTERVAL_SECONDS` for the life of the deployment;
- the funnel in [`07`](../07-observability.md) counts it as in flight for ever, so `success rate`
  and `merge rate` are computed against a denominator that never resolves;
- nothing escalates it, so no human is told.

The poller ADR names this class explicitly as the reason the component exists: a session that
errors, stalls, "or hits its ACU cap produces no GitHub event at all — exactly the failures worth
surfacing".

## Decision

`FAILED` is applied, with `blocked_reason` naming which of the three it was, when the poller
observes:

| Condition | `blocked_reason` |
|---|---|
| `acus_consumed >= ACU_CAPS[issue_class]`, and no pull request | `acu_cap_exhausted` |
| status `error` | `session_error` |
| `SessionStatus.is_terminal`, and no pull request | `session_ended_without_pull_request` |

"No pull request" means neither `pull_requests[]` on this observation nor `remediation.pr_url`. The
two are checked together so that the link a remediation already holds is not undone by an
observation that has stopped reporting it — `pull_requests[]` is read from an endpoint whose shape
is unverified until credentials exist ([B8](../blockers.md)).

The cap is judged from `acus_consumed` rather than from any status, because Devin enforces
`max_acu_limit` itself and the status a session stops at when it does so is undocumented. It is
ordered first, so a session that stopped *because* of the cap reports that rather than whatever
status Devin chose to render it as.

A `blocked` report still outranks all three: `BLOCKED` is applied before `FAILED`, and the terminal
state it produces absorbs the second trigger.

## Alternatives considered

| Option | Why not |
|---|---|
| Fail on the ACU cap regardless of the pull request | The pull request is the deliverable. A remediation holding one is carried the rest of the way by check suites and a reviewer, neither of which needs the session — failing it would discard finished work and drop a real merge out of the funnel |
| Treat `suspended` as finished too | `SessionStatus.is_terminal` is `{exit, error}`, T11's reading of the API. A suspended session is one the fix loop may still resume, and inventing a second definition here would put two answers to "is Devin done" in the codebase. If that reading is wrong, `is_terminal` is where to fix it |
| Leave it in `RUNNING` and let an operator notice | This is the "looks busy for ever" failure the stall record rejects in its own alternatives table. It also costs a request per tick, indefinitely, for a remediation that cannot change |
| A timeout — fail a remediation that has been `RUNNING` for N hours | Answers a different question, badly: a slow session is not a finished one, and the threshold would have to be guessed per issue class. Devin telling us it is finished is a fact; elapsed time is an inference |
| Escalate to `BLOCKED` rather than `FAILED` | `BLOCKED` is "Devin reported it cannot proceed" — Devin reported nothing here. `FAILED` is where [`04`](../04-state-machine.md) puts the cap, and the reason column carries the detail either way |

## Consequences

Every remediation now leaves `RUNNING` eventually, by one path or another, so the funnel resolves
and the polled set stays bounded by work actually in flight. `acu_cap_exhausted` and
`session_ended_without_pull_request` appear as their own rows in the failure-breakdown panel, which
is where the answer to "are the per-class caps too low?" becomes visible — the number that would
otherwise only be discoverable by reading `acus_consumed` on stuck rows by hand.

The cost is that `FAILED` is terminal, and a session Devin *reports* as finished may not be. A
remediation failed on a `pull_requests[]` that was merely late is not resumed, and the pull request,
if one appears afterwards, is never linked. That is the same exposure the whole component carries —
the poller is the only thing that links a pull request — and it is bounded here by requiring Devin's
own terminal status rather than an inference of ours.

**What would tell us this was wrong:** `exit` turning out to precede `pull_requests[]` being
populated, so that a session routinely reports itself finished a poll or two before its pull request
appears. That would show up as `session_ended_without_pull_request` remediations whose issues have a
Devin pull request open against them, and the answer would be to require the condition on two
consecutive observations.
