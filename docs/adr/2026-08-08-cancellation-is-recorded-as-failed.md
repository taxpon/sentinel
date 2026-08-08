---
title: Cancelling a remediation is recorded as FAILED with a cancellation reason
status: accepted
date: 2026-08-08
type: architecture
areas: [github, pipeline]
tasks: [T20]
files: [src/sentinel/github/events.py]
specs: [docs/06-event-pipeline.md, docs/04-state-machine.md]
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
ingress path applies the trigger like any other, so the remediation ends in `FAILED`, and records
the reason in `remediation_event.detail`.

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
own. The reason is on the event, so the dashboard and the audit trail can say *why* a remediation
ended, and the escalation path in T23/T24 can read it to decide whether to comment on the issue at
all — commenting "this failed, a human should look" on an issue somebody has just deliberately
closed would be noise.

The cost is paid in the analytics. `FAILED` is the failure bucket, so until a `CANCELLED` state
exists, the failure breakdown and the success rate in [07](../07-observability.md) count a
cancellation as a failure. Every such row carries a cancellation `Reason`, so the figures can be
corrected by filtering on it — but nothing forces a consumer to, and the headline number is wrong by
however many remediations were called off. That obligation on the escalation path is likewise
recorded here and not enforced by anything in this module.

**What would tell us this was wrong:** cancellations becoming common enough to distort the failure
rate — say more than one in ten terminal remediations — or a reader of the dashboard asking why the
failure count disagrees with the number of things that actually went wrong. The fix at that point is
a `CANCELLED` state, agreed with whoever owns the state machine and the migration, and this record
superseded.
