---
title: The remediation filing script is a dry run until an operator says otherwise
status: accepted
date: 2026-08-09
type: process
areas: [remediation, ops]
tasks: [T51]
files: [scripts/file_remediation_issues.py, tests/test_file_remediation_issues.py]
specs: [docs/remediation-candidates.md, docs/09-operations.md]
supersedes:
---

# The remediation filing script is a dry run until an operator says otherwise

## Context

T51 is titled "File the eight remediation issues on the fork", and read literally its deliverable is
eight issues on `taxpon/superset`.

Filing them is not a step in a build. `taxpon/superset` is a public fork of a real project, and each
issue carries `devin:autofix` — the label `sentinel.github.events` dispatches on. So filing the
eight does not produce eight issues; it produces eight Devin sessions, spends the ACU budget against
caps that are still provisional (B11), and opens eight pull requests on a repository other people
can see. Two of the candidates (C2, C4) are the ones most likely to consume their whole budget in
the test loop, and one of them is expected to end `blocked`.

None of that is reversible in the way a bad commit is. The project's own working agreements say so:
"do not delete issues, sessions or history to tidy up." An issue filed by mistake stays filed.

Nobody has authorised that spend or that visible activity. The repository owner asked for the
capability; the pipeline is not yet through wave 5, and Devin credentials are not even available
(B8), so an issue filed today would sit labelled with nothing watching it.

## Decision

The task delivers `scripts/file_remediation_issues.py`, not the issues.

A dry run is what the script does when it is given no arguments. It reads the fork — issue titles
and label names — reports exactly what it would file and what it would skip, and makes no request
that could change anything. `--apply` is a word an operator has to type, and it is the only thing
that writes.

`tests/test_file_remediation_issues.py` asserts the property at its widest: after a dry run, *every
request the process made was a `GET`*. Not "no issue was created" and not "no label was created",
because the failure worth catching is a write on an endpoint no test thought to model.

The script is deliberately absent from the `Makefile`. `make bootstrap` and `make seed-issues` exist
because they are safe to run into; this one is not, and a target is an invitation.

## Alternatives considered

| Option | Why not |
|---|---|
| File the eight issues, as the task title reads | It is an outward-facing act on a public repository, costing budget nobody has approved, at a point where nothing is listening for the label. The task title describes the capability; it does not carry the authorisation |
| `--apply` as the default, with `--dry-run` available | The dangerous mode must not be the one you get by mistyping. Every other script here reconciles and is safe to re-run; this one is the exception, so the exception is where the friction goes |
| A confirmation prompt instead of a flag | Unattended and unassertable. A prompt cannot be tested, and the one place this will eventually run — an operator following the runbook — is exactly where a habitual `y` is cheapest |
| Build nothing until the fork is authorised | The parse of `docs/remediation-candidates.md`, the idempotence and the issue bodies are the reviewable substance. Deferring them leaves the decision to a session under time pressure, with the fork in front of it |

## Consequences

T52 ("Execute and monitor the remediation runs") inherits the act this task did not perform. That is
the right seam: T52 is the task that has a run log to write and is scheduled after Devin credentials
exist, and its dependency on T51 is now a dependency on a script rather than on a repository state.

The dry run still reads GitHub, so it needs a token with read access and it needs the fork's issue
tracker to be on. It is not an offline preview, and against a fork that has not been bootstrapped it
reports the trigger label as one it would have to create (B1).

Idempotence is what makes `--apply` survivable at all: existing issue titles are read first, open
and closed, so a second `--apply` files nothing and a run interrupted halfway is completed by the
next one rather than doubled. Without that, "the flag is hard to type" would be the only protection,
and the first partial failure would leave someone choosing between duplicates and manual cleanup.

**What would tell us this was wrong:** the fork being authorised and the filing becoming routine —
re-run after each edit to the triage — at which point the friction is in the wrong place and the
default should be revisited along with a `Makefile` target.
