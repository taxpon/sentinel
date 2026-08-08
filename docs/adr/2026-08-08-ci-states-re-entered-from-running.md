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

## Decision

`RUNNING` is a legal source for all three `check_suite` triggers, alongside `PR_OPENED` for
`requested` and `CI_RUNNING` for the two `completed` conclusions. `docs/04-state-machine.md` is
corrected in the same branch: the state diagram gains `RUNNING --> CI_RUNNING`, the three CI rows
gain their second source, and the loop section says that `PR_OPENED` is entered once.

## Alternatives considered

| Option | Why not |
|---|---|
| Return to `PR_OPENED` rather than `RUNNING` when a session is resumed | Contradicts the two loop edges the spec names as "the substance of the system", and drops the meaning of `RUNNING` — Devin is working — for the part of the lifecycle where it is most true |
| Leave the table alone and special-case the second lap in the callers | The loop stops being a first-class path in the machine and becomes an exception re-implemented in the webhook handler and the poller |
| Admit `completed` only from `CI_RUNNING`, trusting `requested` to always arrive first | A missed delivery would then strand the remediation in `RUNNING` until the poller intervened. The spec designs for missed webhooks explicitly, and its own sequence diagram shows `completed` observed from `RUNNING` |

## Consequences

The loop closes with no caller-side exception: `CI_FAILED → RUNNING → CI_RUNNING → CI_FAILED` is
ordinary table lookup, and a `completed` whose `requested` was lost still lands correctly.
`PR_OPENED` becomes a one-shot state marking the pull request's appearance rather than a stage each
lap passes through, so time-in-state analytics must not treat it as a per-lap stage — `cycle` is
what counts laps.

**What would tell us this was wrong:** check suites observed while a remediation is in `PR_OPENED`
after the first lap, which would mean the pull request is being reopened per lap rather than pushed
to; or an analytics question that needs "first CI run" distinguished from "fix CI run" by state
rather than by `cycle`.
