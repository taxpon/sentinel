---
title: A script loads the configuration group it reads, not the whole of Settings
status: accepted
date: 2026-08-10
type: architecture
areas: [ops]
tasks: [T02, T40, T41, T51]
files: [src/sentinel/config.py, scripts/bootstrap_github.py, scripts/file_remediation_issues.py, scripts/bootstrap_devin.py]
specs: [docs/09-operations.md]
supersedes:
---

# A script loads the configuration group it reads, not the whole of Settings

## Context

`Settings` was one model holding every variable in the table in
[`09-operations.md#configuration`](../09-operations.md#configuration), and `get_settings()` was the
only way to read it. Six of those variables are required, so every caller was required to have all
six — including the scripts, which each use a handful.

What that cost, filing the eight remediation issues on the fork:

```bash
GITHUB_TOKEN=$(gh auth token) DEVIN_API_TOKEN=cog_placeholder DEVIN_ORG_ID=org-placeholder \
DEVIN_PLAYBOOK_IDS='{"security":"p"}' GITHUB_WEBHOOK_SECRET=whsec_placeholder \
DATABASE_URL=postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel \
uv run scripts/file_remediation_issues.py --apply
```

Four of the six are placeholders invented to get past a validator, for a script that opens issues
over the GitHub REST API and touches nothing else. The dry run — the mode that exists so an
operator can see what would happen *before* anything is set up — was blocked identically, which is
the part that makes this more than an inconvenience: the safe way to run the script was as hard to
reach as the unsafe one, and the placeholders that unblock it are indistinguishable from a real
misconfiguration to everything downstream.

`alembic/env.py` already solved its own version of this by reading `DATABASE_URL` from the
environment and borrowing only `normalise_database_url`, on the grounds that "applying a migration
needs a database, not an API token". That is the right instinct and the wrong mechanism to repeat:
it re-implements the read, and it works there only because a migration needs exactly one variable
and no defaults.

## Decision

The variables are declared in groups, each a `BaseSettings` subclass of its own, and `Settings` is
the composition of all of them:

- `TargetSettings` — `TARGET_REPO`, `TARGET_BASE_BRANCH`, `AUTOFIX_LABEL`;
- `GitHubSettings(TargetSettings)` — `GITHUB_TOKEN`;
- `WebhookSettings` — `GITHUB_WEBHOOK_SECRET`;
- `DevinSettings(TargetSettings)` — the six `DEVIN_*` variables;
- `DatabaseSettings`, `PolicySettings`, `ReportingSettings`.

`load_config(model)` reads any one of them, or a composition a caller declares:

```python
class WritingConfiguration(Configuration, WebhookSettings):
    """A run that writes also gives the hook a secret."""
```

Each script declares what it reads and loads that. `scripts/bootstrap_github.py` declares two, and
chooses between them on the flag: a `--dry-run` sends nothing, so it needs no secret to send, and a
run that writes gets the ordinary required-variable error if `GITHUB_WEBHOOK_SECRET` is missing.

`get_settings()` is unchanged in every observable way — still `@cache`d, still returning `Settings`,
still raising `ConfigurationError`. It is now `load_config(Settings)` behind the cache. **Services
load `Settings` and nothing narrower**: `api`, `worker` and `poller` fail at startup on any missing
variable exactly as before, which is the property that keeps a half-configured process from failing
later, mid-remediation, on an issue somebody has already labelled.

Narrowing changes which variables are read and nothing else. Every group inherits one
`SettingsConfigDict`, so `SecretStr`, `env_ignore_empty`, whitespace stripping, the frozen fields,
the `.env` precedence and the error that names variables without echoing values are the same in a
script as in a service — they are properties of the model, and it is the same model. A test asserts
that each group requires exactly those of its own variables the documented table marks required, so
a group cannot quietly relax one.

`load_config` is deliberately **not** cached. `get_settings()` caches because three long-running
processes share one object and must validate once; a script reads its configuration once in `main`
and hands it down, so a cache there would buy nothing and would add a second thing for tests to
remember to clear.

Two signatures in `src/` widened to the group they actually read — `DevinClient(settings)` takes
`DevinSettings`, `configure_logging` takes `ReportingSettings`, and `secret_values` takes any group.
A service still passes its whole `Settings`, which is all of them.

## Consequences

A dry run needs what it takes to read:

```bash
GITHUB_TOKEN=$(gh auth token) uv run scripts/file_remediation_issues.py
GITHUB_TOKEN=$(gh auth token) uv run scripts/bootstrap_github.py --dry-run
```

A script also cannot leak a credential it never loaded: `secret_values()` is read off the model, so
the redaction a script installs covers exactly the credentials it holds — fewer values scrubbed,
and no value present to scrub.

The cost is that the required-ness of a variable is now stated in two places for a reader to hold
together: the table says whether it is required *at all*, and the group says of whom. A script that
starts reading a new variable must widen its declaration, and will fail loudly at the attribute if
it does not — an `AttributeError` naming the field, at the line that reads it.

## Alternatives rejected

**Leave it, and document the placeholders.** What the owner ran is a working instruction, and it is
also a habit of pasting invented credentials into a shell to get past a validator. On the day one
of those placeholders is a real token from another deployment, nothing in the system distinguishes
it from the configuration it is standing in for.

**A flag on the loader — `get_settings(require=...)`.** One model, with the required-ness of its
fields decided per call. It cannot be typed: the returned object would carry `database_url` whatever
was asked for, so nothing would stop a script reading one that was never validated. The groups make
the absent variable an `AttributeError` at the read rather than a `None` at the use.

**Build a model per call from a set of field names** — `load_config("github_token", ...)`. Exactly
as precise as the groups and unusable from a type checker's point of view: the result of
`create_model` has no static attributes, so every `settings.github_token` in three scripts becomes
unchecked. It also puts the composition at the call site, where nothing names *why* those fields go
together.

**Nested models with a prefix** (`GITHUB__TOKEN`, `settings.github.token`), which is the other
pydantic-settings idiom. It renames every variable in the documented table and in `.env.example`,
for a deployment that already has secrets set on Fly under the current names. The variables are
flat because the table is flat.
