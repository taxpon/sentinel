---
title: Isolate tests by truncating between them, in the harness rather than in each test
status: accepted
date: 2026-08-08
type: architecture
areas: [ops, data]
tasks: [T04]
files: [tests/conftest.py, tests/factories.py]
specs: [docs/08-testing.md]
supersedes:
---

# Isolate tests by truncating between them, in the harness rather than in each test

## Context

`docs/08-testing.md` runs the orchestrator tests against a real Postgres from Compose, because the
queue claims jobs with `FOR UPDATE SKIP LOCKED` and SQLite cannot emulate it. The test matrix in
that document has hundreds of database tests ahead of it — the state machine, the queue, the
policies and the analytics are all table-driven over seeded rows — so whatever each of them pays to
get a clean database is paid hundreds of times.

Two constraints pull against each other:

- **The queue tests need two connections that can see each other's committed rows.** That is the
  whole content of `SKIP LOCKED`: one worker must meet a row another worker has locked and not yet
  committed.
- **`tests/test_models.py` drops the schema on purpose.** It downgrades to base and asserts the
  database is empty, so the schema cannot be built once per session and assumed to survive.

Three globals leak the same way the database does, and were leaking already: `structlog.configure()`
installs one pipeline for the interpreter, `prometheus_client.REGISTRY` is a module-level singleton,
and `get_settings()` caches one configuration for the process. T17 left its own reset in a local
fixture and asked for it to be made unconditional.

On this machine, 50 database tests take about 5 seconds when the harness truncates between them and
about 10 seconds when each re-runs `alembic upgrade head`.

## Decision

The `database` fixture truncates every table with `RESTART IDENTITY CASCADE` **before** each test,
after checking that the schema is there and re-migrating it when it is not. Rows are committed
normally; nothing is wrapped in a transaction the harness later rolls back.

Cleaning up is the harness's job and not each test's. The `structlog` reset and the `get_settings`
cache clear are an **autouse** fixture, so a test that logs without asking for `capture` still
cannot configure the next one, and metrics are built against a `CollectorRegistry` the test owns
rather than the process-global default.

## Alternatives considered

| Option | Why not |
|---|---|
| Drop the schema and re-run the migrations per test (what `tests/test_models.py` does) | Correct, and about three times the cost — the difference is ~95 ms on every database test, which is minutes across the suite the test matrix describes. It stays available as `recreate_schema()`, and is what a schema test should use. |
| Wrap each test in one transaction and roll it back | The fastest option, and it breaks the tests the real Postgres exists for: a second connection cannot see rows the first has not committed, so every `SKIP LOCKED` test would pass with the locking clause deleted. |
| Migrate once per session and truncate ever after | One `DROP SCHEMA` in another test file leaves every later test failing on a missing table. The check is one query and makes the harness self-healing instead. |
| Leave the resets in each test file, as T03 and T17 did | The failure mode is a green test, not a red one: the test that leaks is not the test that fails, and the one that fails looks unrelated. An autouse fixture removes the chance to forget. |
| Truncate on the way out rather than on the way in | A failing test then deletes the rows that would explain it, and a test that reached the database by some other route starts dirty. |

## Consequences

A database test costs about 55 ms, most of it opening a connection rather than the `TRUNCATE`.
Sequences restart, so the first row of every test has id 1 and a test may assert on it. Tests still
`commit()` where the behaviour under test involves committing, which keeps the queue tests honest.

What would tell us this was wrong:

- a test that passes alone and fails in the suite, or the reverse — the truncation missed something
  the schema grew, most likely a table added without `Base.metadata` knowing about it;
- `TRUNCATE` blocking on another connection's lock, which would mean a test is leaving a connection
  open past its own end;
- the per-test cost climbing back towards the re-migration figure, which would mean the schema check
  is failing and the harness is rebuilding every time.
