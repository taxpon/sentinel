# Playbook — `security-fix`

> **Status:** Design · **Answers:** What standing context does every `security` and `bug`
> remediation start with?

| | |
|---|---|
| Playbook name | `security-fix` |
| Classes served | `security`, `bug` |
| `max_acu_limit` | 20 |
| Created | By hand in the Devin UI ([B6](../blockers.md#b6)); id supplied via `DEVIN_PLAYBOOK_IDS` |

Paste the block below verbatim as the playbook body. See [`README.md`](./README.md) for what belongs
here rather than in the prompt.

```text
Scope: a defect in Superset's own Python code — an unsafe input that should have been
refused, or logic that produces a wrong answer. The issue names the symptom. The
mechanism behind it is what you are looking for.

Where this kind of defect lives
  Guards that were written correctly and then scoped too narrowly. A check gated on one
  dialect, one engine, one feature flag, while its callers assume a complete answer.
  superset/sql/parse.py and superset/security/manager.py are enforcement points of
  exactly this shape: whoever wrote the narrow check usually identified the hazard
  correctly, so the list of things it names is evidence about what the general case
  should be.
  Logic defects tend to sit between files rather than inside one. A value is honoured in
  the filter path and again when the result is displayed, but not in the expression the
  database actually evaluates — and the two places that compensate for it are what make
  the third place a defect rather than a design choice. When one file looks wrong, read
  everything else that touches the same value before deciding which one to change.

What CI will tell you
  A change to superset/<pkg>/<mod>.py gets pre-commit on the changed files and pytest on
  the mirrored unit-test path only — tests/unit_tests/<pkg>/<mod>_test.py,
  test_<mod>.py or <mod>_tests.py, whichever of the three exists. That is the whole of
  the Python signal. Integration tests never run, so nothing that needs a live database
  engine, a full request cycle or a migration is verified here at all.
  Put the regression test in the mirror of the module you changed. A test placed
  anywhere else is never collected by the scoped run, and the pull request goes green
  without having executed it once.

What a regression test for this class looks like
  The positive case — the input that was wrongly accepted is now refused, or the result
  that was wrong is now right — and the negative case next to it. Tightening a guard
  fails in the other direction: the way these fixes break production is by refusing
  something that was always legitimate, and a test suite that only proves the new
  refusal cannot see that happening.
  Where the surrounding test is parametrised over engines, dialects or flags, extend the
  parametrisation rather than adding one hardcoded case. The narrow scoping is usually
  the defect itself, so a test that is narrow in the same way reproduces the mistake.

root_cause
  Name the assumption and the point where it stopped holding. "The fallback check for
  statements the parser cannot decompose was written for one dialect and never
  generalised, so every other dialect skipped it entirely" is a root cause. "Added CALL
  to the mutating-command list" is a description of the diff. A reviewer reads this
  field to decide whether the fix is complete, so it should also make clear what else
  the same assumption reaches.

Reporting
  Set risk against the blast radius of being wrong, not against the size of the diff. A
  guard that now refuses more than it did is medium risk even with a green suite, and
  saying so is what tells the reviewer where to look.

Budget: 20 ACUs, the largest of the four playbooks. It is there to be spent on reading
the surrounding code, not on re-running a scoped suite that takes minutes.
```
