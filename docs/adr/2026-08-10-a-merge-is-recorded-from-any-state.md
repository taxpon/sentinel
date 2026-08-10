---
title: A merge is recorded from any state, and the state is left where it is
status: accepted
date: 2026-08-10
type: architecture
areas: [pipeline, analytics]
tasks: [T21, T24]
files:
  - src/sentinel/api/webhooks.py
  - src/sentinel/analytics/metrics.py
specs: [docs/03-data-model.md, docs/04-state-machine.md]
supersedes:
---

# A merge is recorded from any state, and the state is left where it is

## Context

`Trigger.PR_MERGED` is legal only from `IN_REVIEW`, and terminal states absorb every trigger
(`docs/04-state-machine.md`, invariant 1). `merged_at` was stamped by `_stamp`, which the ingress
calls only when a transition *moved*.

Remediation 1 escalated to `BLOCKED`, and a human resolved the escalation the way humans do: by
reading the pull request and merging it. The merge webhook arrived, `PR_MERGED` was absorbed, and
the last event on the remediation is `BLOCKED -> BLOCKED pr_merged`. `merged_at` stayed null, so the
funnel reported `merged: 0` — beside a link to
[a pull request GitHub shows as merged](https://github.com/taxpon/superset/pull/9).

Every mechanism behaved as specified. The specification let *terminal for work* mean *terminal for
observation*, and those are not the same thing: no further work should happen on a `BLOCKED`
remediation, but things can still happen to its pull request.

This is not an edge case in the run about to start. Escalation is a designed outcome
(`docs/04-state-machine.md#escalation`), a human resolving one by merging is the expected response,
and every remediation that takes that path reproduces this exactly.

The dashboard's live table exists so a reader can click through to the pull request and check any
claim at source. A row that says "not merged" next to a link to a merged pull request does not
merely lose a number — it makes the table untrustworthy for every other number on it.

## Decision

**Stamp `merged_at` whenever a merge is observed, whatever state the remediation is in**, and leave
the state alone.

`merged_at` is written from the trigger rather than from the target state, outside the `moved`
branch — the only column that is. Every other timestamp records a state the remediation entered;
this one records something that happened to the pull request.

**The funnel counts merges from `merged_at`, not from `state`.** It already did — `metrics.summary`
selects on the timestamp — so the fix was to make the column true rather than to change the query.
`docs/03-data-model.md` now says so explicitly, because that was incidental and is now load-bearing.

A remediation can therefore be `BLOCKED` with a `merged_at`, and appear in both the merged count and
the failure breakdown. That is not a contradiction: it is what "Devin escalated, and then a person
merged it" is.

## Alternatives considered

| Option | Why not |
|---|---|
| Widen `PR_MERGED` so a terminal state moves to `MERGED` | Erases the escalation. `blocked_reason` and the failure-breakdown row would go with it, and the autonomy story would read as a clean merge when a human was needed. Two lies replacing one. |
| Add a `MERGED_AFTER_ESCALATION` state | A state with no transition out and no side effect is a column with extra steps, and it multiplies every state-keyed query in `docs/07-observability.md`. The pair of facts is already expressible. |
| Count the funnel's `merged` from `state = MERGED` instead | The direction that makes this worse. The column is the observation; the state is the workflow. Counting workflow as observation is the mistake being fixed. |
| Let the poller notice instead | It cannot. `sentinel.devin.schemas` deliberately does not model `pr_state` on `pull_requests[]`, and Devin's view of a pull request is second-hand anyway. GitHub's `pull_request.closed` delivery is the authoritative observation, and it already arrives. |
| Leave it, and correct the funnel by hand for the write-up | A number that needs a footnote to be true is the thing the dashboard exists to avoid. |

## Consequences

The funnel, the merge rate, MTTR, review latency, throughput and impact all now count a
merged-after-escalation remediation, because all of them read `merged_at`. That is a real change to
the headline figures, in the direction of matching what GitHub shows.

`state` and `merged_at` can now disagree in a way a reader has to interpret: `BLOCKED` plus a merge
time. `docs/04-state-machine.md` invariant 1 states the reading. The alternative was a number that
disagrees with the pull request, which no amount of documentation repairs.

Only the webhook path stamps this. If the merge delivery is missed entirely — the tunnel rotating
mid-run ([B9](../blockers.md#b9)) — nothing recovers it, because the poller cannot see a pull
request's state. That gap is unchanged by this record, but it is worth naming: reconciliation is
self-healing for sessions and not for merges.

**What would tell us this was wrong.** A reader asking why a remediation is `BLOCKED` when the
dashboard counts it as merged, and not being satisfied by the answer — that would mean the pair of
facts needs a state of its own after all, and the second row of the alternatives table was right.
Equally: a `merged_at` on a remediation whose pull request was closed unmerged and later reopened
and merged by someone else entirely would mean the trigger is too blunt and the observation needs to
carry the pull request number it came from.
