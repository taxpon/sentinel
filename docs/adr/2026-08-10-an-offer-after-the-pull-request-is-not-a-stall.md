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
([`07`](../07-observability.md)): the autonomy rate reads 0% while every remediation in fact
completed without human help; the failure breakdown shows eight escalations that never happened; and
`BLOCKED` is terminal, so a remediation parks there instead of going on through CI and review.

Answering each question by hand is not a workaround. `human_message_count` is the denominator of the
autonomy rate, so eight human interventions corrupt the same figure the defect does.

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

The offer is **recorded once** as a `remediation_event` of kind `devin_call`, with
`detail.note = session_question_after_pull_request` and `from_state = to_state`, because nothing
moved. Once, and not per tick: nobody answers the question, so the condition persists for the rest
of the remediation's life and a tick lands every `POLL_INTERVAL_SECONDS`.

**The log is the memory.** The poller asks whether that row is already there rather than holding a
flag. The earlier record turned down a two-observation rule because "consecutive" is state no column
holds, and a counter in the poller's memory "would be lost on every restart and absent whenever
`poll_once` is called directly". "Has this been recorded" does not have that problem: the
append-only log answers it durably, from any process, and it is the artefact the note is for.

`outcome: blocked` in the structured report is untouched and stays unconditional. It is Devin saying
outright that it cannot go on, and a pull request does not answer that.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep escalating, and answer each question by hand | Eight human interventions, and `human_message_count` is the autonomy rate's denominator — the workaround corrupts the same figure the defect does, and does it in a way that looks legitimate |
| Read the question text and decide whether it is an offer | Nothing in the session body carries the question. It would mean a natural-language judgement about Devin's own words, made by us, to decide whether to end a remediation — strictly worse than a structural signal that is already on the observation |
| Escalate only after the session has been `waiting_for_user` for N minutes | A timeout is a new mechanism and this repository has added none uninvited. It also answers a different question — see [Consequences](#consequences) |
| Never escalate on `waiting_for_user` at all | Removes the case the earlier record was written for. A session stuck before it has anything to show produces no GitHub event, so nothing else would ever surface it |
| Record nothing | The question is a fact about the run — the only place Devin asked the operator for something — and `status_detail` is not a column, so the next observation overwrites the only trace |
| Record it on every tick | A few hundred identical rows in the log the timeline panel renders, for a condition that changed once. This is the rule [`the poller records only what moves`](./2026-08-08-the-poller-records-only-what-moves.md) already states |
| A new `remediation_event.kind` for it | [`03`](../03-data-model.md) fixes five kinds and `dashboard/src/api.ts` types the union; `devin_call` is accurate — the fact came back from `GET /v3/…/sessions/{id}` and moved nothing — so a sixth would buy a vocabulary change and a UI change for no distinction |
| Condition on the remediation's linked pull request alone | Devin opens the pull request and offers the extra well inside one poll interval, so the observation that links the pull request is routinely the one that also carries the question. Reading only the link would escalate on that tick and never get to make the link |

## Consequences

A remediation whose session ends with an offer now goes on through CI and review, which is what the
run measures. The autonomy rate counts the remediations that finished without a human, the failure
breakdown holds only escalations that happened, and the offer itself is visible on the timeline as
one `devin_call` row where the escalation used to be.

The cost is that a session which genuinely stalls **after** opening a pull request is no longer
escalated by this rule. It is a smaller loss than it sounds: the pull request exists, so the check
suites and the reviewer carry the remediation, and the fix loop resumes the session with a message —
which is also, incidentally, the only thing Sentinel ever says to a session and therefore the only
thing that could clear the detail. What is lost is the case where the session is holding back work
it has not pushed. Nothing surfaces that but the operator diagnosis already in
[`09`](../09-operations.md).

**A long-parked session is deliberately not handled.** A session sitting `waiting_for_user` for an
hour after its pull request is different from one asking a question and being ignored for a minute,
and a duration rule would separate them. It is not built here: it is a timeout, timeouts are a
mechanism this repository has kept out, and the note's `created_at` gives any later rule the clock
it would need without committing to one now. What would make it worth building is a remediation that
goes quiet after the offer — no check suite, no review — because that is the shape a real
post-pull-request stall has and the shape this change cannot tell from a healthy wait.

Nothing else in the pipeline reads `waiting_for_user`. The ACU cap and the end-of-session failure
are keyed off the pull request and not off the detail, so both are unchanged, and the `escalate`
handler sees one fewer reason rather than a different one.

**What would tell us this was wrong:** a session that asks a genuine blocking question after opening
a pull request — "which of these two migrations should I keep?" — and then sits, un-escalated, while
CI stays green and a reviewer waits on a change that will never come. Equally, a `waiting_for_user`
that arrives *with* the first `pull_requests[]` entry but before the pull request is real, which
would mean the deliverable is not the signal it is taken for here.
