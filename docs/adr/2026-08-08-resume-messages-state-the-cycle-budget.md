---
title: Tell the session which fix cycle it is on and how many remain
status: accepted
date: 2026-08-08
type: architecture
areas: [devin, pipeline]
tasks: [T15, T23]
files: [src/sentinel/devin/playbooks.py]
specs: [docs/05-devin-integration.md, docs/04-state-machine.md]
supersedes:
---

# Tell the session which fix cycle it is on and how many remain

## Context

The review-fix loop resumes the same session on `CI_FAILED → RUNNING` and
`CHANGES_REQUESTED → RUNNING`, and `cycle > MAX_FIX_CYCLES` forces `FAILED`
([04](../04-state-machine.md)). The session cannot observe either number: Sentinel counts the cycles
and enforces the limit outside Devin. `docs/05-devin-integration.md` templates only the CI-failure
message, and it carries neither the cycle nor the budget; the changes-requested message is described
in the state machine but not templated anywhere.

The structured output offers `outcome: "blocked"` as a first-class alternative to producing a
change, and `blocked` escalates to a human rather than failing quietly. A session that does not know
it is on its last cycle has no reason to prefer that alternative over another attempt.

## Decision

Both resume messages end with the same two sentences: which cycle this is, out of how many, and that
a goal which cannot be reached within the remaining cycles should be reported as `blocked` with a
specific reason. The spec's CI-failure text is reproduced verbatim ahead of it, and the
changes-requested message is written to the same shape — the pull request, the reviewer's words,
then the goal restated.

`docs/05-devin-integration.md` carries all three blocks as of this record, so what is sent can still
be diffed against the document; the tests read the blocks out of it and assert equality.

Passing a cycle outside `1..max_cycles` raises. Reaching the cap is a state-machine transition to
`FAILED`, not a message to send.

## Alternatives considered

| Option | Why not |
|---|---|
| Send the spec's text unchanged | The cycle is the one fact only Sentinel holds, and withholding it makes the cap arrive as an unexplained termination mid-attempt |
| Instruct the session to change approach on the last cycle | Prescribes the how, which [the delegation rule](./2026-08-07-delegate-task-not-steps.md) rejects. Stating the budget is a fact; "try something simpler now" is a step |
| Carry the cycle only in the `cycle:N` tag | Tags are metadata for the dashboard and the audit trail. Nothing puts them in front of the session |

## Consequences

A session near the cap can choose to escalate with a diagnosis instead of spending the last cycle on
a guess, which is the outcome the pipeline can actually act on. The addition is two sentences of
standing text repeated on every resume, and it is fixed wording rather than anything derived from
the failure, so it cannot crowd out the log excerpt.

**What would tell us this was wrong:** sessions reporting `blocked` on a late cycle for problems a
further attempt would have solved — the notice talking them out of work they could have finished.
