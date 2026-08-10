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

## Amendment, 2026-08-10: the query string needed the same treatment

The first real deployment failed anyway. `fly postgres attach` wrote a `DATABASE_URL` ending
`?sslmode=disable`, and `fly deploy`'s `release_command` died on it:

```
TypeError: connect() got an unexpected keyword argument 'sslmode'
```

Swapping the scheme was half the adaptation. The **query string** is written for the driver the
provider assumes too, and this decision carried it across untouched — there was even a test pinning
that it survived, written when `sslmode=require` looked like something to protect.

The mechanism, from SQLAlchemy's asyncpg dialect: `create_connect_args` does `opts.update(url.query)`
and returns the result as keyword arguments. The dialect interprets nothing — `ssl` does not appear
anywhere in the module — so a query parameter reaches `asyncpg.connect` under its own name, and the
libpq names are not asyncpg's names. Reaching the right driver is not the same as reaching it with
arguments it accepts.

So `normalise_database_url` now also rewrites the query string, in three classes:

| Class | Parameters | Why |
|---|---|---|
| Translated | `sslmode` → `ssl` | Not our interpretation. asyncpg's own DSN parser performs this exact rename — `if 'sslmode' in query: ssl = query.pop('sslmode')` — passing the value through unchanged, and then parses it against an enum of the libpq names (`disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`). SQLAlchemy never hands asyncpg a DSN to parse, only keyword arguments, which is why the rename has to happen in our code instead of asyncpg's |
| Carried across | `ssl`, `target_session_attrs`, `timeout`, `command_timeout`, `server_settings`, `passfile`, and the four the dialect's DBAPI shim pops itself (`async_fallback`, `async_creator_fn`, `prepared_statement_cache_size`, `prepared_statement_name_func`) | The stack already accepts them under those names |
| Rejected | `sslrootcert`, `sslcert`, `sslkey`, `sslcrl`, `options`, `application_name`, `connect_timeout`, and anything unrecognised | No equivalent a URL can express |

**Nothing is dropped**, which is a departure from the first instinct. The first instinct was that
`sslmode=disable` could be discarded, since "do not use TLS" and "say nothing about TLS" sound like
the same request. They are not, and the deployment proved it: with the parameter deleted by hand
the `TypeError` was replaced by a `ConnectionResetError` raised from `start_tls`. asyncpg given no
`ssl` argument does not decline TLS — it attempts it (`connect_utils.py:652-656`):

```python
if ssl is None:
    ssl = os.getenv('PGSSLMODE')
if ssl is None and have_tcp_addrs:
    ssl = 'prefer'
```

And `prefer` is not a safe silence. It falls back to plaintext only when the server *declines* SSL
at the protocol level, answering `N` to the SSLRequest. When the server answers `S` and the
handshake then fails, `connect_utils.py:1102-1122` retries on two authorization-shaped exceptions
and on nothing else, so a `ConnectionResetError` propagates and the connection is dead. Fly's
endpoint does the second thing; the Compose Postgres does the first, which is exactly why every
local run was green while the deploy was not.

So both directions are load-bearing, for different reasons:

- `sslmode=disable` **must** become an explicit `ssl=disable` — `SSLMode.disable` is the one value
  that sets `ssl = False` (`connect_utils.py:689-690`). Dropping it breaks a real deployment.
- `sslmode=require` **must** become an explicit `ssl=require`. Dropping that one would connect
  unencrypted to a database on the public internet that somebody asked to encrypt, and would
  succeed while doing it — the worse failure, for being invisible.

Since the rename is exact for all six values, translating costs nothing and decides neither
question.

The rejected class is the deliberate one. Those parameters say how strictly the server's certificate
is checked, or what the session starts as, and asyncpg exposes them only as an `ssl.SSLContext` or a
`server_settings` dict — objects, which a URL string cannot carry. Dropping them would connect
anyway, less verified or less configured than the operator asked for, with nothing said about it. A
deployment that stops with the parameter named costs the time it takes to read the message; one that
quietly stops verifying a certificate costs however long it takes somebody to notice. The error
names the parameter and never the URL, for the same reason the scheme error does not.

`ASYNCPG_CONNECT_PARAMETERS` in `config.py` is a transcription of `asyncpg.connect`'s signature, kept
there so the module still imports no driver. A transcription that is never compared to its source
goes stale in the dangerous direction — an asyncpg release adding a keyword would have us rejecting
a URL the operator was entitled to write — so a test asserts it equals
`inspect.signature(asyncpg.connect).parameters`, and a version bump fails CI rather than a deploy.

**What would tell us this was wrong:** an operator hitting the rejected class for a parameter they
genuinely need. `sslrootcert` is the likely one — a provider requiring `verify-ca` against a private
CA cannot express that in a URL at all under asyncpg, and the answer would be a `connect_args`
mechanism rather than a wider rewrite here.
