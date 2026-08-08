---
title: An abandoned pull request still escalates, unlike a cancelled remediation
status: accepted
date: 2026-08-09
type: architecture
areas: [pipeline, github]
tasks: [T20, T23]
files: [src/sentinel/pipeline/handlers.py]
specs: [docs/04-state-machine.md, docs/06-event-pipeline.md]
supersedes:
---

# An abandoned pull request still escalates, unlike a cancelled remediation

## Context

Three reasons reach `FAILED` through a person deciding something rather than through anything going
wrong, and [the cancellation record](./2026-08-08-cancellation-is-recorded-as-failed.md) settled two
of them: `autofix_label_removed` and `issue_closed` are recorded but **not** escalated, because
commenting "a human should look at this" on an issue somebody has just closed on purpose is telling
them to look at the thing they were looking at when they stopped it.

That record left the third undecided and named this task as the one to decide it.
`pull_request.closed` with `merged: false` is the row [06](../06-event-pipeline.md#subscribed-events)
labels "**`FAILED`** — abandoned", and the mapping emits it as `pull_request_closed_unmerged`. It is
usually the same kind of act: a maintainer looking at Devin's pull request and closing it.

What separates it from the two that are suppressed:

- **The escalation comment lands on the originating issue, not on the pull request.** A maintainer
  who closed the pull request has not necessarily been near the issue.
- **The issue is still open, and still carries `devin:autofix`.** Both suppressed reasons end with
  the request itself withdrawn — the label gone, or the issue closed. Here the request stands and
  only the attempt at it has ended.
- **The spec already classifies it.** The two suppressed reasons had no state, no trigger and no row
  in [04](../04-state-machine.md) at all, which is why the mapping had to invent a transition for
  them and why suppressing the escalation was a judgement left open. This one the spec files as a
  failure, and [04](../04-state-machine.md#escalation) says `FAILED` escalates.

Re-labelling does not restart anything: `remediation` is unique on `(repo, issue_number)` and
`ISSUE_LABELLED` is legal only from `None`, so an issue whose remediation has ended keeps its label
and gets no second attempt however many times the label is toggled.

## Decision

`pull_request_closed_unmerged` is **not** added to `CANCELLED_BY_A_HUMAN`. It escalates like any
other failure: the `needs-human` label, and a comment on the issue naming the reason.

The rule the set now expresses is not "a human did this on purpose" — all three are that. It is
**whether the request itself was withdrawn**. Where it was, Sentinel says nothing. Where the issue
is still open and still asking, Sentinel says that the automated attempt is over.

## Alternatives considered

| Option | Why not |
|---|---|
| Suppress it too, as the same class of deliberate act | It is the only one of the three that leaves an open, labelled issue with nothing to say the attempt has ended. The label stays on and buys nothing, the dashboard row is the only trace, and the next person to read the issue finds a stale `devin:autofix` and no explanation. Suppression is meant to avoid telling someone what they already know — the maintainer who closed the pull request may never see the issue |
| Suppress the comment but still apply `needs-human` | Half a signal: the label is a filter, and a filter that turns up an issue with no explanation on it is worse than either alternative. Whatever a reader is told to look at has to say why |
| Comment on the pull request instead of the issue | The pull request is closed; a comment on it is read by nobody, and it is not where the remediation's history lives. The issue is what `remediation` is keyed on |
| Decide it in the mapping, by giving the reason a different intent | The mapping is a pure function with no view of what escalation costs, and [the cancellation record](./2026-08-08-cancellation-is-recorded-as-failed.md) deliberately put the suppression in the handler that performs the escalation. Splitting the rule across both is how the two disagree later |

## Consequences

The suppression set has a stated rule rather than a list of cases, so a fourth human-caused reason
has something to be tested against instead of a precedent to be argued from. An issue whose pull
request was rejected is visible in exactly the way an issue whose session errored is: labelled,
commented, and on the failure-breakdown panel under its own reason.

The cost is the noise this record's neighbour exists to avoid, in the one case where it is
arguable: a maintainer who closes a pull request *and* considers the matter finished gets a comment
on the issue anyway. Closing the issue is what tells Sentinel that — and doing so is absorbed
silently, because the remediation is already terminal.

**What would tell us this was wrong:** maintainers routinely closing both the pull request and the
issue in the same minute, which would mean the comment always arrives on something already settled
and the two acts are one; or `needs-human` accumulating on issues nobody intends to pick up, which
would mean the label is being read as "Sentinel gave up" rather than as a request.
