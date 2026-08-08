---
title: Select remediation targets only where the narrowed CI can prove the fix
status: accepted
date: 2026-08-08
type: process
areas: [remediation]
tasks: [T50]
files: []
specs: [docs/08-testing.md, docs/remediation-candidates.md]
supersedes:
---

# Select remediation targets only where the narrowed CI can prove the fix

## Context

[08](../08-testing.md) requires every remediation to carry a regression test that fails without the
fix, and requires CI to be green **on the pull request** rather than locally. The fork's CI is
deliberately narrow ([B2](../blockers.md), [scoped CI on the fork](./2026-08-07-scoped-ci-on-the-fork.md)):
`pre-commit` on changed files, `pytest` scoped to the test paths the diff touches, and `npm test`
scoped to changed frontend packages. Integration and end-to-end tests do not run.

Superset carries 366 test files under `tests/unit_tests/` and a much larger body under
`tests/integration_tests/`. Several of the defects found during triage — including some more severe
than the ones selected — are only reachable through database-backed integration tests. A fix for one
of those can be correct and still produce no evidence the acceptance criteria will accept.

Triage also produced more viable candidates than the eight required, spread unevenly across the
eight issue classes: `security`, `bug` and `deprecation` each had several, while `security-dep` and
`frontend-dep` had roughly one defensible candidate apiece after false positives were removed.

## Decision

A named test host — a file under `tests/unit_tests/**`, or a jest suite inside a frontend package
the diff touches — is a **hard filter** on candidate selection, not a preference. A defect without
one is not filed, however severe. Every candidate in
[`docs/remediation-candidates.md`](../remediation-candidates.md) names its host.

Given that filter, the eight candidates are allocated **one per issue class** rather than
concentrating on the classes with the deepest supply of good defects.

## Alternatives considered

| Option | Why not |
|---|---|
| Select on severity and widen CI for the ones that need it | Reintroduces the tens-of-minutes cycle time that [scoped CI on the fork](./2026-08-07-scoped-ci-on-the-fork.md) was written to avoid, and does so precisely on the candidates most likely to need several fix cycles |
| Select on severity and accept locally-run evidence | Breaks acceptance criterion 2. Evidence asserted in a session transcript is not independently verifiable, which is the whole point of requiring CI on the PR |
| Have Devin write the missing unit-test scaffolding first | Turns each remediation into two tasks and makes "did the fix work?" depend on test infrastructure written by the same agent in the same session |
| Pick the eight strongest candidates regardless of class | Would have produced roughly four `security`/`bug` items and left `frontend-dep`, `typing` and `perf` unrepresented, undercutting the claim in [01](../01-overview.md) that the system is not just a dependency bumper |

## Consequences

Every candidate can produce the evidence the acceptance criteria demand, so no remediation fails for
a structural reason. Coverage of all eight classes is guaranteed rather than merely likely, and a
single failed candidate costs one class rather than leaving a class empty.

The costs are real and are stated in the candidate document rather than hidden. The set is biased
toward defects that happen to sit in unit-testable modules, which is not the same as the most
important defects — genuinely severe integration-only findings were passed over. One-per-class also
means the two weakest candidates (C4 DOMPurify, C2 Flask) are carried for coverage rather than
merit, and both sit close to the "dependency bumper" failure mode. The compensation is that the
diagnosis criterion was over-satisfied: four candidates require real root-cause analysis where two
were required.

**What would tell us this was wrong.** If several remediations pass the narrowed CI and are then
found broken by a heavier workflow before merge, the filter is selecting for testability against the
wrong signal, and the CI narrowing — not the selection rule — needs revisiting. Equally, if the
weakest one-per-class candidates merge with no fix cycles and no reviewer comments, they are
demonstrating nothing, and the next set should concentrate on the classes with real defect supply
and accept fewer classes.
