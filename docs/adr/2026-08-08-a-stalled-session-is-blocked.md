---
title: A session that is running and waiting for a user is blocked, on the first observation
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T24]
files: [src/sentinel/pipeline/poller.py]
specs: [docs/04-state-machine.md, docs/05-devin-integration.md]
supersedes:
---

# A session that is running and waiting for a user is blocked, on the first observation

## Context

[`05`](../05-devin-integration.md) reads `status_detail` on every poll because it "distinguishes
`working` from `waiting_for_user`, which is how a stalled session is detected". The poller ADR names
the same condition as one of the failures worth surfacing: a session that stalls in
`waiting_for_user` "produces no GitHub event at all".

Nothing in Sentinel answers a Devin session on its own initiative. The only inbound messages are the
resume messages of the review-fix loop, sent when CI fails or a reviewer requests changes. A
question asked while Devin is working is therefore asked of nobody.

`waiting_for_user` is not by itself a stall, and neither is "Devin considers itself at work".
`SessionStatus.is_working` covers `claimed`, `running` and `resuming`, and each of the three means
something different here:

- `running` is the one [`05`](../05-devin-integration.md) names — a session at work, waiting.
- `claimed` has been picked up and has not begun, so it cannot have asked anything yet.
- `resuming` is, by definition, a session Sentinel has *just sent a message to*. It is the one
  moment at which a `waiting_for_user` left over from before that message would be read as a fresh
  question — and the timing of `status_detail` relative to a posted message is undocumented and
  unverifiable until credentials exist ([B8](../blockers.md)).

A session that has finished a lap and is idling until it is resumed is also waiting for input, and
that is the normal condition of every remediation sitting in CI or in review.

The cost of the two mistakes is not symmetric. A missed stall costs polls, on a remediation an
operator is already told to go and look at. A false stall is unrecoverable: `BLOCKED` is terminal,
which drops the remediation out of the polled set — and the poller is the only thing that can link a
pull request ([`04`](../04-state-machine.md)), so a session that goes on to open one would produce a
pull request Sentinel never sees. `pr_opened_at` would never be stamped, the merge would never enter
the funnel, and a fix that shipped would be counted as a blocked failure in `merged / pr_opened` and
in the autonomy rate.

[`04`](../04-state-machine.md) defines `BLOCKED` as "Devin reported it cannot proceed, or a policy
limit stopped us. Escalated to a human", tabulates it as reachable from any state, and gives it
`blocked_reason`, a comment on the issue and the `needs-human` label.

## Decision

A session whose status is **`running`** and whose `status_detail` is `waiting_for_user` moves the
remediation to `BLOCKED` with `blocked_reason = session_waiting_for_user`, on the first observation,
and enqueues the `escalate` job that comments on the issue and applies `needs-human`.

The poller keeps its own set for this — `STALLED_STATUSES` — rather than reusing
`SessionStatus.is_working`, which is T11's answer to a different question and is right for the one
it answers. `claimed`, `resuming`, `suspended` and `exit` with the same detail are all left alone.

## Alternatives considered

| Option | Why not |
|---|---|
| Log a warning and keep polling | The remediation looks busy for ever, which is exactly the failure [`09`](../09-operations.md) tells an operator to go looking for by hand. A pipeline that hides its own stalls cannot be evaluated |
| Escalate only after N consecutive observations | Would answer the false positive too, and more generally — but "consecutive" is state no column holds, and `remediation` takes no new column outside T03. Held in the poller's memory it would be lost on every restart and absent whenever `poll_once` is called directly, which is how the end-to-end test drives it. Narrowing the status set costs nothing and removes the case actually identified |
| Keep `is_working`, and accept the `resuming` window | The window is exactly one poll wide and lands on lap two of the fix loop, which is the most expensive remediation in the system to lose — it has a pull request open, and losing it also loses the pull request |
| Answer the question automatically | Steering Devin line by line is what the prompt design explicitly avoids, and an answer invented by Sentinel would be worse than no answer |
| A distinct `STALLED` state | A fourth terminal state that escalates identically to `BLOCKED`, differing only in the word. `blocked_reason` already carries the distinction, and the failure-breakdown panel groups on it |
| Treat any `waiting_for_user` as a stall, whatever the status | Blocks every remediation the moment its session finishes a lap — which is every successful remediation, immediately after its pull request opens |

## Consequences

A stalled session is escalated within one poll interval, appears in the failure breakdown under its
own reason, and stays visible rather than being retried. The reader of the dashboard learns
something true about the system's limits: this class of issue produced a question Devin could not
resolve alone.

The cost is that `BLOCKED` is terminal, so a human who answers the question in the Devin dashboard
gets a session that finishes its work while Sentinel no longer tracks it. That is the same bargain
every escalation in [`04`](../04-state-machine.md) makes — escalated work is not retried
automatically — and the pull request, if one opens, is still a pull request on the target repository.

A stall that Devin reports under `claimed` or `resuming` rather than `running` is missed, and the
remediation sits until an operator reads `devin_status` off the row — the diagnosis
[`09`](../09-operations.md) already documents. The residual risk is a `status_detail` that stays
stale into `running` after a resume, which this does not cover and which no amount of status
narrowing can.

**What would tell us this was wrong:** `waiting_for_user` turning out to be common and routinely
self-clearing, or Devin gaining a way to ask a question that Sentinel could answer from the issue.
Either would make this an escalation of something that was not a failure. Equally: `status_detail`
turning out to lag a posted message, which would make the narrowed status set insufficient and put
the two-observation rule back on the table.
