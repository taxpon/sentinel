# Playbook — `flaky-test`

> **Status:** Design · **Answers:** What standing context does every `flaky-test` and `typing`
> remediation start with?

| | |
|---|---|
| Playbook name | `flaky-test` |
| Classes served | `flaky-test`, `typing` |
| `max_acu_limit` | 12 |
| Created | By hand in the Devin UI ([B6](../blockers.md#b6)); id supplied via `DEVIN_PLAYBOOK_IDS` |

Paste the block below verbatim as the playbook body. See [`README.md`](./README.md) for what belongs
here rather than in the prompt.

```text
Scope: a check that has been switched off. A test that is skipped or intermittent, or a
module that type checking has been told to stop complaining about. In both cases the
suppression is the visible thing and never the thing to fix; the work is to make the
signal real again and then to leave it on.

Where the cause lives
  For a skipped test, the reason is usually not in the test file. A query that can never
  match — a test id no component renders, a role or label that was renamed out from
  under it — fails identically on every run, and gets skipped because a failure that
  nobody can explain looks like flakiness. Establish which of the two you have before
  changing anything: search the whole repository for the selector the test asks for, and
  if nothing in the tree produces it, the test was written against an intention that was
  never wired up. That is a missing production hook, not a broken test. Genuinely
  intermittent tests are a different problem with different causes — shared state
  between cases, timers, an await that can resolve out of order.
  For a suppressed type error, the suppression comment names what mypy could not work
  out. SQLAlchemy's declarative attributes are the usual reason on this codebase, and
  the fix is to give mypy the information it lacks — an annotation, a narrowing cast —
  never a broader ignore or a wider exclusion.

What CI will tell you
  A changed file under tests/unit_tests/ is its own pytest target, so a Python test you
  edit does get run. A frontend test is scoped by directory instead: touching anything
  under superset-frontend/src/dashboard/** selects the whole src/dashboard jest suite,
  so the test you re-enabled runs and so does everything around it — which is the point,
  because re-enabling a test that has been off for a long time is exactly when
  neighbouring assumptions turn out to have drifted.
  mypy runs through pre-commit, on the changed files only. Dropping a module from a
  warn_unused_ignores override in pyproject.toml therefore proves nothing unless that
  module is itself in the diff. Note also that touching pyproject.toml escalates the
  pytest scope to the entire tests/unit_tests suite, so expect a much longer run than
  the size of the diff suggests.
  Both runners are configured to pass on an empty selection: pytest treats "no tests
  collected" as a warning, and jest runs with --passWithNoTests. A path that selects
  nothing produces a green tick and no evidence whatsoever. Read the CI job summary,
  which prints the exact scope that ran, before believing a pass.

What counts as fixed
  The re-enabled test is the regression test. It must fail on the tree as it was and
  pass after your change, and if it passes on the unmodified tree you have not found the
  cause. Remove the lint suppression that accompanied the skip in the same commit — an
  oxlint or ruff directive left behind is a standing statement that something here is
  still expected to be disabled.
  Deleting the test, weakening it until it asserts something that cannot fail, or
  re-skipping it with a better-worded reason are not fixes for this class. If the test
  should not exist, that is an outcome of "blocked" with the argument written out, not a
  quiet deletion.
  For a typing fix the proof is directional. mypy passing is not evidence, because mypy
  passed before; the evidence is that the exclusion is gone, so a stale or unnecessary
  ignore in that module is an error again. Name the behavioural test that exercises the
  newly typed path as well, because a config change on its own tells a reviewer nothing
  about whether the code still works.
  structured_output.tests.added is not allowed to be empty for this class either. A
  re-enabled test is an added test — list it by file path and test name. For a typing
  change, list the behavioural test that guards the path alongside the config entry that
  was removed.

root_cause
  The mechanism, and why it stayed hidden. "The test queried a data-testid that no
  component ever set, so it could never pass on any run; skipping it concealed a missing
  test hook rather than an intermittent failure" is a root cause. "Un-skipped the test"
  is not.

Budget: 12 ACUs. Most of it should go on establishing why the signal was suppressed. The
change that follows is usually a few lines, and a few lines written before the cause is
understood is how this class produces a test that passes for the wrong reason.
```
