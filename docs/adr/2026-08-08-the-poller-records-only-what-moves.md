---
title: The poller records only the observations that move a remediation
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T24]
files: [src/sentinel/pipeline/poller.py]
specs: [docs/03-data-model.md, docs/04-state-machine.md]
supersedes:
---

# The poller records only the observations that move a remediation

## Context

[`04`](../04-state-machine.md) says a trigger that carries no new verdict — a webhook arriving after
a terminal state, a second `check_suite` success, a `PR_OPENED` for a pull request already linked —
is "recorded in `remediation_event` and otherwise ignored". `state.transition()` implements the
second half by returning `absorbed=True` rather than raising.

That rule was written for webhooks, which arrive once per real-world event. The poller is not like
that. It re-reads the same session every `POLL_INTERVAL_SECONDS`, so almost every tick observes
exactly what the last one observed: a remediation sitting in `IN_REVIEW` for a working day would
absorb the same `PR_OPENED` about 1,700 times.

`remediation_event` is append-only and is the source of truth for every duration and rate in
[`07`](../07-observability.md). It is also the audit trail a reviewer reads to see what happened to
one issue.

## Decision

An absorbed trigger writes no `remediation_event` and no job. Only a transition whose `moved` is
true is recorded, and it is recorded exactly once, in the same transaction as the column update.

The observed columns are a separate matter and are reconciled on every tick, moved or not.
`devin_status`, `devin_session_url`, `acus_consumed` and `structured_output` are the observation
itself rather than a record of a change, and the row is what the dashboard reads and what the ACU
cap is judged against. A column whose value is unchanged is not re-assigned, so a tick that saw
nothing new still issues no `UPDATE`.

The absorption itself still happens in `state.transition()` rather than in the poller. `PR_OPENED`
is applied on every tick that sees `pull_requests[]`, and the state machine's
`PullRequestCondition.UNLINKED` is what makes the link write-once — the poller does not check
whether a pull request is already linked and skip the trigger, because that would be a second copy
of a rule that has to stay identical to the first.

Invariant 4 of [`04`](../04-state-machine.md) — "every transition writes exactly one
`remediation_event`" — is unaffected: an absorbed trigger is not a transition. The sentence in the
review-fix loop section of that document which said a repeat *poller* observation "is recorded as an
event and otherwise ignored" is amended in the same change, because it is the one this record
overturns.

## Alternatives considered

| Option | Why not |
|---|---|
| Record every observation, as the webhook path records an absorbed delivery | Three orders of magnitude more rows than transitions. The timeline panel and the audit trail become unreadable, and the table that exists to answer "what happened to this issue" answers "it was polled" |
| Record the first absorbed observation of each kind and suppress the rest | Needs state saying which kinds have been seen, which is the poller re-implementing the state machine's own conditions in a second place |
| Record observations in a separate table | A second log with a second retention story, for data whose only content is "nothing changed" — which the tick log line already says |
| Have the poller check the pull request link itself and skip the trigger | Duplicates `PullRequestCondition.UNLINKED`. Two copies of a write-once rule can disagree, and the state machine's tests pin the version that would then be dead code |

## Consequences

`remediation_event` stays proportional to what actually happened, so the timeline is readable and
the durations derived from it cost nothing to compute. The poller's own repetition is visible in the
`poller.tick` log line and in the Devin request histogram instead.

The cost is that the event log cannot answer "when did we last successfully poll this session".
Nothing needs that today: `devin_status` and `acus_consumed` are reconciled on the row every tick,
and poller lag is published from a database snapshot by whichever process serves `/metrics`
([ADR](./2026-08-08-metrics-are-process-local.md)).

**What would tell us this was wrong:** needing to prove a session *was* polled during some interval
— an incident where the question is whether the poller was running rather than what it saw. That is
a `last_polled_at` column on `remediation`, not an event per tick.
