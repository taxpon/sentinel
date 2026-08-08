---
title: Enforce the tests-with-source rule as a CI job that diffs the pull request
status: accepted
date: 2026-08-08
type: process
areas: [ops]
tasks: [T05]
files: [.github/workflows/ci.yml]
specs: [docs/08-testing.md]
supersedes:
---

# Enforce the tests-with-source rule as a CI job that diffs the pull request

## Context

Rule 1 in `CLAUDE.md` is that a change under `src/sentinel/**` arrives with a change under
`tests/**`, and that CI enforces it. Most of the work on this repository is done by parallel agent
sessions, so the rule has to hold against an author who cannot be talked to, and it has to be
checkable from the repository alone — no bot installation, no branch-protection settings that live
outside the tree.

Coverage thresholds were the obvious candidate and do not answer the same question: a change to a
module that already sits at 100% line coverage moves no number, and coverage says nothing about
whether the new behaviour was asserted.

## Decision

A `tests-required` job in `.github/workflows/ci.yml` diffs the pull request against its base
(`git diff --name-only <base.sha> HEAD`, where `HEAD` is the merge commit GitHub builds for the
event) and fails when a path matching `^src/sentinel/` is present without any path matching
`^tests/`.

The job runs on `pull_request` only. Pushes to `main` arrive through a pull request that has already
passed the gate, and a push event has no comparable notion of "the change".

## Alternatives considered

| Option | Why not |
|---|---|
| A coverage threshold in `pytest --cov` | Measures the wrong thing. Editing already-covered code passes untouched, and coverage cannot see whether an assertion is meaningful |
| A `pre-commit` hook or a local `make` target | Runs on the author's machine, where it can be skipped with `--no-verify`. The rule exists precisely for authors who are not being watched |
| A third-party PR-policy bot (Danger, `paths-filter` + required checks) | Adds a dependency and configuration that lives outside the repository, for about ten lines of shell |
| Reviewing for it by hand | It is the rule most likely to be waved through at the end of a long review, which is why it was written down in the first place |

## Consequences

The check is cheap — a checkout and one `git diff`, under half a minute — and its verdict is
reproducible locally with the same command. A pull request touching only `docs/`, `dashboard/` or
`scripts/` passes without doing anything.

It is deliberately shallow: it compares path prefixes, not content. A one-line edit to a test file
satisfies it, and nothing here judges whether the test asserts behaviour — that stays a reviewer's
job, and rule 1 says so.

**What would tell us this was wrong:** empty or token test edits appearing in pull requests solely
to clear the job. That is the check being gamed rather than followed, and the answer would be a
stronger signal — assertions counted, or coverage of the changed lines specifically — not a louder
version of this one.
