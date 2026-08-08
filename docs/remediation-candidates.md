# Remediation candidates

> **Status:** Design · **Answers:** Which eight concrete defects in `taxpon/superset` will the pipeline be pointed at, and what proves each one is real?

These are the eight targets T51 files as issues and Devin acts on. Each is written to be actionable
without further human interpretation: the defect, the evidence that it exists, what "fixed" means,
and the regression test that fails without the fix ([08](./08-testing.md), "Remediation acceptance
criteria").

## How these were selected

Every candidate was grounded in the actual tree, not in a changelog or an issue title. The working
checkout was `taxpon/superset` at `f5bca3b` (post-6.0.0 `master`), and the evidence came from
reading the code, resolving advisories against the [OSV](https://osv.dev) API, and querying GitHub
for upstream issues and advisories.

One constraint shaped the selection more than any other. The fork's CI is deliberately narrowed
([B2](./blockers.md), [ADR](./adr/2026-08-08-select-only-unit-testable-targets.md)): `pre-commit` on
changed files, `pytest` scoped to the test paths the diff touches, and `npm test` scoped to changed
frontend packages. Integration and end-to-end tests do not run. A defect whose only proof lives in
`tests/integration_tests/` therefore cannot produce the evidence the acceptance criteria demand, so
**a named test host under `tests/unit_tests/**` or a scoped jest suite was treated as a hard filter**,
not a preference. Each candidate below names its host.

## Summary

| # | Candidate | Class | Evidence strength |
|---|---|---|---|
| [C1](#c1) | `is_mutating()` misses `CALL` on every dialect but Postgres | `security` | **Strong** — read the code, reproduced against sqlglot |
| [C2](#c2) | Flask held at 2.3.3 with an unpatched advisory | `security-dep` | **Strong** — advisory resolved via OSV; constraint graph checked |
| [C3](#c3) | Dataset Hour Offset ignored before time-grain truncation | `bug` | **Medium-strong** — code read and confirmed; upstream issue open |
| [C4](#c4) | DOMPurify 3.4.12 below the fixed version | `frontend-dep` | **Medium** — advisory real, exploitability at our call sites not shown |
| [C5](#c5) | Skipped `BackgroundStyleDropdown` test in `Row.test.tsx` | `flaky-test` | **Strong** — root cause fully traced |
| [C6](#c6) | O(2N) query loop in `TagDAO.create_custom_tagged_objects` | `perf` | **Strong** — code read; batching idiom already in the same file |
| [C7](#c7) | Legacy `Query.get()` on live code paths | `deprecation` | **Strong** — 7 live sites; warnings already errors in `pytest.ini` |
| [C8](#c8) | `warn_unused_ignores` relaxed on two small security commands | `typing` | **Medium** — small and safe, but low drama |

Eight candidates, eight distinct classes — one per class in
[01](./01-overview.md#issue-classes). The portfolio assessment is in
[the closing section](#portfolio-criteria).

---

## C1 — `is_mutating()` misses `CALL` on every dialect but Postgres {#c1}

**Class:** `security`

### Evidence

`superset/sql/parse.py:1000-1005` gates the `exp.Command` fallback check on a single dialect:

```python
if (
    self._dialect == Dialects.POSTGRES
    and isinstance(self._parsed, exp.Command)
    and self._parsed.name.upper() in self._POSTGRES_MUTATING_COMMAND_NAMES
):
    return True
```

sqlglot parses certain statements as an opaque `exp.Command` rather than a structured AST, so
node-type matching cannot see the mutation inside them. The set at `superset/sql/parse.py:758`,
`_POSTGRES_MUTATING_COMMAND_NAMES`, already lists `CALL` among those risks — the author identified
the hazard correctly and then scoped the guard to one dialect.

Reproduced by importing the module against the pinned sqlglot 30.15.0: `CALL some_proc()` returns
`is_mutating=False` on mysql, snowflake, presto, trino, oracle, bigquery and duckdb, and `True` only
on postgresql. On mysql, `REPLACE INTO`, `RENAME TABLE` and `OPTIMIZE TABLE` are missed as well.

This matters because `has_mutation()` is the enforcement point for the "Allow DML" read-only guard —
`superset/sql_lab.py:468` plus four other call sites. A SQL Lab user on a connection configured with
`allow_dml=False` can therefore mutate data through a stored procedure on any non-Postgres engine.
This is the same bug class as the already-patched GHSA-787v-v9vq-4rgv, which is what makes the
narrow scoping conspicuous rather than merely incomplete.

**Not a CVE.** All 31 published `apache-superset` GHSAs are patched at or below 6.0.0 and none apply
to this checkout. C1 is a first-party finding, and the issue text should say so rather than imply an
advisory exists.

### Fixed means

The `exp.Command` fallback is evaluated for every dialect, with the per-dialect keyword set
generalised rather than hardcoded to Postgres. Dialect-specific extras (`REPLACE`, `RENAME`,
`OPTIMIZE` on mysql) are folded into the same mechanism.

### Regression test

Host: `tests/unit_tests/sql/parse_tests.py`. `test_is_mutating` (~line 1542) already parametrizes
every engine but never exercises the Command-fallback path;
`test_is_mutating_postgres_command_constructs` (~line 1758) tests `CALL` but hardcodes postgresql.
The new case asserts:

```python
assert SQLStatement("CALL some_proc()", engine="mysql").is_mutating() is True
```

which fails on the current code and passes after the fix.

### Blast radius

Backend only. One module (`superset/sql/parse.py`) plus its test file. No frontend, no migration, no
API surface change. The behavioural risk is in the other direction — a statement previously allowed
becomes blocked — so the fix should be reviewed for over-blocking, and the test matrix extended
across dialects rather than narrowed.

### Why it is a good target

The strongest candidate in the set. It is a genuine security defect in Superset's own code, requires
reading a parser to understand, is provable by a two-line unit test in the narrowed CI, and the
root-cause narrative ("the risk was identified and the guard was scoped too tightly") is exactly
what `structured_output.root_cause` is supposed to capture.

---

## C2 — Flask held at 2.3.3 with an unpatched advisory {#c2}

**Class:** `security-dep`

### Evidence

`requirements/base.txt:112` pins `flask==2.3.3`. OSV reports that version as affected by
**GHSA-68rp-wp8r-4726** / **CVE-2026-27205** (LOW, "Flask session does not add `Vary: Cookie` header
when accessed in some ways"), with `introduced: 0` and `fixed: 3.1.3`. Some forms of session access,
notably the Python `in` operator, do not mark the response as varying on the cookie, so a caching
proxy in front of Superset can serve one user's page to another.

The interesting part is why the pin is stuck. `pyproject.toml:55` already permits
`flask>=2.2.5, <4.0.0`, so the abstract constraint is not the blocker. Every Flask extension in the
lock was checked against its PyPI metadata:

| Package | Declared Flask constraint |
|---|---|
| `flask-appbuilder==5.2.2` | `Flask<4,>=2` |
| `flask-jwt-extended==4.7.1` | `Flask<4.0,>=2.0` |
| `flask-caching==2.4.1` | `flask>=2.0` |
| `flask-session==0.8.0` | `flask>=2.2` |
| `flask-babel==3.1.0` | `Flask (>=2.0)` |
| `flask-limiter==3.12` | `Flask>=2` |
| `flask-migrate==4.1.0` | `Flask>=0.9` |
| `flask-compress==1.24` | none |

**Nothing in the graph caps Flask below 4.** The lock is simply stale. Supporting evidence that the
current state is untested rather than intentional: `requirements/base.txt:476` pins
`werkzeug==3.1.6` — floored deliberately in `requirements/base.in` for CVE-2026-27199 — alongside a
Flask release that predates Werkzeug 3 entirely. That combination is off upstream's tested matrix.

`requirements/base.in` is the right place for the fix, and the file is already written in exactly
this idiom: a list of security-driven floor pins, each with a `# Security: CVE-…` comment.

### Fixed means

A floored pin in `requirements/base.in` (`flask>=3.1.3,<4.0.0`, with the CVE comment the file's
convention requires), the lock recompiled with `uv pip compile`, and Superset's own call sites
adapted to the Flask 3 API removals.

### Regression test

Weaker here than elsewhere, and worth stating plainly: a dependency floor is asserted by the
manifest, not by a behavioural test. The honest acceptance evidence is that `pre-commit` and the
scoped `pytest` pass on the recompiled lock, plus a test in `tests/unit_tests/config_test.py`
asserting the application still initialises. Devin should be instructed that the manifest change is
the fix and the test suite is the guard against regression, not the proof of the CVE.

### Blast radius

Backend, but wide by nature: `requirements/base.txt` and `requirements/development.txt` are both
regenerated, touching many pinned lines. Application code changes should be small — the Flask 3
removals (`flask.Markup`, `flask.escape`, `before_first_request`, `app.json_encoder`,
`flask.json.JSONEncoder`, `_app_ctx_stack`) were grepped across `superset/` and `tests/unit_tests/`
and **all have zero live hits**. The only `JSONEncoder` matches in `superset/utils/json.py` are
`simplejson`, not `flask.json`. The residual risk sits in Flask-AppBuilder, not in Superset.

### Why it is a good target, and where it is weak

Good: the constraint analysis is real work — establishing that nothing blocks the upgrade required
checking eight packages' metadata, which is precisely the kind of investigation that gets skipped
when a human triages a dependency alert in thirty seconds.

Weak: the class is defined as "a CVE fix in a dependency that requires adapting to a breaking API
change," and the grep suggests Superset's own call sites may need **no** changes at all. If the
recompile is clean, this degrades toward a version bump. That is a real possibility and it is why C2
is not counted among the diagnosis-heavy candidates below. The CVE itself is also LOW severity and
conditional on a caching proxy being deployed.

---

## C3 — Dataset Hour Offset ignored before time-grain truncation {#c3}

**Class:** `bug`

### Evidence

`superset/connectors/sqla/models.py:1130-1171`, `get_timestamp_expression()`. The final expression is
built at line 1170:

```python
time_expr = self.db_engine_spec.get_timestamp_expr(col, pdf, time_grain)
```

`self.offset` — the dataset column's Hour Offset, declared at line 219 — is never consulted, and
`get_timestamp_expr` takes no offset parameter. So the database truncates on the *unshifted* column.

The offset is honoured in two other places, which is what makes this a defect rather than a design
choice: `superset/models/helpers.py:3298-3302` shifts the filter bounds by the offset, and
`superset/utils/core.py:2083-2084` shifts the fetched dataframe for display. With grain = day and
offset = -4, the database still buckets rows on UTC midnight; only the label moves. Rows land in the
wrong bucket while appearing correctly labelled — the worst failure mode for an analytics tool,
because the chart looks right.

Upstream issue: https://github.com/apache/superset/issues/40871 (open).

**Unverified.** The comment near `superset/models/helpers.py:3298` carries a `(#104810)` reference
that matches no real `apache/superset` issue. It looks like a placeholder baked into this fork.
Devin should not chase it, and T51 should not quote it in the issue body.

### Fixed means

The offset is applied to the column expression *before* time-grain truncation, so bucketing and
labelling agree. The display-side shift in `superset/utils/core.py` must not then double-count —
resolving that interaction is the substance of the fix, not the one-line expression change.

### Regression test

Host: `tests/unit_tests/connectors/sqla/models_test.py`. Construct a `TableColumn` with
`offset=-4`, call `get_timestamp_expression(time_grain="P1D")`, and assert the compiled SQL contains
the offset shift inside the `DATE_TRUNC` argument. Currently the compiled string contains no shift
at all, so the assertion fails without the fix.

### Blast radius

Backend only, but a sensitive area: `superset/connectors/sqla/models.py` feeds every time-series
chart. `superset/utils/core.py` may need a companion change to avoid double-shifting. No frontend,
no migration. Heavier upstream workflows covering chart data should be run on the PR before merge,
per [08](./08-testing.md).

### Why it is a good target

Real diagnosis. The defect is only visible once you have read three files and noticed that two of
them compensate for something the third never did. There is no advisory, no lint rule and no failing
test pointing at it — an agent has to reason about it, which is the capability under evaluation.

---

## C4 — DOMPurify 3.4.12 below the fixed version {#c4}

**Class:** `frontend-dep`

### Evidence

Scanning all 2,819 unique `package@version` pairs in
`superset-frontend/package-lock.json` against OSV returned eight hits; after removing the ones whose
locked version is already at or above the fix, **DOMPurify is the only genuinely unpatched direct
dependency**. The lockfile resolves `dompurify` to 3.4.12; **GHSA-55q2-fjhq-7xh7** (MODERATE,
"IN_PLACE hook removal leaves a detached subtree executable, causing XSS") is fixed in 3.4.13.

It is declared in three manifests, all of which admit 3.4.13 under their caret ranges:

| Manifest | Line | Range |
|---|---|---|
| `superset-frontend/package.json` | 398 | `^3.4.11` |
| `superset-frontend/packages/superset-ui-core/package.json` | 71 | `^3.4.12` |
| `superset-frontend/plugins/legacy-preset-chart-nvd3/package.json` | 34 | `^3.4.12` |

Call sites: `superset-frontend/plugins/legacy-preset-chart-nvd3/src/utils.ts` (seven `sanitize()`
calls at lines 112, 141, 165, 195, 226, 274, 290),
`superset-frontend/packages/superset-ui-core/src/components/AsyncAceEditor/Tooltip.tsx:35`, and
references in `superset-frontend/src/utils/navigationUtils.ts`.

**Honest limit on exploitability.** The advisory's attack path requires `IN_PLACE` sanitization
combined with a hook that removes an element. Grepping the frontend for `IN_PLACE`, `addHook` and
`removeHook` found **no matches** — every Superset call site uses the default string-in/string-out
`sanitize()`. So the dependency is unambiguously below its fixed version, but the specific attack
described in the advisory does not obviously reach Superset. The issue should say this rather than
overstate the risk.

### Fixed means

The lockfile resolves `dompurify` to ≥3.4.13, with the caret ranges left alone since they already
permit it, plus a short call-site audit recording that no `IN_PLACE` or hook usage exists.

### Regression test

Host: `superset-frontend/plugins/legacy-preset-chart-nvd3/test/utils.test.ts` (358 lines, exercises
the tooltip sanitizers directly). Scoped jest runs it because the package manifest changes. As with
C2, the honest framing is that the version floor is the fix and the existing suite guards against
sanitizer-behaviour regressions across the bump; a test cannot assert "not vulnerable."

### Blast radius

Frontend only, three manifests plus `package-lock.json`. A patch-level bump within the same major,
so API breakage is unlikely. This is the one candidate that exercises the scoped-jest half of the CI
narrowing, which is worth having represented in the portfolio.

### Why it is weak

The weakest of the eight, and marked as such. A patch bump inside a major version with no forced
call-site change is close to the "just a dependency bumper" failure mode
[01](./01-overview.md) explicitly warns against. It earns its place by covering the
`frontend-dep` class and the jest path, not by difficulty. Two further hits worth noting sit
adjacent to it: `nanoid` and `js-yaml` appear only transitively, and `image-size 0.7.5` is reachable
only through a dev toolchain path.

---

## C5 — Skipped `BackgroundStyleDropdown` test in `Row.test.tsx` {#c5}

**Class:** `flaky-test`

### Evidence

`superset-frontend/src/dashboard/components/gridComponents/Row/Row.test.tsx:223`:

```javascript
/* oxlint-disable-next-line jest/no-disabled-tests */
test.skip('should render a BackgroundStyleDropdown when focused', () => {
  const { rerender } = setup({ component: rowWithoutChildren });
  expect(screen.queryByTestId('background-style-dropdown')).toBeFalsy();
```

The root cause is fully traceable statically: **nothing anywhere in the repository sets
`data-testid="background-style-dropdown"`.** `BackgroundStyleDropdown.tsx` renders through
`PopoverDropdown` and never assigns a test id, so `queryByTestId` can never resolve regardless of
component state. The test was written against an intended test id that was never wired up, and was
skipped rather than fixed.

### Fixed means

Either the test id is added to `BackgroundStyleDropdown` — the better fix, since the dropdown is a
legitimate testing target — or the assertions are rewritten to query by role. The test is then
un-skipped and the `oxlint-disable-next-line jest/no-disabled-tests` suppression removed.

### Regression test

The un-skipped test *is* the regression test. It fails on the current tree (the second assertion
cannot find the dropdown after the settings button is clicked) and passes once the test id exists.
Scoped jest runs it because `superset-frontend/src/dashboard/**` is touched.

### Blast radius

Frontend only: the test file plus `BackgroundStyleDropdown.tsx`. A `data-testid` attribute has no
runtime behaviour.

### Why it is a good target

Small, certain, and completely verifiable inside the narrowed CI — a useful counterweight to the
open-ended candidates. It also demonstrates the class honestly: the agent must work out *why* the
query fails, and the answer is not in the test file.

**A note on the class name.** This test is deterministically broken rather than intermittent. The
`flaky-test` class in [01](./01-overview.md) covers "a skipped or intermittent test, diagnosed and
re-enabled," so it fits, but the issue title should say "skipped," not "flaky."

**Rejected sibling.** `Column.test.tsx:180` is superficially similar and should *not* be filed: it
mixes stale Enzyme `.find()`/`.simulate()` calls against an RTL render and contains an assertion
comparing a DOM node to a plain object. Fixing it means rewriting the test wholesale, which makes
"does the fix work?" unanswerable.

---

## C6 — O(2N) query loop in `TagDAO.create_custom_tagged_objects` {#c6}

**Class:** `perf`

### Evidence

`superset/daos/tag.py:46-70`. The loop at line 54 issues two queries per tag:

```python
for name in clean_tag_names:
    type_ = TagType.custom
    tag = TagDAO.get_by_name(name, type_)          # SELECT #1, per tag

    existing_tagged_object = (
        db.session.query(TaggedObject)              # SELECT #2, per tag
        .filter_by(object_id=object_id, object_type=object_type, tag=tag)
        .first()
    )
```

`2N + 1` statements for `N` tags. Reached from `superset/commands/utils.py:282` on every dashboard,
chart, dataset and saved-query tag update, so it is on a routine interactive path.

The fix idiom already exists eleven lines further down the same file: `find_by_names`
(`superset/daos/tag.py:136-141`) resolves a whole list in a single `Tag.name.in_(...)` query. The
codebase knows how to do this; this call site was not converted.

### Fixed means

Both lookups are batched — one `Tag.name.in_(...)` query and one `TaggedObject` query filtered by
the resolved tag ids — giving a constant number of statements independent of `N`, with identical
observable behaviour.

### Regression test

Host: `tests/unit_tests/dao/tag_test.py`.

**Known obstacle, stated up front.** There is no `assert_num_queries` helper anywhere in the
repository. The regression test must therefore install its own SQLAlchemy
`before_cursor_execute`/`after_cursor_execute` event listener, count statements while creating
tagged objects for several tags, and assert the count does not scale with the number of tags. This
is the most interesting part of the task and the part most likely to stall an agent, so the issue
body must name the obstacle explicitly rather than leaving it to be discovered.

### Blast radius

Backend only: `superset/daos/tag.py` and its unit test. No schema change, no API change. Behaviour
must be identical, including the de-duplication semantics the current loop provides.

### Why it is a good target

The measurement is the work. Anyone can rewrite the loop; producing a test that *proves* the query
count dropped requires building instrumentation the repository does not have. That is a good
demonstration of an agent doing engineering rather than pattern-matching, and it produces the
before/after number the `perf` class calls for.

---

## C7 — Legacy `Query.get()` on live code paths {#c7}

**Class:** `deprecation`

### Evidence

This is tracked debt, not incidental drift. `pytest.ini:28-47` already promotes eight named
`RemovedIn20Warning` patterns to hard errors, and `pyproject.toml:118` pins
`sqlalchemy>=1.4.43,<2` with comments citing the pending 2.0 bump.

`Query.get()` is legacy in SQLAlchemy 1.4 and removed in 2.0, superseded by `Session.get()`. Seven
live sites outside migrations:

| File | Line |
|---|---|
| `superset/security/manager.py` | 3013 |
| `superset/cli/export_example.py` | 158 |
| `superset/mcp_service/chart/preview_utils.py` | 84 |
| `superset/commands/dataset/duplicate.py` | 68 |
| `superset/commands/importers/v1/examples.py` | 72 |
| `superset/commands/sql_lab/estimate.py` | 69 |
| `superset/daos/dataset.py` | 633 |

The last two are covered by existing unit tests. Migration files under
`superset/migrations/versions/` also match and must be left alone — they are frozen historical
artefacts.

**Scope discipline matters here.** The broader `session.query(` pattern has 588 occurrences across
217 files; converting it wholesale would be unreviewable. The `.get()` shape is a bounded,
mechanical, seven-site change with a clear correctness argument.

**Negative findings worth recording**, because they narrow the field and explain why this shape was
chosen: `datetime.utcnow()`, the deprecated pandas APIs (`DataFrame.append`, `applymap`,
`iteritems`, `fillna(method=)`, `infer_datetime_format`) and the old `flask.Markup` imports all have
**zero** live hits. The obvious deprecation targets are already clean.

### Fixed means

All seven live sites use `db.session.get(Model, id)`. Migrations untouched.

### Regression test

Host: `tests/unit_tests/commands/sql_lab/` for `estimate.py`, and the existing DAO tests for
`superset/daos/dataset.py:633`. The durable guard is a test that runs the converted path with
`RemovedIn20Warning` promoted to an error — the mechanism `pytest.ini` already establishes — so the
test fails on the old call and passes on the new one. Devin should extend the `pytest.ini`
`filterwarnings` list with the `Query.get()` pattern in the same PR, which is what makes the fix
permanent rather than a one-off edit.

### Blast radius

Backend only, seven files, one line each, plus `pytest.ini`. Wide but shallow. `session.get()` and
`Query.get()` have the same identity-map semantics, so behaviour is unchanged.

### Relationship to C2

These two are the closest pair in the portfolio and the relationship should be acknowledged rather
than glossed over: both touch the Flask/SQLAlchemy stack being held back. They are kept distinct by
scope and by ordering. C7 is valid *within* SQLAlchemy 1.4 today and does not depend on any upgrade;
C2 is a Flask floor in a different manifest. C7 should be filed and merged first so the two never
collide in the same file.

---

## C8 — `warn_unused_ignores` relaxed on two small security commands {#c8}

**Class:** `typing`

### Evidence

`pyproject.toml:348-361` disables `warn_unused_ignores` for eight modules:

```toml
# Disable warn_unused_ignores for modules with dynamic type assignments
# These type: ignore comments are needed in CI where superset-core types aren't visible
[[tool.mypy.overrides]]
module = [
    "superset.core.api.core_api_injection",
    "superset.security.manager",
    "superset.daos.base",
    "superset.connectors.sqla.models",
    "superset.tags.filters",
    "superset.commands.security.update",
    "superset.commands.security.create",
    "superset.semantic_layers.api",
]
warn_unused_ignores = false
```

Two entries are small enough to fix properly rather than paper over:

| Module | Lines | Suppression |
|---|---|---|
| `superset/commands/security/create.py` | 73 | `:67` `# type: ignore[attr-defined]` |
| `superset/commands/security/update.py` | 95 | `:83` `# type: ignore[attr-defined]` |

Both suppress the same construct, `SqlaTable.id.in_(self._tables)`, and both are directly exercised
by `tests/unit_tests/commands/security/rls_test.py`.

The rest of the list should be avoided: `superset/security/manager.py` is 5,044 lines and
`superset/connectors/sqla/models.py` is 2,368. `superset/tags/filters.py` (119 lines, two
`# type: ignore[union-attr]`, tested by `tests/unit_tests/tags/filters_test.py`) is a viable
substitute if the security commands turn out to be blocked by the `superset-core` visibility problem
the config comment describes.

### Fixed means

The `attr-defined` suppressions are removed by giving mypy the information it lacks — a proper
annotation or a narrowing cast — and the two modules are dropped from the `warn_unused_ignores`
override list, so an unused ignore in them becomes an error again.

### Regression test

`mypy` itself is the test, and it runs via `pre-commit` on changed files in the narrowed CI. The
proof is directional: removing the two modules from the override list makes CI fail if a stale
ignore is reintroduced. `tests/unit_tests/commands/security/rls_test.py` guards the behaviour.

### Blast radius

Backend only: two 70-95 line modules plus `pyproject.toml`. Confined.

### Why it is a good target, and where it is weak

Good: the class exists to show that type-tightening surfaces real defects, and shrinking a config
exclusion list is a durable, verifiable improvement rather than a cosmetic one.

Weak: the honest expectation is that `SqlaTable.id.in_(...)` is a mypy blind spot around
SQLAlchemy's declarative attributes, not a latent bug — so the "surfacing real defects in the
process" half of the class description may not materialise. If a defect does surface, this becomes a
much better story; that outcome cannot be promised in advance.

---

## Alternates considered and not selected

| Candidate | Class | Why not selected |
|---|---|---|
| `test_update_with_password_mask`, `tests/unit_tests/databases/api_test.py:350` | `flaky-test` | `@pytest.mark.skip(reason="Works locally but fails on CI")`, asserting that updating a gsheets database with a masked private key does not clobber the stored secret. Genuinely flaky and correctly located under `tests/unit_tests/`. Not selected because the fork has **no CI history to reproduce against** ([B2](./blockers.md)), so the agent would be diagnosing a failure it cannot observe. Promote this if C5 proves too small once run. |
| `Column.test.tsx:180` | `flaky-test` | Requires a wholesale rewrite (Enzyme idioms against an RTL render), which makes success unmeasurable. |
| `cryptography==49.0.0` — GHSA-g6cj-pr64-35w5 (HIGH) | `security-dep` | Fixed only in 50.0.0, which is excluded by a deliberate `cryptography>=49.0.0,<50.0.0` cap in both `pyproject.toml:53` and `requirements/base.in`. Raising the cap is a maintainer decision, not an autofix. |
| `setuptools==80.9.0` — GHSA-h35f-9h28-mq5c | `security-dep` | Held by an explicit `setuptools<81` pin whose reason is documented in `requirements/base.in` and in `docs/docs/contributing/pkg-resources-migration.md`. Bumping it contradicts a recorded decision. |
| `paramiko==3.5.1` — GHSA-r374-rxx8-8654 | `security-dep` | `last_affected: 4.0.0` with no fixed version published. Nothing to upgrade to. |
| `xlsx 0.20.3` — GHSA-4r6h-8v6p-xvw6, GHSA-5pgg-2g8v-p4x9 | `frontend-dep` | A permanent false positive. Both advisories carry `introduced: 0` and **no fixed version**, because the npm package is abandoned; the patched builds (0.19.3, 0.20.2) exist only on the SheetJS CDN. `superset-frontend/package.json:236` already pins the CDN tarball `xlsx-0.20.3`, which is *above* both thresholds. Not a defect — but see the operational note below. |
| `db.session.merge()` at `superset/daos/base.py:508` | `deprecation` | Inside `BaseDAO.update()`, inherited by roughly 28 DAOs. Correct in principle, blast radius too large to review confidently. |
| Broad `session.query(...)` conversion | `deprecation` | 588 occurrences across 217 files. Unreviewable. |

**Operational note arising from `xlsx`.** The scheduled vulnerability sweep
([05](./05-devin-integration.md)) will report `xlsx` on every single run, forever, because the
advisories can never be satisfied by a version constraint. The sweep needs a suppression list with a
recorded justification, or it will file the same non-issue indefinitely and train reviewers to
ignore it. This is a finding about Sentinel, not about Superset, and belongs on the sweep's own
backlog rather than in this set.

---

## What could not be verified {#unverified}

Stated here rather than buried, per [08](./08-testing.md).

- **The `(#104810)` reference** near `superset/models/helpers.py:3298` matches no real
  `apache/superset` issue. Treated as a placeholder in this fork. (C3)
- **Whether Flask 3.1.3 resolves cleanly.** No resolver run was attempted — the constraint analysis
  is from PyPI metadata for each package, not from an executed `uv pip compile`. Flask-AppBuilder
  5.2.2 declares `Flask<4,>=2` but was not tested against Flask 3.1.3. (C2)
- **Whether C5's missing test id is the *only* reason the test was skipped.** The jest suite was not
  executed; the root cause is established by static reading. A second cause may surface once the
  test runs. (C5)
- **DOMPurify exploitability at Superset's call sites.** No `IN_PLACE` or hook usage was found, so
  the advisory's described path appears not to apply. This is an absence-of-evidence argument, not a
  proof of safety. (C4)
- **C6's actual query counts.** Derived by reading the loop, not measured. `2N + 1` is the
  statement count implied by the code, not an observed figure. (C6)
- **Whether C8 surfaces a real defect.** Expected to be a mypy blind spot around SQLAlchemy
  declarative attributes rather than a latent bug. (C8)
- **Superset's own advisories.** All 31 published `apache-superset` GHSAs are patched at or below
  6.0.0; none apply to this checkout. C1 is a first-party finding with no advisory behind it.

---

## Portfolio criteria {#portfolio-criteria}

Checked against the portfolio-level criteria in [08](./08-testing.md).

### At least six distinct issue classes — **met, with margin**

Eight candidates across **eight** classes, one each: `security` (C1), `security-dep` (C2), `bug`
(C3), `frontend-dep` (C4), `flaky-test` (C5), `perf` (C6), `deprecation` (C7), `typing` (C8). Every
class in [01](./01-overview.md#issue-classes) is exercised. One per class was chosen deliberately
over doubling up on the strongest class, so that a failure in any single candidate costs coverage of
one class rather than leaving a class unrepresented
([ADR](./adr/2026-08-08-select-only-unit-testable-targets.md)).

### At least two showing genuine diagnosis — **met**

Four qualify, which gives headroom if one falls through:

- **C1** — requires reading a SQL parser and understanding why `exp.Command` defeats node-type
  matching. No test, lint rule or advisory points at it.
- **C3** — requires noticing that two files compensate for something a third never did. Invisible
  from any single file.
- **C6** — requires building query-counting instrumentation the repository does not have.
- **C5** — requires establishing that a test id referenced by the test exists nowhere in the tree.

C2, C4, C7 and C8 are the mechanical half of the portfolio, which is the correct proportion: the
point is a system that handles both, not eight hard problems.

### No two attacking the same underlying problem — **met, with one caveat**

Eight distinct root causes. The one pair worth flagging is **C2 and C7**, both touching the
Flask/SQLAlchemy stack that is being held back. They are separated by scope (a Flask floor in
`requirements/base.in` versus seven `Query.get()` call sites), by manifest, and by ordering — C7
merges first. A reviewer could reasonably call them adjacent, so it is recorded here rather than
left to be noticed.

**C2 and C4** are both dependency advisories, but the taxonomy separates `security-dep` and
`frontend-dep` by design, and they share no package, ecosystem, manifest or call site.

### Weakest candidates, ranked

1. **C4 (DOMPurify)** — a patch bump inside a major version, with no forced call-site change and no
   demonstrated exploitability. Closest to the "dependency bumper" failure mode. Kept for
   `frontend-dep` coverage and because it is the only candidate exercising the scoped-jest path.
2. **C2 (Flask)** — the constraint analysis is genuine, but if the recompile is clean it degrades to
   a version bump. The CVE is LOW and conditional on a caching proxy.
3. **C8 (typing)** — safe and bounded, but the "surfacing real defects" half of the class may not
   materialise.

C4 and C2 together are why the diagnosis criterion was over-satisfied rather than met exactly.

### Risk to the set as a whole

Every candidate names a test host that the narrowed CI actually runs, so none of them can fail for
the structural reason that killed the alternatives. The residual risks are per-candidate and stated
in each section. The largest single risk is **C6**, where the missing `assert_num_queries` helper
means the regression test is a small piece of infrastructure work rather than an assertion — worth
watching in the first run, and the most likely candidate to consume its ACU budget in the test loop
([B11](./blockers.md)).
