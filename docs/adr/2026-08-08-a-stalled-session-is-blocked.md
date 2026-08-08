---
title: A working session waiting for a user is blocked, on the first observation
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T24]
files: [src/sentinel/pipeline/poller.py]
specs: [docs/04-state-machine.md, docs/05-devin-integration.md]
supersedes:
---

# A working session waiting for a user is blocked, on the first observation

## Context

[`05`](../05-devin-integration.md) reads `status_detail` on every poll because it "distinguishes
`working` from `waiting_for_user`, which is how a stalled session is detected". The poller ADR names
the same condition as one of the failures worth surfacing: a session that stalls in
`waiting_for_user` "produces no GitHub event at all".

Nothing in Sentinel answers a Devin session. The only inbound messages are the resume messages of
the review-fix loop, which are sent when CI fails or a reviewer requests changes — neither of which
can happen while the session is still working on its first change. A question asked mid-session is
therefore asked of nobody.

`waiting_for_user` is not by itself a stall. A session that has finished a lap and is idling until
it is resumed is also waiting for input, and that is the normal condition of every remediation
sitting in CI or in review. What separates the two is the status: `claimed`, `running` or `resuming`
means Devin considers itself at work.

[`04`](../04-state-machine.md) defines `BLOCKED` as "Devin reported it cannot proceed, or a policy
limit stopped us. Escalated to a human", tabulates it as reachable from any state, and gives it
`blocked_reason`, a comment on the issue and the `needs-human` label.

## Decision

A session whose status `is_working` and whose `status_detail` is `waiting_for_user` moves the
remediation to `BLOCKED` with `blocked_reason = session_waiting_for_user`, on the first observation,
and enqueues the `escalate` job that comments on the issue and applies `needs-human`.

A session that is `suspended` or `exit` with the same detail is left alone.

## Alternatives considered

| Option | Why not |
|---|---|
| Log a warning and keep polling | The remediation looks busy for ever, which is exactly the failure [`09`](../09-operations.md) tells an operator to go looking for by hand. A pipeline that hides its own stalls cannot be evaluated |
| Escalate only after N consecutive observations | Buys nothing: the answer can only come from a human, and a human is not watching. It would delay the escalation by N × `POLL_INTERVAL_SECONDS` and add a counter with no other purpose |
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

**What would tell us this was wrong:** `waiting_for_user` turning out to be common and routinely
self-clearing, or Devin gaining a way to ask a question that Sentinel could answer from the issue.
Either would make this an escalation of something that was not a failure.
