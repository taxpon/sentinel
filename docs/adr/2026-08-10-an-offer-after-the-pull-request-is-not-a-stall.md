---
title: A question asked after the pull request is an offer, not a stall
status: accepted
date: 2026-08-10
type: architecture
areas: [pipeline]
tasks: [T24]
files: [src/sentinel/pipeline/poller.py]
specs: [docs/04-state-machine.md, docs/05-devin-integration.md]
supersedes:
---

<!-- `make adr-index` validates these fields and refuses to write the index if any record fails. -->

# A question asked after the pull request is an offer, not a stall

## Context

[`2026-08-08-a-stalled-session-is-blocked`](./2026-08-08-a-stalled-session-is-blocked.md) reads
`status_detail: waiting_for_user` on a session Devin reports as `running` as a stall, and escalates
it to `BLOCKED` on the first observation. That record named what would tell us it was wrong:
"`waiting_for_user` turning out to be common and routinely self-clearing", or Devin "gaining a way
to ask a question that Sentinel could answer". The first half has happened, in a form the record did
not anticipate — the question is common, and it is not about the work.

Issue #5, re-run on 2026-08-10 under the corrected CI logic:

```
12:10:06  None            -> QUEUED           issue_labelled
12:10:08  QUEUED          -> SESSION_CREATED  session_created
12:10:24  SESSION_CREATED -> RUNNING          session_running
12:15:53  RUNNING         -> PR_OPENED        pr_opened
12:16:34  PR_OPENED       -> BLOCKED          blocked           <- 41 seconds later
```

`blocked_reason: session_waiting_for_user`, `cycle: 0`, no CI failure and no review. Devin had
finished: it opened <https://github.com/taxpon/superset/pull/11>, reported that the issue's premise
was partly wrong — the skipped test had two never-passing causes rather than the one the issue
claimed, and it fixed both — and then asked whether the user would like it to run the app end to end
as an optional extra. That question set `status_detail: waiting_for_user`.

`waiting_for_user` therefore covers two situations with opposite treatments:

| | |
|---|---|
| Stuck, cannot proceed without input | Escalate — the state exists for this |
| Finished the work, offering something further | Not an escalation |

Devin has done the second one for one, on every session observed so far. If that holds, **all eight
remediations end `BLOCKED`**, and the consequences are in the figures the submission rests on
([`07`](../07-observability.md)):

- **There is no autonomy rate at all.** `BLOCKED` is terminal, so nothing reaches `MERGED`; the rate
  is `merged with cycle = 0 and human_message_count = 0 / merged`, and `_ratio` returns `0.0` for an
  empty denominator while the panel gates on `funnel.merged > 0` and renders an em dash with
  "nothing merged in this window"
  ([ADR](./2026-08-08-blank-a-figure-whose-denominator-is-empty.md)). The headline figure of the
  submission is *absent*, not low — which is worse, and is the reading a viewer gets.
- The failure breakdown shows eight escalations that never happened.
- A remediation parks at `BLOCKED` instead of going on through CI and review, so the funnel stops at
  `pr_opened` and every duration past it is undefined.

Answering each question by hand is not a workaround. `human_message_count == 0` is half the predicate
that counts a merge as autonomous, so eight human interventions would take the numerator to zero and
report 0% autonomy off a full denominator — the defect's figure, arrived at legitimately.

What distinguishes the two readings is available on the observation: the deliverable. A session that
has produced a pull request has delivered what it was asked for, so anything it asks after that is
about an extra. A session that has not is being held up by its own question, and that path is the
one the earlier record was written for.

## Decision

**Once a pull request exists, `waiting_for_user` is not a stall.** `blocked_reason` returns
`session_waiting_for_user` only when no pull request exists — neither linked to the remediation nor
reported by the session in this very observation, the same "either side counts" rule the ACU cap and
the end-of-session failure already use. Before a pull request exists, the escalation is unchanged:
first observation, `BLOCKED`, `needs-human`.

The offer is recorded as a `remediation_event` of kind `devin_call`, with
`detail.note = session_question_after_pull_request` and `from_state = to_state`, because nothing
moved.

**Once per fix cycle, not once per remediation.** `pr_url` is write-once and never cleared, so the
condition that suppresses the escalation stays true for the rest of the remediation's life. Keyed on
the remediation alone, an offer recorded on cycle 0 would swallow a *different* question asked on
cycle 1 — one raised part-way through a fix, where the session may genuinely be stuck — and that
question would then produce no state change, no row, no metric and nothing but a log line. So
`detail.cycle` is written and is part of the key.

**The log is the memory.** The poller asks whether that row is already there rather than holding a
flag. The earlier record turned down a two-observation rule because "consecutive" is state no column
holds, and a counter in the poller's memory "would be lost on every restart and absent whenever
`poll_once` is called directly". "Has this been recorded" does not have that problem: the
append-only log answers it durably, from any process, and it is the artefact the note is for.

**The log line is unconditional and the row is not load-bearing.** The line is emitted on every tick
the condition holds, before the deduplication and whether or not a row follows, so the condition can
never be entirely unobservable. The row itself is written inside a `SAVEPOINT`, with database faults
caught and logged: it runs in the same transaction as the transitions, and an exception escaping it
would roll back `PR_OPENED`, `pr_url`, `pr_number` and `pr_opened_at` along with it — for ever, since
the next tick would see the same session and fail identically, while `poller_lag_seconds` and the
heartbeat stayed green because `_beat` runs regardless. An annotation must not be able to veto a
state transition, and the savepoint is what makes that structural rather than a convention.

**An observation that escalates is not annotated.** When `outcome: blocked` arrives on a session that
is also waiting with a pull request open, the remediation is escalated and no note is written. The
note is the *alternative* to an escalation; a row asserting both would contradict the table this
record puts in [`04`](../04-state-machine.md).

`outcome: blocked` in the structured report is otherwise untouched and stays unconditional. It is
Devin saying outright that it cannot go on, and a pull request does not answer that.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep escalating, and answer each question by hand | Eight human interventions, and `human_message_count == 0` is half of what makes a merge count as autonomous — the workaround zeroes the same figure the defect erases, and does it in a way that looks legitimate |
| Read the question text and decide whether it is an offer | Nothing in the session body carries the question. It would mean a natural-language judgement about Devin's own words, made by us, to decide whether to end a remediation — strictly worse than a structural signal that is already on the observation |
| Escalate only after the session has been `waiting_for_user` for N minutes | A timeout is a new mechanism and this repository has added none uninvited. It also answers a different question — see [Consequences](#consequences) |
| Never escalate on `waiting_for_user` at all | Removes the case the earlier record was written for. A session stuck before it has anything to show produces no GitHub event, so nothing else would ever surface it |
| Record nothing | The question is a fact about the run — the only place Devin asked the operator for something — and `status_detail` is not a column, so the next observation overwrites the only trace |
| Record it on every tick | A few hundred identical rows in the log the timeline panel renders, for a condition that changed once. This is the rule [`the poller records only what moves`](./2026-08-08-the-poller-records-only-what-moves.md) already states |
| A new `remediation_event.kind` for it | [`03`](../03-data-model.md) fixes five kinds and `dashboard/src/api.ts` types the union; `devin_call` is accurate — the fact came back from `GET /v3/…/sessions/{id}` and moved nothing — so a sixth would buy a vocabulary change and a UI change for no distinction |
| Condition on the remediation's linked pull request alone | Devin opens the pull request and offers the extra well inside one poll interval, so the observation that links the pull request is routinely the one that also carries the question. `PR_OPENED` is applied before `BLOCKED`, so the link *would* be made — and then the remediation would escalate straight out of the state it had just entered, which is the `PR_OPENED -> BLOCKED` pair in the Context above, arrived at one tick earlier |

## Consequences

A remediation whose session ends with an offer now goes on through CI and review, which is what the
run measures. The autonomy rate counts the remediations that finished without a human, the failure
breakdown holds only escalations that happened, and the offer itself is visible on the timeline as
one `devin_call` row where the escalation used to be.

**The cost is that a session which genuinely stalls after opening a pull request is no longer
escalated by this rule, and this record adopts an alternative the earlier one rejected.**
[`2026-08-08`](./2026-08-08-a-stalled-session-is-blocked.md) turned down "log a warning and keep
polling" with *"the remediation looks busy for ever … a pipeline that hides its own stalls cannot be
evaluated"*. That is exactly what happens here, for the post-pull-request subset. What is claimed to
justify it is the subset and not the premise: the pull request exists, so check suites and a reviewer
carry the remediation, and the fix loop resumes the session with a message — which is the only thing
Sentinel ever says to a session and therefore the only thing that could clear the detail. The reader
should know the earlier record ruled the other way on the same premise, and weigh the narrowing
rather than take the argument as new.

**What is really lost is a session holding back work it has not pushed**, and the loss is silent in
the figures as well as in the alarm. `_failures` in [`07`](../07-observability.md) counts only
`BLOCKED` and `FAILED`, so a remediation that used to appear in the failure breakdown under its own
reason now contributes a `labelled` count that never advances, and then ages out of the window
entirely. Nothing surfaces it but an operator looking: [`09`](../09-operations.md) gains a
troubleshooting row and a query for remediations that have not moved in N hours, which is the only
detection this change leaves.

**A long-parked session is deliberately not handled.** A session sitting `waiting_for_user` for an
hour after its pull request is different from one asking a question and being ignored for a minute,
and a duration rule would separate them. It is not built here: it is a timeout, timeouts are a
mechanism this repository has kept out, and the note's `created_at` gives any later rule the clock it
would need — for the cycle it was written on, which is why the note is keyed on the cycle rather than
the remediation. What would make it worth building is a remediation that goes quiet after the offer —
no check suite, no review — because that is the shape a real post-pull-request stall has and the
shape this change cannot tell from a healthy wait.

**The gate is the existence of a `pull_requests[]` entry, not the state of it.** `pr_state` is
carried by the API and modelled by nothing, so a closed or draft pull request counts the same as an
open one, and the vocabulary of that field has never been observed — the live call of 2026-08-10
recorded its name and type and not its values ([B8](../blockers.md#b8)). Branching on an enum taken
from a reference rather than a response is the mistake `tasks/lessons.md` records four defects from,
so the finer judgement waits for evidence.

Nothing else in the pipeline reads `waiting_for_user`. The ACU cap and the end-of-session failure
are keyed off the pull request and not off the detail, so both are unchanged, and the `escalate`
handler sees one fewer reason rather than a different one.

**"Recorded once" is a check-then-act, so two pollers running at once could write the row twice.**
Compose and `fly.toml` each run exactly one, and the existing write-once pull-request link has the
identical shape — `PullRequestCondition.UNLINKED` is read and then acted on, in the same window.
Making either structural means a uniqueness constraint, which is a schema change and belongs to T03.
What it would cost if it ever happened is two identical rows in the timeline: noise, not a wrong
state.

**What would tell us this was wrong:** a session that asks a genuine blocking question after opening
a pull request — "which of these two migrations should I keep?" — and then sits, un-escalated, while
CI stays green and a reviewer waits on a change that will never come. Equally, a `waiting_for_user`
that arrives *with* the first `pull_requests[]` entry but before the pull request is real, which
would mean the deliverable is not the signal it is taken for here.
