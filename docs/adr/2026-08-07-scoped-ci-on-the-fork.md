---
title: Add a narrow CI workflow to the fork instead of running Superset full suite
status: accepted
date: 2026-08-07
type: process
areas: [remediation]
tasks: [T42]
files: []
specs: [docs/08-testing.md]
supersedes:
---

# Add a narrow CI workflow to the fork instead of running Superset full suite

## Context

`taxpon/superset` carries 49 inherited workflows. Several — end-to-end, Playwright, Helm — take tens
of minutes. CI is the feedback signal inside the review-fix loop, so its latency directly sets how
long a self-correction cycle takes and whether the loop can be demonstrated at all. A freshly forked
repository also has no registered workflows until one runs.

## Decision

Add one lightweight `devin-autofix-ci.yml` to the fork: `pre-commit` on changed files, `pytest`
scoped to the test paths the diff touches, and `npm test` scoped to changed frontend packages. Where
a remediation touches an area covered by a heavier workflow, run that workflow on the pull request
before merging.

## Alternatives considered

| Option | Why not |
|---|---|
| Run the inherited full suite on every PR | Cycle times in the tens of minutes make the loop impossible to demonstrate, and much of the suite is irrelevant to any given diff |
| Disable CI and rely on the agent running tests locally | Removes the independent signal. Evidence has to be verifiable on the pull request, not asserted in a session transcript |

## Consequences

The loop runs in minutes, so self-correction is observable. The signal is genuinely narrower than a
full-suite run, and this is stated plainly wherever results are reported rather than presented as
full validation.

**What would tell us this was wrong:** a regression reaching merge that the full suite would have
caught — which is the specific risk being accepted here.
