# Playbook — `deprecation`

> **Status:** Design · **Answers:** What standing context does every `deprecation` and `perf`
> remediation start with?

| | |
|---|---|
| Playbook name | `deprecation` |
| Classes served | `deprecation`, `perf` |
| `max_acu_limit` | 12 |
| Created | By hand in the Devin UI ([B6](../blockers.md#b6)); id supplied via `DEVIN_PLAYBOOK_IDS` |

Paste the block below verbatim as the playbook body. See [`README.md`](./README.md) for what belongs
here rather than in the prompt.

```text
Scope: a call that still works today but sits on a removal path, or code whose shape
costs more than it needs to. What makes this class different from every other one here
is that the observable behaviour must not move at all. You are changing how, never what,
and the pull request's central claim is that nothing else changed.

Choosing the slice
  These defects arrive as populations, not instances. A legacy accessor has a handful of
  live call sites and belongs to a broader legacy idiom with hundreds; a per-item query
  loop in one data-access object has siblings across the codebase. The judgement being
  asked for is where to stop. A bounded set with one mechanical transformation and a
  clear correctness argument is reviewable; a sweep across two hundred files is not,
  however correct each edit is. Fix the slice the issue names, and put the rest in the
  pull request description rather than in the diff.
  Look for the idiom that already exists in the tree before inventing one. This codebase
  usually contains the batched or modern form a few lines away from the code that does
  not use it — a bulk resolver defined in the same module as the loop that calls the
  single-item lookup per iteration. Using it makes the change reviewable as "this call
  site was never converted" instead of as a new pattern somebody now has to assess.

Making it stick
  A deprecation fix that only edits call sites regresses the first time somebody writes
  the old form again. pytest.ini already promotes a list of SQLAlchemy 2.0 removal
  warnings to hard errors; extending that list with the pattern you have just eliminated
  — in the same pull request — is what turns a cleanup into a guarantee. Prefer that to
  any amount of additional test coverage, because it is the only part of this work that
  cannot rot.

What CI will tell you
  Each changed superset/<pkg>/<mod>.py selects its own mirrored unit-test path, so a
  change across seven modules produces a union of small targets, and any module without
  a mirrored test file contributes nothing. Editing pytest.ini escalates the scope to
  the whole tests/unit_tests suite, which for this class is a feature rather than a
  cost: a warnings-as-errors change is exactly the one that deserves a broad run.
  Watch the mirror naming. The scope is derived from the module path by convention, so a
  test directory that is not named after the package it covers is not found, and the
  only reason a run touches your module at all may be that the test file you edited is
  itself in the diff. Check the CI job summary, which prints the resolved scope, and do
  not read "no tests collected" as a pass.

Regression test
  For a deprecation, the durable test is the converted path executed with the removal
  warning promoted to an error: it fails on the old call and passes on the new one.
  Assert the behaviour that is claimed to be unchanged as well — identity-map semantics,
  ordering, de-duplication — because "identical behaviour" is the promise the pull
  request is making and nothing else in this diff substantiates it.
  For a performance fix, the measurement is the test, and this repository has no
  assert_num_queries helper to borrow. Build the instrumentation: a SQLAlchemy
  before_cursor_execute listener that counts statements while the path under test runs.
  Assert that the count does not grow with the size of the input rather than asserting
  one specific number, so an unrelated query added later does not break a test that is
  about complexity. Put the before and after counts in the pull request; a performance
  change without a number is an assertion, not a result.

root_cause
  For a deprecation, why the old form is still there and what makes the replacement
  exactly equivalent. For a performance defect, the shape that produces the cost — "the
  loop resolves each tag with its own SELECT and then issues a second SELECT to find any
  existing link, so the statement count grows as 2N+1 in the number of tags" — and not
  "batched the lookups", which describes the diff rather than the defect.

Traps
  superset/migrations/versions/** matches these patterns and is frozen history. Leave it
  alone; a converted migration is a defect, not an improvement.
  Do not reformat, rename or restructure the modules you pass through on the way. The
  value of this class comes from a diff boring enough to review at a glance, and every
  incidental change spends that.

Budget: 12 ACUs. A wide, shallow change is cheap to make; keeping it wide and shallow is
the discipline this class is testing.
```
