---
title: Hold credentials as SecretStr and rewrite configuration errors before raising them
status: accepted
date: 2026-08-08
type: architecture
areas: [ops]
tasks: [T02]
files: [src/sentinel/config.py]
specs: [docs/09-operations.md]
supersedes:
---

# Hold credentials as SecretStr and rewrite configuration errors before raising them

## Context

Five of the six required variables are credentials: `DEVIN_API_TOKEN`, `GITHUB_TOKEN`,
`GITHUB_WEBHOOK_SECRET`, and `DATABASE_URL`, whose DSN embeds the Postgres password.
[`docs/07-observability.md`](../07-observability.md) states that tokens and webhook secrets are
never logged, at any level, and the repository is intended to be made public
([`09-operations.md`](../09-operations.md#before-making-the-repository-public)).

Configuration is validated at startup, so the settings object and the errors it produces are exactly
what lands in the first lines of a container's log. `pydantic.ValidationError` repeats the input it
rejected — `input_value='cog_…'` — in its string form and in `errors()`. A malformed token, or a
`DATABASE_URL` with the wrong driver, therefore prints the credential in full to stderr and into
whatever collects those logs.

## Decision

Every credential field is a `SecretStr`, including `DATABASE_URL`; consumers call
`.get_secret_value()` at the point of use. `get_settings()` catches `ValidationError` and raises
`ConfigurationError` with a rendering built from each error's variable name and message only, never
its input, chained with `from None` so the original cannot resurface through `__cause__` or a
traceback.

## Alternatives considered

| Option | Why not |
|---|---|
| Let `ValidationError` propagate | It is the leak. It also reports `devin_api_token`, the field name, where the operator needs `DEVIN_API_TOKEN`, the variable they must edit |
| `SecretStr` alone, no wrapper | Masks the value in `repr` and dumps, but not in the validation error, which is the path a misconfigured deployment actually takes |
| Plain `str` plus a logging filter | Redaction then depends on every future call site choosing to log through the filter — the failure mode is silent and only visible after the fact |
| Plain `str` for `DATABASE_URL`, secret for the rest | The DSN carries a password; it is a credential regardless of what it is called |

## Consequences

A misconfigured process prints the variable names it needs and nothing else, which is what an
operator wants and what a public repository requires. The cost is a `.get_secret_value()` call
wherever a credential is used — the engine URL in `db.py`, the auth headers in the Devin and GitHub
clients — and a wrapper that must keep passing values out of the message as pydantic's error set
grows.

**What would tell us this was wrong:** an operator unable to diagnose a configuration failure
because the message withholds too much, or a credential found in a log despite this — which would
mean the leak is somewhere other than startup validation, and the fix belongs in the logging layer
instead.
