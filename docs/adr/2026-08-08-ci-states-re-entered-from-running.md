---
title: Re-enter the CI states from RUNNING so the review-fix loop closes
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline, github]
tasks: [T14, T20]
files: [src/sentinel/pipeline/state.py]
specs: [docs/04-state-machine.md]
supersedes:
---

# Re-enter the CI states from RUNNING so the review-fix loop closes

## Context

As written, the transition table in [04](../04-state-machine.md) listed `PR_OPENED` as the only
source of `CI_RUNNING`, and `CI_RUNNING` as the only source of `CI_PASSED` and `CI_FAILED`. The
loop edges `CI_FAILED → RUNNING` and `CHANGES_REQUESTED → RUNNING` return to `RUNNING`, and
`PR_OPENED` is entered once — when the pull request appears — because the fix commits of later laps
are pushed to a pull request that already exists.

Encoded literally, the second lap could not close: the `check_suite` events for the fix commit
arrive while the state is `RUNNING`, which no CI row admitted, so they would raise. The same
document's loop sequence diagram shows the opposite, with `check_suite.completed — success`
observed directly in `RUNNING` and the state becoming `CI_PASSED`. The table and the diagram
disagreed.

Three metrics in [07](../07-observability.md) rest on `PR_OPENED` being reached on every path that
reaches CI: the funnel, the merge rate `merged / pr_opened`, and time-to-PR from `pr_opened_at`.
Under the original table that was structurally guaranteed. Any widening of the CI rows puts it at
risk.

## Decision

`RUNNING` is a legal source for all three `check_suite` triggers, alongside `PR_OPENED` for
`requested` and `CI_RUNNING` for the two `completed` conclusions — **bounded by the pull request
link, not by the lap count**:

- the CI triggers require a linked pull request. `PR_OPENED` is the only trigger that links one, so
  it stays on every path into the CI states and `RUNNING → CI_PASSED` cannot bypass it;
- `RUNNING → PR_OPENED` is legal only while none is linked. A later observation is absorbed —
  recorded as an event, state and `pr_opened_at` untouched — rather than raising or moving.

`docs/04-state-machine.md` is corrected in the same branch: the state diagram gains all three
widened edges — `RUNNING --> CI_RUNNING`, `RUNNING --> CI_PASSED` and `RUNNING --> CI_FAILED` — the
three CI rows gain their second source and their condition, the `PR_OPENED` row gains "only while no
pull request is linked", and a fifth invariant states that a pull request is linked exactly once and
that `PR_OPENED` is on every path to CI.

The diagram is the artifact most readers see first, and a picture that disagrees with the table is
what produced this bug in the first place, so it is now compared against the table by the test suite
rather than by eye.

## Alternatives considered

| Option | Why not |
|---|---|
| Leave the table alone and let the poller re-fire `RUNNING → PR_OPENED` each lap | The strongest of these: the trigger column already says `pull_requests[]` on the session, the poller re-reads it every poll, and from `PR_OPENED` the original CI rows work unchanged — so the old table was not quite the dead end it looks. Rejected because the row's documented side effect is "set `pr_opened_at`", which a lap-two firing re-stamps, and because it walks the state backwards out of the loop it is meant to model. Time-to-PR would silently measure the last lap rather than the first |
| Widen the CI rows with no condition attached | What the first version of this decision did. It makes `QUEUED → SESSION_CREATED → RUNNING → CI_PASSED → IN_REVIEW → MERGED` legal with no pull request ever linked, so the funnel can report `ci_green > pr_opened`, `merged / pr_opened` can divide by zero, and time-to-PR loses the sample. Structurally impossible under the old table, so widening had to carry a condition |
| Gate the widened rows on `cycle > 0` instead of on the link | Equivalent for the loop, but it is the wrong fact. What makes a check suite meaningful is that a pull request exists, and what must not be written twice is `pr_opened_at`. `cycle` happens to correlate |
| Return to `PR_OPENED` rather than `RUNNING` when a session is resumed | Contradicts the two loop edges the spec names as "the substance of the system", and drops the meaning of `RUNNING` — Devin is working — for the part of the lifecycle where it is most true |
| Leave the table alone and special-case the second lap in the callers | The loop stops being a first-class path in the machine and becomes an exception re-implemented in the webhook handler and the poller |
| Admit `completed` only from `CI_RUNNING`, trusting `requested` to always arrive first | A missed delivery would then strand the remediation in `RUNNING` until the poller intervened. The spec designs for missed webhooks explicitly, and its own sequence diagram shows `completed` observed from `RUNNING` |

## Consequences

The loop closes with no caller-side exception: `CI_FAILED → RUNNING → CI_RUNNING → CI_FAILED` is
ordinary table lookup, and a `completed` whose `requested` was lost still lands. The funnel
invariants survive the widening — `pr_opened >= ci_green >= merged` holds by construction, not by
convention — and `pr_opened_at` is written once however often the poller re-observes the pull
request.

The cost is a third argument. `transition` needs `pr_linked` alongside `state` and `cycle`, so
every caller reads one more column, and a caller that passes it wrongly gets a wrong answer rather
than an error. `PR_OPENED` also becomes a one-shot state marking the pull request's appearance
rather than a stage each lap passes through, so time-in-state analytics must not treat it as a
per-lap stage — `cycle` is what counts laps.

**What would tell us this was wrong:** a legitimate reason to link a *second* pull request to one
remediation — Devin abandoning its first and opening another would do it — which the write-once
rule would silently absorb, leaving the remediation pointing at the dead pull request. Or a check
suite that must be attributed to a remediation before its pull request is known, which would make
the `LINKED` condition reject an event the pipeline should have kept.
