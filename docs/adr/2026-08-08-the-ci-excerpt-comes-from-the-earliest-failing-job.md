---
title: The CI excerpt comes from the earliest failing job of the latest failing run
status: accepted
date: 2026-08-08
type: architecture
areas: [github, remediation]
tasks: [T12, T23]
files: [src/sentinel/github/client.py]
specs: [docs/04-state-machine.md, docs/fork-ci/README.md]
supersedes:
---

# The CI excerpt comes from the earliest failing job of the latest failing run

## Context

On the `CI_FAILED -> RUNNING` edge the worker resumes the Devin session with the failure as context
(`docs/04-state-machine.md`). The excerpt fetched here is the entire evidence the session gets, so
which job it comes from decides whether the next lap of the loop is aimed at the real fault.

A head SHA on the fork does not fail in one place. `devin-autofix-ci.yml` runs five jobs, and the
last of them — `devin-autofix-ci` — runs `if: always()` and fails whenever a signal job did
(`docs/fork-ci/README.md`). So a failed `pytest` produces two failing jobs, and the aggregate's log
contains only a restatement that something upstream failed.

`check_suite` is what the state machine advances on, but a check run is not a workflow job: the
logs endpoint is `GET /repos/{repo}/actions/jobs/{job_id}/logs`, and the only link from a check run
back to a job id is the text of its `details_url`. A SHA can also carry more than one workflow run,
because a run can be re-run by hand.

## Decision

Resolve the log through the Actions API rather than through check runs: list the workflow runs for
the head SHA, take the failing one with the **highest id**, list its jobs, and take the **earliest**
failing job by `started_at` with the job id as the tie-break. Fetch that job's log and return its
last `LOG_EXCERPT_LINES` lines.

"Highest id" is the newest attempt — GitHub allocates run ids in order — and an older run's failure
has been superseded. `run_started_at` is deliberately not used for this: a run queued later can
start earlier than one ahead of it in the queue, which would let the runner pool decide which
attempt counts as current, and the field can be absent while the id never is.

"Earliest failing job" is the cause rather than a consequence — a job that fails because another
job failed can only start after it, and the aggregate job depends on all three signal jobs, so it
is always last. Here the timestamp is the right signal and the id is not: a run's jobs are all
created at once, so their ids say nothing about the order they ran in. No job name appears in the
client.

A job GitHub has not stamped with a `started_at` therefore sorts **last**, not to the epoch, which
would be first. This is not hypothetical: a job that never ran concludes `startup_failure`, which
this selection deliberately includes, and it is the population most likely to carry a null start
time. At the epoch it would beat every real failure and become the whole of the evidence the
session receives. Ranked last it is still chosen when it is the only failure — the case where it
*is* the evidence.

If the log itself has expired, the job is returned with an empty excerpt rather than raised: the
name and URL still describe the failure, and `ci_failure_message` renders the gap as
`NO_LOG_OUTPUT`. A `410 Gone` is a `4xx`, which neither this client nor the queue retries, so
raising would fail the remediation over a log that was merely too old.

`get_check_runs` remains for reading the CI state of a SHA. It is not the path to the log.

## Alternatives considered

| Option | Why not |
|---|---|
| Parse the job id out of a check run's `details_url` | Depends on the shape of a `html_url`, which is not part of the API contract, to reach an endpoint that is |
| Take the first failing job the API returns | The order is unspecified. Returning the aggregate job's log would send Devin a message whose evidence section says a signal job failed, and nothing else |
| Exclude the aggregate job by name | Puts the fork's workflow layout inside Sentinel's client, so renaming a job in another repository silently degrades the excerpt here |
| Send every failing job's log | Several megabytes of mostly duplicate output, and `LOG_EXCERPT_LINES` exists because the instruction after the excerpt has to survive it |
| Sort a missing `started_at` to the epoch | Simpler, and wrong in the direction that matters: an unstamped job wins the `min` outright, so the one class of failure with no log to speak of displaces the one with the traceback in it |
| Order the runs by `run_started_at` as well, for symmetry with the jobs | The two orderings answer different questions. Among attempts, the one GitHub accepted last is current; among the jobs of one attempt, the one that ran first is the cause. Using a timestamp for both looks consistent and puts the runner pool's queueing in charge of which attempt Sentinel reports on |

## Consequences

The session is resumed with the log of the job that actually broke, chosen by a rule that holds for
any workflow rather than for this one. Adding, renaming or reordering jobs in
`devin-autofix-ci.yml` needs no change here.

The cost is three requests instead of one, and one deliberate blind spot: if two independent jobs
fail at once — a `pytest` failure and a `jest` failure — only the earlier one is forwarded. The
session sees the second on the next lap, which costs a cycle.

**What would tell us this was wrong:** remediations spending a cycle per failing job, which would
show up as fix-cycle counts clustering at the number of signal jobs. The answer then is to forward
every failing job's excerpt under a shared line budget, not to change which one is first.
