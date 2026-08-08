---
title: Accept a check suite event wherever a linked pull request can sit
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline, github]
tasks: [T14, T20, T24]
files: [src/sentinel/pipeline/state.py]
specs: [docs/04-state-machine.md]
supersedes:
---

# Accept a check suite event wherever a linked pull request can sit

## Context

As written, the transition table in [04](../04-state-machine.md) listed `PR_OPENED` as the only
source of `CI_RUNNING`, and `CI_RUNNING` as the only source of `CI_PASSED` and `CI_FAILED`. It
assumed a remediation always sits in a CI state when a check suite reports. It does not, for two
independent reasons.

**The fix laps.** The loop edges `CI_FAILED → RUNNING` and `CHANGES_REQUESTED → RUNNING` return to
`RUNNING`, and `PR_OPENED` is entered once — when the pull request appears — because the fix commits
of later laps are pushed to a pull request that already exists. Encoded literally, the second lap
could not close: the `check_suite` events for the fix commit arrive while the state is `RUNNING`,
which no CI row admitted, so they would raise. The same document's loop sequence diagram showed the
opposite, with `check_suite.completed — success` observed directly in `RUNNING`. The table and the
diagram disagreed.

**The first lap.** `pull_request.opened` cannot resolve a remediation — `remediation` is keyed
`(repo, issue_number)`, the payload carries no issue number, and a Devin pull request body is not
required to name the issue it fixes (the recorded payload cites Sentry and Shortcut instead). So
the poller links the pull request, up to `POLL_INTERVAL_SECONDS` after it appeared — routinely after
the first `check_suite.requested` has already been dropped as unresolvable. The remediation is
therefore in `PR_OPENED` when the first conclusion arrives, and `PR_OPENED` had no CI-completed
edge. Widening to `RUNNING` alone did not reach this: the poller's own linking is what moves the
remediation out of `RUNNING` into the one state with nowhere to go. Because the poll interval is
wider than the gap between a pull request opening and its first check suite, this was the dominant
ordering, not the rare one — every remediation, lap one.

Generalising: Sentinel does not choose when a check suite reports, and every state after
`PR_OPENED` can hold a linked pull request. Enumerating the states that *happen* to be CI states is
the wrong shape for the rule.

Three metrics in [07](../07-observability.md) rest on `PR_OPENED` being reached on every path that
reaches CI: the funnel, the merge rate `merged / pr_opened`, and time-to-PR from `pr_opened_at`.
Under the original table that was structurally guaranteed. Any widening of the CI rows puts it at
risk.

## Decision

**All three `check_suite` triggers are legal from every state that can hold a linked pull request** —
`PR_OPENED`, `RUNNING`, `CI_RUNNING`, `CI_FAILED`, `CI_PASSED`, `IN_REVIEW`, `CHANGES_REQUESTED`.
None of them may reject one. Two conditions bound that:

- **The link, not the lap count.** The CI triggers require a linked pull request, and `PR_OPENED` is
  the only trigger that links one, so it stays on every path into the CI states and
  `RUNNING → CI_PASSED` cannot bypass it. `RUNNING → PR_OPENED` is legal only while none is linked;
  a later observation is absorbed — recorded as an event, state and `pr_opened_at` untouched.
- **An event with no new verdict is absorbed, not moved.** A suite *starting* again is never news,
  so `requested` moves only from `PR_OPENED` and `RUNNING`. A second success is ignored, because
  `ci_green_at` is the first green and a success must not drag a remediation out of review. A
  failure is news almost everywhere — including `IN_REVIEW`, where nothing else would re-engage the
  fix loop — but not in `CHANGES_REQUESTED`, which already has a `resume_session` pending that will
  produce a fresh suite, nor twice over in `CI_FAILED`.

`docs/04-state-machine.md` carries this as a "Check suite events" section whose table gives, per
trigger, the states it moves from and the states where it is recorded and ignored. The state diagram
gains every widened edge, the two `completed` rows of the transition table gain their sources, the
`PR_OPENED` row gains "only while no pull request is linked", and a fifth invariant states that a
pull request is linked exactly once and that `PR_OPENED` is on every path to CI.

The diagram is the artifact most readers see first, and a picture that disagrees with the table is
what produced this bug in the first place, so it is now compared against the table by the test
suite rather than by eye. The property itself is asserted over the *reachable* graph rather than
over the sources `RULES` happens to list — an enumeration of the table would have passed while lap
one was broken, because `PR_OPENED` was reachable and unlisted.

## Alternatives considered

| Option | Why not |
|---|---|
| Leave the table alone and let the poller re-fire `RUNNING → PR_OPENED` each lap | The strongest of these: the trigger column already says `pull_requests[]` on the session, the poller re-reads it every poll, and from `PR_OPENED` the original CI rows work unchanged — so the old table was not quite the dead end it looks. Rejected because the row's documented side effect is "set `pr_opened_at`", which a lap-two firing re-stamps, and because it walks the state backwards out of the loop it is meant to model. Time-to-PR would silently measure the last lap rather than the first |
| Widen the CI rows with no condition attached | What the first version of this decision did. It makes `QUEUED → SESSION_CREATED → RUNNING → CI_PASSED → IN_REVIEW → MERGED` legal with no pull request ever linked, so the funnel can report `ci_green > pr_opened`, `merged / pr_opened` can divide by zero, and time-to-PR loses the sample. Structurally impossible under the old table, so widening had to carry a condition |
| Gate the widened rows on `cycle > 0` instead of on the link | Equivalent for the loop, but it is the wrong fact. What makes a check suite meaningful is that a pull request exists, and what must not be written twice is `pr_opened_at`. `cycle` happens to correlate |
| Return to `PR_OPENED` rather than `RUNNING` when a session is resumed | Contradicts the two loop edges the spec names as "the substance of the system", and drops the meaning of `RUNNING` — Devin is working — for the part of the lifecycle where it is most true |
| Leave the table alone and special-case the second lap in the callers | The loop stops being a first-class path in the machine and becomes an exception re-implemented in the webhook handler and the poller |
| Admit `completed` only from `CI_RUNNING`, trusting `requested` to always arrive first | A missed delivery would then strand the remediation in `RUNNING` until the poller intervened. The spec designs for missed webhooks explicitly, and its own sequence diagram shows `completed` observed from `RUNNING`. Since the poller became the linker, a dropped `requested` is not even a fault: on lap one it is guaranteed |
| Add `PR_OPENED` to the `completed` rows and stop there | Fixes the reported sequence and nothing else. `IN_REVIEW` and `CHANGES_REQUESTED` can hold a linked pull request too, and a check suite completing there would raise for the same reason. The rule wanted stating over the states that can hold a link, not over the states that happen to have been reported |
| Move on every check suite event, from every linked state | Symmetric and simpler to state, but wrong in both directions: a success arriving in `IN_REVIEW` would re-stamp `ci_green_at` and drag the remediation out of review, and a `requested` would do the same for no information at all. What varies is whether the event carries a verdict the state does not already have |

## Consequences

Both laps close with no caller-side exception, and a `completed` whose `requested` was lost still
lands — which since the poller became the linker is the **normal** case on lap one, not a fault: the
first `check_suite.requested` arrives while `pr_number` is still NULL, cannot be resolved, and is
dropped by design. The funnel invariants survive the widening — `pr_opened >= ci_green >= merged`
holds by construction, not by convention — and `pr_opened_at` is written once however often the
poller re-observes the pull request.

`CI_RUNNING` becomes an optional state rather than a stage every remediation passes through. Nothing
depends on it today, but analytics that assumed a CI run is visible as time spent in `CI_RUNNING`
would undercount; `remediation_event` carries the `check_suite` events either way.

The cost is a third argument. `transition` needs `pr_linked` alongside `state` and `cycle`, so
every caller reads one more column, and a caller that passes it wrongly gets a wrong answer rather
than an error. `PR_OPENED` also becomes a one-shot state marking the pull request's appearance
rather than a stage each lap passes through, so time-in-state analytics must not treat it as a
per-lap stage — `cycle` is what counts laps. And absorbing is now three distinct rules rather than
one; a fourth would be a sign the table wants a different shape.

**What would tell us this was wrong:** a legitimate reason to link a *second* pull request to one
remediation — Devin abandoning its first and opening another would do it — which the write-once
rule would silently absorb, leaving the remediation pointing at the dead pull request. Or a check
suite that must be attributed to a remediation before its pull request is known, which would make
the `LINKED` condition reject an event the pipeline should have kept.
