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
| [C2](#c2) | paramiko 3.5.1 accepts SHA-1, and the fix crosses two majors | `security-dep` | **Strong** — advisory resolved via OSV; both blockers read in the tree |
| [C3](#c3) | Dataset Hour Offset ignored before time-grain truncation | `bug` | **Medium-strong** — code read and confirmed; upstream issue open |
| [C4](#c4) | deck.gl family carries a transitive `image-size` DoS | `frontend-dep` | **Medium-strong** — advisories verified; CI cannot exercise the parsers |
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

## C2 — paramiko 3.5.1 accepts SHA-1, and the fix crosses two majors {#c2}

**Class:** `security-dep`

### Evidence

`requirements/base.txt:283` pins `paramiko==3.5.1`. OSV reports it affected by
**GHSA-r374-rxx8-8654** / **PYSEC-2026-2858** / **CVE-2026-44405** — "Paramiko rsakey.py allows the
SHA-1 algorithm." A weak signature algorithm is not rejected, so an `ssh-rsa`+SHA-1 host key or
signature is accepted where it should be refused. Relevant to Superset because SSH tunnels are a
supported way to reach a database.

What makes this a real remediation rather than a pin bump is that **two independent, documented
blockers sit in front of the upgrade**, both visible in the tree.

The first is a constraint the project has already collided with. `pyproject.toml:99`:

```toml
"paramiko>=3.4.0, <4.0", # 4.0 removed DSSKey, still referenced by sshtunnel
```

Diffing `paramiko/__init__.py` between tags 3.5.1 and 5.0.0 confirms `DSSKey` is exported in 3.5.1
and gone in 5.0.0. Superset does not use `DSSKey` itself, but its hard dependency `sshtunnel` does,
and Superset imports `sshtunnel` at `superset/extensions/ssh.py:26` and `superset/models/core.py:40`
(used at `ssh.py:115-116`, `176`, `185`, `213`, `253`).

The second is easy to miss and is the better test of whether an agent actually investigates.
`pyproject.toml:541`, inside `[tool.liccheck.authorized_packages]` under a block headed
`# TODO REMOVE THESE DEPS FROM CODEBASE`:

```toml
paramiko = "3"  # GPL
```

That allowlist entry is keyed on the **major version**. Moving to 5.x without updating it fails the
licence check, in a file the diff would not otherwise touch.

Superset's own paramiko surface is concentrated in `superset/extensions/ssh.py`, which imports
`RSAKey, ECDSAKey, Ed25519Key, PasswordRequiredException, PKey, SSHException` (lines 28-36) plus
`paramiko.pkey.UnknownKeyType`, and calls `RSAKey.from_private_key` (69),
`paramiko.PKey.from_type_string` (103) and `paramiko.Transport` / `.start_client` /
`.get_remote_server_key` (183-186). Those are the call sites a two-major jump has to be checked
against.

**Inferred, not verified.** OSV's `affected` block gives only `last_affected: 4.0.0` with **no
`fixed` event**. That paramiko 5.0.0 is the fixed version was inferred by reading `rsakey.py` at that
tag, where a comment states "we no longer want to have ssh-rsa+SHA1 in HASHES". The issue body must
carry that caveat so Devin verifies the fixed version rather than trusting it.

### Fixed means

paramiko is raised to a release that rejects SHA-1 — 5.0.0 on current evidence — with the
`pyproject.toml:99` cap raised, the `sshtunnel`/`DSSKey` dependency resolved (upgrading `sshtunnel`
or establishing that it no longer needs `DSSKey`), the `liccheck` entry at `pyproject.toml:541`
updated to the new major, and the lock recompiled.

### Regression test

Host: `tests/unit_tests/extensions/ssh_test.py`, which already exists and covers the tunnel factory.
The behavioural assertion is that `ssh.py`'s key-loading and transport paths still work across the
two-major jump. As with any dependency floor, the manifest asserts the fix and the suite guards
against regression — but unlike a patch bump, there is a substantial chance of real breakage here,
which is exactly what makes the test meaningful.

### Blast radius

Backend, and crosses more files than a dependency change usually does: `pyproject.toml` in two
separate sections, `requirements/base.txt` and `requirements/development.txt` regenerated,
`superset/extensions/ssh.py`, and possibly the `sshtunnel` pin. No frontend, no migration.

### Why it is a good target

The best dependency candidate in the set, and the one that genuinely earns the `security-dep`
description "requires adapting to a breaking API change." The agent has to discover *why* the pin
exists, deal with a break the maintainers already documented and worked around once, and find a
second constraint hiding in a licence-checking table. A naive bump fails; a correct fix requires
reading the tree.

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

## C4 — deck.gl family carries a transitive `image-size` DoS {#c4}

**Class:** `frontend-dep`

### Evidence

`npm audit` — run twice, the second time against freshly copied manifests to prove the cached result
was not stale — reports `@deck.gl/geo-layers`, `@deck.gl/mesh-layers` and `@luma.gl/gltf` as direct
dependencies with `isSemVerMajor: true` fixes. The chain bottoms out in `image-size`:

```
texture-compressor → @loaders.gl/textures → @loaders.gl/gltf
                   → @luma.gl/gltf / @deck.gl/mesh-layers → @deck.gl/geo-layers
```

The two advisories on `image-size` are **GHSA-w3rx-r6r6-pgpr** (ICNS parser infinite-loop DoS) and
**GHSA-5p2g-fcmc-qvqq** (JXL/HEIF parser). Both were independently corroborated by scanning all
2,819 unique `package@version` pairs in `superset-frontend/package-lock.json` against OSV, which
returned `image-size 0.7.5` carrying exactly that pair.

The three flagged packages are declared at `superset-frontend/package.json:99,101,121` and
`superset-frontend/plugins/preset-chart-deckgl/package.json:33,36`.

**The interesting part.** Grepping the frontend source for those three package names returns
**zero import sites** — the only matches are the manifest declarations themselves. They exist purely
to hold the deck.gl family's peer versions together. The real exposure is the family: deck.gl
requires matching majors across subpackages, so remediating one forces all of them, and that reaches
**30 import lines across 23 files** under `plugins/preset-chart-deckgl/src/**`:

| Subpackage | Import lines |
|---|---|
| `@deck.gl/core` | 15 (e.g. `DeckGLContainer.tsx:34`) |
| `@deck.gl/layers` | 6 |
| `@deck.gl/aggregation-layers` | 5 |
| `@deck.gl/mapbox` | 4 (across 2 files) |

The family is pinned with tildes in the 9.2/9.3 range — `~9.2.5` for most, with
`@deck.gl/aggregation-layers` at `~9.2.11`, `@deck.gl/extensions` at `~9.2.9` and `@deck.gl/mapbox`
at `~9.3.7` in the plugin manifest. All are within major 9, which is what the lockstep requirement
actually constrains.

### Fixed means

The deck.gl and luma.gl subpackages are moved together to a major that resolves `image-size` above
both advisories, with the 30 import sites updated for whatever the major bump changes, and the
`preset-chart-deckgl` plugin's own jest suite passing.

### Regression test

Host: the plugin's existing suites, ten of them, including
`superset-frontend/plugins/preset-chart-deckgl/src/DeckGLContainer.test.tsx`,
`CategoricalDeckGLContainer.test.tsx`, `Multi/Multi.test.tsx` and `layers/*.test.ts`. Scoped jest
runs them because the package is touched.

**The CI signal is weaker here than for any other candidate, and this must be said in the issue.**
The narrowed CI can prove that the deck.gl plugin still renders and that its tests pass across the
major bump — which is the real risk, since the bump breaks call sites. It cannot exercise the
vulnerable `image-size` parsers at all, because nothing in Superset's test suite feeds an ICNS, JXL
or HEIF file through them. The evidence is "the upgrade did not break the plugin," not "the
vulnerability is gone."

### Blast radius

Frontend only, but the largest diff of the eight: two manifests, `package-lock.json`, and up to 23
source files across `plugins/preset-chart-deckgl/src/**`. It does not cross into the backend. This
is the candidate that exercises the scoped-jest half of the CI narrowing.

### Why it is a good target, and where it is weak

Good: it is a genuinely instructive shape — the flagged packages are not the ones that matter, and
an agent that stops at "bump the three packages `npm audit` named" will produce a broken lockfile.
Working out that the fix is a family-wide major bump touching 30 call sites requires understanding
deck.gl's version coupling, which is real diagnosis.

Weak: the vulnerability itself is a denial of service in an image parser reached through a 3D model
loader — a long way from anything a Superset user does. And the CI limitation above means the
strongest available evidence is indirect. It is the right candidate for the class, but the security
value is modest and the issue should not pretend otherwise.

**Adjacent hits considered and not selected.** `dompurify` was also direct and genuinely unpatched
(3.4.12 locked, **GHSA-55q2-fjhq-7xh7** fixed in 3.4.13), but patch-only within the same major with
no forced call-site change, and grepping found no `IN_PLACE` or `addHook` usage, so the advisory's
attack path does not obviously reach Superset. `lerna` is build-only with no import sites.
`eslint-plugin-i18n-strings` is a malware finding with no fix available. `nanoid` and `js-yaml`
appear only transitively.

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

### Why it is a good target

Bounded, mechanical and permanently enforced once the `filterwarnings` entry lands — the fix cannot
silently regress. It is the least intellectually demanding backend candidate, and it is in the set
for exactly that reason: the portfolio needs to show the pipeline handling routine work as well as
diagnosis. The judgement being tested is **scope discipline** — recognising that the seven-site
`.get()` shape is the reviewable slice of a 588-occurrence problem, and stopping there.

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
| `cryptography==49.0.0` — GHSA-g6cj-pr64-35w5 (HIGH, PKCS#7 Bleichenbacher oracle) | `security-dep` | Real, and higher severity than C2. Rejected because the 50.0.0 changelog lists **no backwards-incompatible entries** and Superset's only uses are `default_backend`, `x509` loading and `serialization.load_pem_private_key` — none touched by the fix. An honest pin bump, which fails the "genuine diagnosis" bar the class is meant to demonstrate. |
| `flask==2.3.3` — GHSA-68rp-wp8r-4726 / CVE-2026-27205 (LOW) | `security-dep` | Genuinely unpatched (fixed in 3.1.3), and the constraint analysis was interesting: `pyproject.toml:55` already permits `flask>=2.2.5, <4.0.0`, and checking all eight Flask extensions' PyPI metadata showed **nothing in the graph caps Flask below 4** — the lock is simply stale. But that is the problem: the fix is already inside the allowed range, so it is a recompile. The Flask 3 removals (`flask.Markup`, `before_first_request`, `app.json_encoder`, `_app_ctx_stack`) have **zero live hits** in `superset/`, so no call-site adaptation is forced. |
| `setuptools==80.9.0` — GHSA-h35f-9h28-mq5c | `security-dep` | Held by an explicit `setuptools<81` pin whose reason is documented in `requirements/base.in` and in `docs/docs/contributing/pkg-resources-migration.md`. Also sdist build-time only, with no runtime call site. Bumping it contradicts a recorded decision. |
| `dompurify 3.4.12` — GHSA-55q2-fjhq-7xh7 | `frontend-dep` | Direct and genuinely unpatched, but patch-only within major 3 with no forced call-site change, and no `IN_PLACE`/`addHook` usage anywhere in the frontend, so the advisory's attack path does not obviously reach Superset. The closest runner-up for the class; see the adjacent-hits note under [C4](#c4). |
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
- **That paramiko 5.0.0 is the fixed version.** OSV gives only `last_affected: 4.0.0` with **no
  `fixed` event**. 5.0.0 was inferred by reading `rsakey.py` at that tag, where a comment states "we
  no longer want to have ssh-rsa+SHA1 in HASHES". Devin must confirm the fixed version rather than
  trusting this. (C2)
- **Whether paramiko 5.x resolves at all.** No resolver run was attempted. Whether `sshtunnel` has
  dropped its `DSSKey` dependency, and whether a compatible `sshtunnel` release exists, was not
  checked — that investigation is the substance of the task. (C2)
- **Which deck.gl major actually resolves `image-size`.** `npm audit` reports `isSemVerMajor: true`
  fixes for the three flagged packages, but the target version was not pinned down, and no upgrade
  was attempted against the 30 import sites. (C4)
- **Whether C5's missing test id is the *only* reason the test was skipped.** The jest suite was not
  executed; the root cause is established by static reading. A second cause may surface once the
  test runs. (C5)
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

### At least two showing genuine diagnosis — **met, with substantial margin**

Six of the eight qualify, which gives real headroom if one or two fall through:

- **C1** — requires reading a SQL parser and understanding why `exp.Command` defeats node-type
  matching. No test, lint rule or advisory points at it.
- **C2** — requires discovering *why* the pin exists, handling a break the maintainers already
  documented and worked around once, and finding a second constraint hidden in a licence-checking
  table. A naive bump fails.
- **C3** — requires noticing that two files compensate for something a third never did. Invisible
  from any single file.
- **C4** — requires working out that the three packages `npm audit` names have no import sites at
  all, and that the real fix is a family-wide major bump across 30 call sites.
- **C5** — requires establishing that a test id referenced by the test exists nowhere in the tree.
- **C6** — requires building query-counting instrumentation the repository does not have.

Only **C7** and **C8** are mechanical. That is a reasonable proportion — the point is a system that
handles both, not eight hard problems — but it is worth noting that swapping in paramiko and deck.gl
moved the balance decisively toward diagnosis, at the cost of two candidates that are now more
likely to need several fix cycles ([B11](./blockers.md)).

### No two attacking the same underlying problem — **met, with one caveat**

Eight distinct root causes, in eight different subsystems.

The pair worth flagging is **C2 and C4**. Their root causes are unrelated — a SHA-1 signature
algorithm in an SSH library, versus a DoS in an image parser reached through a 3D model loader — and
they share no package, ecosystem, manifest or call site. But their *remediation shape* is nearly
identical: in both, the advisory sits below the surface, and the fix is a major-version family
upgrade that breaks call sites. The criterion is about the underlying problem, which is satisfied,
but a reviewer looking at the two pull requests will see the same story twice. Recorded here rather
than left to be noticed.

C7 (`Query.get()`) is unrelated to every other candidate: it is valid within SQLAlchemy 1.4 as
pinned today and depends on no upgrade, so it shares nothing with the two dependency candidates
beyond being backend work.

### Weakest candidates, ranked

1. **C8 (typing)** — safe and bounded, but the "surfacing real defects" half of the class may not
   materialise. Now the weakest in the set.
2. **C4 (deck.gl)** — strong as an engineering task, weak as a security story: a DoS in an image
   parser Superset never knowingly invokes, and the narrowed CI cannot exercise the vulnerable code
   at all. The evidence it can produce is "the upgrade did not break the plugin."
3. **C7 (deprecation)** — correct and permanently enforced, but mechanical. Its value is scope
   discipline, not difficulty.

Note what changed: the two weakest candidates in the previous draft were both dependency bumps
carried purely for class coverage. Replacing them with paramiko and deck.gl removed that problem —
no candidate is now in the set *only* to fill a class.

### Risk to the set as a whole

Every candidate names a test host that the narrowed CI actually runs, so none can fail for the
structural reason that eliminated the alternatives. Residual risks are per-candidate and stated in
each section. The three worth watching in the first runs:

- **C6** — the missing `assert_num_queries` helper means the regression test is a small piece of
  infrastructure work rather than an assertion.
- **C2** — a two-major dependency jump with an unresolved transitive blocker (`sshtunnel`/`DSSKey`).
  The most likely candidate to end in `outcome: blocked`, which is itself worth observing.
- **C4** — the largest diff of the eight, up to 23 source files, against a CI signal that cannot
  reach the vulnerability.

All three are the most likely to consume their ACU budget in the test loop ([B11](./blockers.md)).

### Risk to the set as a whole

Every candidate names a test host that the narrowed CI actually runs, so none of them can fail for
the structural reason that killed the alternatives. The residual risks are per-candidate and stated
in each section. The largest single risk is **C6**, where the missing `assert_num_queries` helper
means the regression test is a small piece of infrastructure work rather than an assertion — worth
watching in the first run, and the most likely candidate to consume its ACU budget in the test loop
([B11](./blockers.md)).
