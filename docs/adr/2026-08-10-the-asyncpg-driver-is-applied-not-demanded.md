---
title: Apply the asyncpg driver to DATABASE_URL rather than demanding it
status: accepted
date: 2026-08-10
type: architecture
areas: [ops]
tasks: [T02]
files: [src/sentinel/config.py, alembic/env.py]
specs: [docs/09-operations.md]
supersedes:
---

# Apply the asyncpg driver to DATABASE_URL rather than demanding it

## Context

`Settings.database_url` was validated with `startswith("postgresql+asyncpg://")`. The check exists
for a real failure: SQLAlchemy resolves the driver from the URL, and a `postgresql://` URL loads
psycopg2, which is not installed. Without the check that surfaces as `No module named 'psycopg2'`
raised from inside the engine on the first query, not at startup — verified in this repository:

| URL | `create_async_engine` |
|---|---|
| `postgres://…` | `NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres` |
| `postgresql://…` | `ModuleNotFoundError: No module named 'psycopg2'` |
| `postgresql+asyncpg://…` | engine, driver `asyncpg` |

Naming a driver in a connection URL is SQLAlchemy's convention. Postgres has no such notion, and
neither does anything that hands out a connection string: managed providers issue `postgres://` or
`postgresql://`. `fly postgres attach` does not print a URL for a human to adapt — it writes the
`DATABASE_URL` secret into the app itself. With the check as it was, the documented way to attach a
database left all three processes crash-looping on the scheme, and the operator's only clue was an
error about a prefix they never chose.

`alembic/env.py` reads `DATABASE_URL` straight from the environment rather than through `Settings`,
deliberately, so it was not covered by the check at all. On Fly the migration is the `release_command`
and runs before anything else, so it is the first thing an unadapted URL breaks.

## Decision

`normalise_database_url` in `sentinel.config` returns the URL with `postgresql+asyncpg://`
substituted for a bare `postgres://` or `postgresql://` scheme, returns a URL that already names
asyncpg unchanged, and rejects anything else. The `database_url` validator applies it, and
`alembic/env.py` imports the function — the one thing it borrows from the module it otherwise avoids
— so the migration and the processes resolve the same URL to the same driver.

The rejection is unchanged in substance: a URL naming a driver has chosen one, and the wrong choice
still fails at startup with the variable named. Only the schemes that choose *nothing* are filled
in. The error message quotes the accepted forms and never the URL, which carries the password.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep demanding the exact scheme | Makes `fly postgres attach`, the documented way to attach a database, produce a broken deployment. The knowledge needed to fix it is a rewrite rule that belongs in the code, not in an operator's head |
| Document the rewrite in the runbook instead | A step performed by hand every time a database is created or rotated, whose failure mode is a crash loop reported as a scheme error. Runbook steps that exist only to compensate for a check are the check being wrong |
| Accept any scheme and let SQLAlchemy decide | Gives back exactly the deferred `psycopg2 is not installed` on the first query that the check was written to prevent |
| Normalise in `db.py` at engine creation | `alembic/env.py` does not go through `db.py`, so the release command would still fail; and `Settings` would then hold a URL that is not the one used |
| Parse with `sqlalchemy.engine.make_url` and `set(drivername=…)` | More machinery for a scheme swap, and `make_url` raises with the URL in the message — the one thing this field must never put in a log |

## Consequences

Attaching a Fly (or Neon, or RDS) database is one command with no follow-up edit, and the same URL
works for the release command and the three processes. `Settings.database_url` may now differ from
the environment variable it was read from; it is the driver prefix that differs and nothing else,
and the value is still a `SecretStr`, so nothing is rendered either way.

`alembic/env.py` now imports from `sentinel.config`. That is a pure function and imports no
settings, so the property that made the file avoid `Settings` — a migration must not require the
Devin and GitHub credentials — is intact.

**What would tell us this was wrong:** a URL that is normalised into something that connects but is
not what the operator meant — a provider inventing a `postgres://`-schemed URL that is not a plain
Postgres DSN, or one whose driver we are silently overriding. Both would show up as a connection
that succeeds against the wrong thing rather than an error, which is why anything naming a driver is
still rejected instead of rewritten.
