---
title: Build the test schema by running the migrations, and test for drift
status: accepted
date: 2026-08-08
type: architecture
areas: [data, ops]
tasks: [T03, T04]
files: [alembic/env.py, src/sentinel/models.py, tests/conftest.py, tests/test_models.py]
specs: [docs/03-data-model.md, docs/08-testing.md]
supersedes:
---

# Build the test schema by running the migrations, and test for drift

## Context

The models and the migration are two descriptions of one schema, written by hand and by
autogenerate respectively. Nothing in SQLAlchemy or Alembic makes them agree. The usual failure is
silent: a column is added to a model, every test passes because the test schema was built from the
models, and the mismatch surfaces on the first deployment — or, worse, on the first query against a
production table that never got the column.

Tests need a schema in place before they can write a row, and there are two ways to get one:
`Base.metadata.create_all()`, or the migration.

## Decision

Tests build the schema with `alembic upgrade head` against an empty database, never with
`create_all()`. `alembic/env.py` accepts a caller's connection through `config.attributes`, so a
test runs the migration on the connection and event loop it already has.

One test then asserts that autogenerate finds no difference between the migrated database and
`Base.metadata`. Three things make that comparison mean what it appears to mean.

`Base.metadata` carries a naming convention, because otherwise Postgres names the constraints itself
and Alembic cannot reproduce those names. `compare_server_default` is turned on — in
`alembic/env.py`, and again in the test, which configures its own migration context and does not go
through `env.py` — because it is off by Alembic's default, and with it off a column whose server
default has drifted from its migration is not reported as a difference at all. `job.run_after
DEFAULT now()` is the difference between a job that is immediately claimable and one that is never
claimed. (`compare_type` has been on by default since Alembic 1.12; both places name it anyway, to
pin the comparison rather than inherit it.)

Third, two further tests assert that the comparison *can* fail, by comparing the migrated database
against a copy of the metadata with one column deliberately drifted. Whether the options are right
is then a test result rather than something a reader verifies by eye — which is how the missing
`compare_server_default` survived its first review.

## Alternatives considered

| Option | Why not |
|---|---|
| `create_all()` in the test fixture | The fastest and the most common, and it tests a schema no environment ever runs. A migration that fails to apply, or applies differently, is invisible until deployment |
| Trust review to keep the two in step | The diff that needs noticing is an *absent* file. Reviewers see the model change and read it as complete |
| Generate the models from the database | Inverts the source of truth, and the type annotations the rest of the code relies on are not recoverable from Postgres |
| `alembic check` in CI instead of a test | Equivalent coverage, but it needs its own database service and its own step, and it fails outside the suite that developers run before pushing |

## Consequences

The migration is exercised on every test run rather than once at deploy time, so "the migration does
not apply" is a red test rather than an incident, and a model changed without a migration cannot
reach `main`. Schema setup costs an `upgrade head` per test session instead of a `create_all`, which
is milliseconds at five tables.

It also fixes what `tests/conftest.py` (T04) must do: reset the schema and migrate it, rather than
call `create_all`.

**What would tell us this was wrong:** migration time growing until the suite is slow enough to
discourage running it — at which point the answer is squashing the migration history, not building
the schema a second way. Or persistent false positives from the drift comparison, which would mean
`compare_type` is reporting a dialect detail rather than a real difference.
