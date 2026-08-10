---
title: CI green is the aggregate of a head SHA's check runs, not one suite's conclusion
status: accepted
date: 2026-08-10
type: architecture
areas: [pipeline, remediation]
tasks: [T20, T21, T42]
files:
  - src/sentinel/github/checks.py
  - src/sentinel/github/events.py
  - src/sentinel/github/client.py
  - src/sentinel/pipeline/handlers.py
  - src/sentinel/api/webhooks.py
specs: [docs/04-state-machine.md, docs/06-event-pipeline.md, docs/08-testing.md, docs/blockers.md]
supersedes:
---

# CI green is the aggregate of a head SHA's check runs, not one suite's conclusion

## Context

`docs/04-state-machine.md` treated a `check_suite.completed` conclusion as *the* CI signal:
`success` → `CI_PASSED`, `failure`/`timed_out` → resume the session. That was written on the
assumption that the fork's own scoped workflow would be the one talking.

It is not. GitHub raises one check suite per app and per workflow, and `taxpon/superset` carries 46
active workflows. `docs/fork-ci/README.md` step 2 — disable all but ours — was written to make the
assumption true, and was never carried out.

The first live remediation ([PR #9](https://github.com/taxpon/superset/pull/9)) produced both
possible errors from that one assumption, in a single run over 27 check suites:

| Time after the PR opened | Check suite | Conclusion | What Sentinel did |
|---|---|---|---|
| 0m13s | `Hold Label Check` | `success` | `PR_OPENED → CI_PASSED → IN_REVIEW` |
| 1m31s | `Dependency Review` | `failure` | `IN_REVIEW → CI_FAILED`, resumed the session, `cycle = 1` |
| 3m25s | **`Devin autofix CI`** | `success` | absorbed — the remediation was already in review |

Review was requested on the strength of a workflow that checks for a `hold` label. The fix cycle was
spent on *"Dependency review is not supported on this repository. Please ensure that Dependency
graph is enabled"* — a repository setting, which fails on every pull request in this fork and which
no diff can address — and the resume message was built from that workflow's log, because
`get_failing_job` selected the newest failing run across the whole repository.

Four facts constrain any fix:

- **The payload cannot be narrowed.** `check_suite` carries no workflow name, and `app.slug` is
  `github-actions` for all 24 Actions suites alike. Identifying our own suite requires a call to
  `check_suite.check_runs_url`.
- **The ingress path may make no outbound call**
  ([ADR](./2026-08-07-respond-202-before-external-calls.md)). GitHub abandons a delivery after about
  ten seconds.
- **The loop needs failure to be news.** A rule that never reports failure is as broken as one that
  never reports success — `CI_FAILED → RUNNING` is half the system.
- **`cancelled`, `neutral` and `skipped` cannot be dropped.** Once the question is "has the SHA
  changed state", the completion that finally settles it may well be a skipped suite.

## Decision

A suite conclusion is a reason to **ask**, not an answer.

Every `check_suite.completed` delivery, whatever it concluded, resolves its remediation and enqueues
a fifth job kind, `evaluate_ci`. The worker reads the pull request's current head SHA, then
`GET /repos/{repo}/commits/{sha}/check-runs`, and applies one of three verdicts:

| Verdict | When | Trigger |
|---|---|---|
| **failed** | the check run named `CI_REQUIRED_CHECK_NAME` failed, whatever else is running | `check_suite_failed` |
| **green** | it succeeded, nothing else is failing, nothing is incomplete | `check_suite_succeeded` |
| **pending** | anything else | `check_suite_requested` |

The gate is `devin-autofix-ci`, the `if: always()` conclusion job of the fork's own workflow, which
already fails when any scoped signal did. Asking for that one name is the whole of "our CI passed",
so no list of job names has to be kept in step with the workflow.

Two consequences are deliberate and are the least obvious part of this record:

- **A failing check run outside our own workflow yields *pending*, not *failed*.** It has not judged
  the diff, and resuming a session over it is what spent the cycle above.
- **The head SHA is read from the pull request, not from the job payload.** A payload SHA can be
  stale by the time the job is claimed, and a superseded run's checks are `cancelled` — complete and
  not failing, which is the exact shape an "all clear" rule reads as green.

`get_failing_job` is narrowed to the same workflow by path, so a resume message quotes the check that
judged the diff or says there was none.

## Alternatives considered

| Option | Why not |
|---|---|
| Disable the inherited workflows and keep reading conclusions | Necessary anyway, and done — but it makes correctness a repository setting no test can hold. One re-enabled workflow silently restores the false green. |
| Identify our own suite from the delivery and ignore the rest | Impossible from the payload, and a call to `check_runs_url` costs the same as aggregating while buying less: it would also blind Sentinel to a real failure in an inherited workflow. |
| **Any** failing check run → `CI_FAILED` (the rule first proposed, and approved) | Honest about the pull request, wrong about whose problem it is. While the fork's workflows are active this fires on every remediation, resumes Devin against an unfixable `Dependency Review`, and exhausts `MAX_FIX_CYCLES` — the defect this record exists to remove, relocated rather than fixed. Reverting to it is a one-line change in `sentinel.github.checks.verdict`. |
| Foreign failure → a fourth state of its own | A state with no transition out of it and no side effect is a `CI_RUNNING` with a longer name. The log line carries the diagnosis instead. |
| Required status checks via branch protection | `master` is unprotected, and this moves the definition into a repository setting read through a lazily-computed `mergeable_state` that no test can exercise. |
| Aggregate inline in the webhook handler | Forbidden by [ADR](./2026-08-07-respond-202-before-external-calls.md), and two GitHub calls inside a ten-second budget is how a delivery gets abandoned and redelivered for ever. |

## Consequences

Both defects are closed at the source, and the closure is testable: `sentinel.github.checks.verdict`
is a pure function, and its fixtures put several suites on one SHA — the shape every CI fixture in
the suite previously lacked.

The costs, in order of how much they matter:

- **`docs/fork-ci/README.md` step 2 is now load-bearing.** Under the old rule a live `Dependency
  Review` produced a false green; under this one it produces a *pending* that never resolves, so no
  remediation would reach `IN_REVIEW` at all — honest, and still broken. That step has since been
  carried out (1 active workflow on the fork, 45 disabled — [B2](../blockers.md#b2)), which is what
  makes this definition usable rather than merely correct. Re-enabling one inherited workflow puts
  the stall back, and nothing in this repository can detect that from the inside.
- **Two GitHub calls per completed suite**, against 18 completions on PR #9. Bounded by the
  remediation being non-terminal, and negligible against the rate limit, but it is real traffic that
  did not exist before.
- **An absorbed evaluation writes no `remediation_event`.** Consistent with
  [ADR](./2026-08-08-the-poller-records-only-what-moves.md), and it removes 12 rows of noise from
  PR #9's log — but a check suite arriving after a merge is now visible only in `webhook_delivery`.
- **`ci_green_at` moves later**, to the first *aggregate* green. Time-to-CI-green and review latency
  ([07](../07-observability.md)) are both affected: the numbers recorded before this change are
  wrong in the flattering direction and must not be compared with ones recorded after it.

**What would tell us this was wrong.** A remediation sitting in `CI_RUNNING` with the gate green and
`worker.ci.foreign_failure` in the log, on a fork where step 2 *has* been done — that would mean a
check we do not control is a legitimate part of the verdict and the third row of the alternatives
table was the right one after all. Equally: a remediation reaching `IN_REVIEW` while a reviewer can
see a red check on the pull request would mean the gate is too narrow, and the verdict should
consider more than one name.
