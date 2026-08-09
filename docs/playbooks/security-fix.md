# Playbook — `security-fix`

> **Status:** Design · **Answers:** What standing context does every `security` and `bug`
> remediation start with?

| | |
|---|---|
| Playbook name | `security-fix` |
| Classes served | `security`, `bug` |
| `max_acu_limit` | 20 |
| Created | By hand in the Devin UI ([B6](../blockers.md#b6)); id supplied via `DEVIN_PLAYBOOK_IDS` |

Paste the block below verbatim as the playbook body. See [`README.md`](./README.md) for the section
structure and for what belongs here rather than in the prompt.

```text
## Overview

A defect in Superset's own Python code — an unsafe input that should have been
refused, or logic that produces a wrong answer. The issue names the symptom. The
mechanism behind it is what you are looking for.

## Procedure

1. Identify the code that was supposed to refuse the input or compute the value.
   - The issue reports where the wrong answer surfaced, which is rarely where it
     was decided. Work outwards from the enforcement point, not from the call
     site that reported the symptom.

2. Determine whether the guard is absent or merely narrow, before editing
   anything.
   - Most of this class is a check that was written correctly and then scoped to
     one dialect, one engine or one feature flag, while its callers assume a
     complete answer.
   - Whoever wrote the narrow check usually identified the hazard correctly, so
     the list of cases it names is evidence about what the general case should
     be.

3. Read every other site that handles the same value before choosing which one
   is wrong.
   - Logic defects of this kind sit between files rather than inside one. A
     value honoured in the filter path and again when the result is displayed,
     but not in the expression the database actually evaluates, is a defect
     precisely because the two places that compensate for it make the third look
     deliberate.

4. Place the regression test on the unit-test path that mirrors the module you
   changed.
   - The scoped run derives its pytest target from the changed module, so a test
     placed anywhere else is never collected and the pull request goes green
     without having executed it once.

5. Cover both directions of the guard.
   - The input that was wrongly accepted is now refused, and the input that was
     always legitimate still is. The way these fixes break production is by
     refusing something legitimate, and a suite that only proves the new refusal
     cannot see that happening.

6. Extend the parametrisation of the surrounding test instead of adding a
   hardcoded case to it.
   - Where the test is parametrised over engines, dialects or flags, the narrow
     scoping is usually the defect itself, so a test that is narrow in the same
     way reproduces the mistake.

## Specifications

1. The enforcement point itself refuses the input, on every dialect, engine or
   flag it is reached through — not only the one the issue happened to name.
2. The regression test sits at the mirrored unit-test path of the module you
   changed, and the pytest scope printed in the CI job summary names that path.
3. Both directions are covered: the case that was wrongly accepted fails without
   the fix, and a case that was always legitimate still passes with it.
4. root_cause names the assumption and the point at which it stopped holding,
   and says what else that same assumption reaches.
5. risk is set against what a wrongly tightened guard would refuse in
   production, not against the size of the diff.
6. The pull request claims no more than the scoped run proved. No integration
   test, live database engine, request cycle or migration was exercised.

## Advice & Pointers

Where this kind of defect lives: guards that were written correctly and then
scoped too narrowly. superset/sql/parse.py and superset/security/manager.py are
enforcement points of exactly that shape, and are worth reading around rather
than sampling at the line the issue points to.

What CI will tell you: a change to superset/<pkg>/<mod>.py gets pre-commit on the
changed files and pytest on the mirrored unit-test path only —
tests/unit_tests/<pkg>/<mod>_test.py, test_<mod>.py or <mod>_tests.py, whichever
of the three exists. That is the whole of the Python signal. Integration tests
never run, so nothing that needs a live database engine, a full request cycle or
a migration is verified here at all.

root_cause: "The fallback check for statements the parser cannot decompose was
written for one dialect and never generalised, so every other dialect skipped it
entirely" is a root cause. "Added CALL to the mutating-command list" is a
description of the diff. A reviewer reads this field to decide whether the fix is
complete.

A guard that now refuses more than it did is medium risk even with a green
suite, and saying so is what tells the reviewer where to look.

Budget: 20 ACUs, the largest of the four playbooks. It is there to be spent on
reading the surrounding code, not on re-running a scoped suite that takes
minutes.

## Forbidden actions

- Correcting the symptom at the call site that reported it while leaving the
  enforcement point as narrow as it was.
- Placing the regression test anywhere other than the mirrored unit-test path of
  the module you changed.
- Adding one hardcoded case to a test that is parametrised over engines, dialects
  or flags.
- Presenting the scoped green run as evidence that the vulnerable path is closed
  end to end. Nothing here exercises a live engine, a request cycle or a
  migration.
- Rating a tightened guard low risk because the diff is small.
```
