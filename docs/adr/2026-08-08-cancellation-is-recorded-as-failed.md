---
title: Cancelling a remediation is recorded as FAILED with a cancellation reason
status: accepted
date: 2026-08-08
type: architecture
areas: [github, pipeline]
tasks: [T20, T22, T23, T24]
files: [src/sentinel/github/events.py]
specs: [docs/06-event-pipeline.md, docs/04-state-machine.md, docs/07-observability.md]
supersedes:
---

# Cancelling a remediation is recorded as `FAILED` with a cancellation reason

## Context

The subscribed-events table in [06](../06-event-pipeline.md#subscribed-events) gives
`issues.unlabeled` and `issues.closed` the intent "cancel if not yet terminal". No trigger, no
state and no transition for cancellation appears anywhere in
[04](../04-state-machine.md#transitions); T14's review recorded the omission as a pre-existing
gap in the specs rather than closing it, because `src/sentinel/pipeline/state.py` encodes the
transition table and nothing else.

What was true when the mapping had to choose:

- `State` has twelve members and no `CANCELLED`. Adding one means a new trigger in T14's file, a
  new value in `remediation.state`, and a new bucket in every funnel and rate in
  [07](../07-observability.md).
- `Trigger.FAILED` is legal from every non-terminal state, and `transition()` absorbs any trigger
  applied to a terminal one — which is exactly the shape of "cancel if not yet terminal".
- The spec already routes a non-error ending through `FAILED`: `pull_request.closed` with
  `merged: false` is "**`FAILED`** — abandoned" in the same table.
- Sentinel closes the issue itself when a pull request merges, so a successful remediation produces
  an `issues.closed` delivery of its own.
- Doing nothing is not free. A remediation whose label was removed has a Devin session still
  running, still consuming ACUs against `DAILY_ACU_BUDGET`, and still on course to open a pull
  request nobody asked for.

## Decision

`issues.unlabeled` carrying `AUTOFIX_LABEL`, and `issues.closed`, map to `Intent.CANCEL` with
`Trigger.FAILED` and a `Reason` naming the cause — `autofix_label_removed` or `issue_closed`. The
ingress path applies the trigger like any other, so the remediation ends in `FAILED`.

**The reason is written to `remediation.blocked_reason`**, and to `remediation_event.detail` with the
rest of the transition. The column is where it has to go for the reason to be visible: the failure
breakdown in [07](../07-observability.md) is `count grouped by blocked_reason for state ∈ {BLOCKED,
FAILED}`, so a cancellation with the reason only on the event would land in that panel as an
unlabelled null bucket, and "filter it out" would mean a join the metric does not do.
[03](../03-data-model.md) already describes the column as populated on `BLOCKED` / `FAILED`, so this
is using it as specified rather than widening it.

Escalation is **suppressed** for these two reasons. `FAILED` otherwise comments on the issue and
applies `needs-human` ([04](../04-state-machine.md#escalation)); doing that to an issue a maintainer
has just deliberately closed, or unlabelled, is telling a person to look at the thing they were
looking at when they called it off. The transition is still recorded and still visible on the
dashboard.

Sentinel's own post-merge `issues.closed` needs no special case: the remediation is `MERGED`, which
is terminal, so `transition()` absorbs it. That is what "if not yet terminal" buys.

Cancellation is deliberately **not** given a state of its own here. Inventing one would mean editing
another task's owned file and another task's migration on the strength of one row of a table.

## Alternatives considered

| Option | Why not |
|---|---|
| Classify both actions as `ignored` | The cheapest change and the most expensive outcome: the session keeps running and keeps spending. It also makes removing the label a control that visibly does nothing, which is worse than not offering it |
| Add a `CANCELLED` state and trigger | The right end state, and out of reach: `src/sentinel/pipeline/state.py` is T14's, the state vocabulary is in T03's migration, and every analytics denominator in [07](../07-observability.md) would need a decision about which side of the funnel it falls on. Worth doing deliberately, not as a side effect of the mapping task |
| Use `Trigger.BLOCKED` | `BLOCKED` means Devin cannot proceed or a policy limit stopped us, and it escalates by design. A person removing a label is the opposite of something needing a human |
| Ignore `issues.closed`, cancel only on `unlabeled` | Closing the issue is the commoner way to call work off, and it is the one a maintainer reaches for without knowing Sentinel exists |

## Consequences

Cancellation works today, in the layer that had the gap, without touching a file this task does not
own. Because the reason lands in `blocked_reason`, the failure breakdown shows
`autofix_label_removed` and `issue_closed` as named rows next to `daily_acu_budget_exhausted` and
the rest — so the panel reports cancellations rather than hiding them, and reads as a list of
reasons work stopped rather than a list of things that broke.

The cost is paid in the aggregate figures either way. `FAILED` is the failure bucket, so until a
`CANCELLED` state exists, the **success rate** — which is a count of states, not of reasons —
counts a cancellation as a failure, and no amount of labelling corrects that.

Two obligations land outside this module and are enforced by nothing in it: writing the reason to
`blocked_reason` (T22), and suppressing the escalation comment and `needs-human` label for these two
reasons (T23, T24). Both tasks are named in this record's front matter so the generated index and
`.claude/rules/adr-pointers.md` route it to them.

**What would tell us this was wrong:** cancellations becoming common enough to distort the failure
rate — say more than one in ten terminal remediations — or a reader of the dashboard asking why the
failure count disagrees with the number of things that actually went wrong. The fix at that point is
a `CANCELLED` state, agreed with whoever owns the state machine and the migration, and this record
superseded.
