---
title: Report success, not neutral, when the scoped CI finds nothing to run
status: accepted
date: 2026-08-08
type: architecture
areas: [remediation, pipeline]
tasks: [T42]
files: []
specs: [docs/08-testing.md, docs/04-state-machine.md, docs/06-event-pipeline.md]
supersedes:
---

# Report success, not neutral, when the scoped CI finds nothing to run

## Context

`devin-autofix-ci.yml` derives its test scope from the diff. Some diffs resolve to no test target
at all — documentation, `helm/`, a frontend lockfile bump, a Python module with no mirrored test
file. The workflow still has to conclude, and its conclusion is what moves a remediation.

Sentinel maps `check_suite.completed` conclusions to transitions ([06](../06-event-pipeline.md)):
`success` → `CI_PASSED`, `failure`/`timed_out` → resume the Devin session. `neutral`, `skipped` and
`cancelled` are not mapped to anything. There is no timeout state in the machine
([04](../04-state-machine.md)): a remediation that receives an unmapped conclusion stays in
`CI_RUNNING` with nothing to move it, and nothing to escalate it either.

GitHub concludes a workflow whose jobs were all skipped as `skipped`, not `success`.

Every merge is approved by a human ([2026-08-07-humans-approve-every-merge](./2026-08-07-humans-approve-every-merge.md)).

## Decision

The workflow ends in an aggregate job that runs with `if: always()`, so the check suite always
reaches a definite conclusion rather than being decided by which jobs happened to be skipped.

When the diff resolved to no test target, that job reports **success** and emits a `::warning::`
annotation plus a job-summary note stating that the conclusion reflects `pre-commit` only and is
not evidence that the change works. Paths that wanted a test target and got none are named
individually in the warning.

## Alternatives considered

| Option | Why not |
|---|---|
| `neutral` | Honest about the signal, but unmapped by the state machine. Turns "a green PR carrying a caveat" — recoverable, a human sees it — into "a remediation stalled in `CI_RUNNING`" — no state, no escalation, no dashboard entry. Trades a visible weak signal for an invisible dead one |
| Let the jobs skip and the suite conclude `skipped` | Same failure as `neutral`, arrived at by accident rather than by choice |
| `failure` | Would resume the Devin session and burn a fix cycle and ACUs against a diff that contains nothing wrong. `MAX_FIX_CYCLES` would eventually force `FAILED` on a healthy change |
| Map `neutral`/`skipped` in Sentinel instead | Fixes this workflow but not the general case: any check suite from any source can conclude `neutral`, and the mapping would have to guess what it meant. Better for the workflow that knows why it ran nothing to say so explicitly |

## Consequences

The loop always advances, and no remediation can be lost to a conclusion the state machine has no
edge for. The cost is that `CI_PASSED` no longer implies "tests ran" — it implies "nothing this
workflow could check objected". That distinction is carried in the annotation and the job summary,
where the human approving the merge will see it, rather than in the state name.

**What would tell us this was wrong:** a remediation merged on a vacuous green that a reviewer read
as test evidence — that is, the warning being present but not acted on. If that happens, the answer
is to make the caveat harder to skip (a PR comment from Sentinel, or refusing to request review
while the scope was empty), not to switch the conclusion back to `neutral`.
