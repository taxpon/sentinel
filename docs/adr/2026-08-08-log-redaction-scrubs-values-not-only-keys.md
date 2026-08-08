---
title: Scrub credentials out of log values, not only out of fields named like credentials
status: accepted
date: 2026-08-08
type: architecture
areas: [ops]
tasks: [T17]
files: [src/sentinel/observability/logging.py]
specs: [docs/07-observability.md]
supersedes:
---

# Scrub credentials out of log values, not only out of fields named like credentials

## Context

"Tokens and webhook secrets are never logged, at any level" is stated in
[`07`](../07-observability.md) and repeated wherever a credential is handled. It is a property of
the output, not of any one call site, and the call sites are written by a dozen parallel sessions
and read once.

`Settings` holds every credential in a `SecretStr`, which masks a `repr` and a `str`. That covers
the field being passed to the logger as an object and nothing else. An interpolated
`.get_secret_value()` is an ordinary string by the time the logger sees it. So is a token read back
out of a Devin response, a DSN assembled at runtime, and the message of an exception raised by a
driver that was handed one — none of which came from `Settings` at all.

The values most likely to carry a credential are the ones nobody labelled: `detail`, `body`,
`request`, an ORM row logged whole, a traceback. Filtering on the *name* of a field cannot see any
of them.

## Decision

A redaction processor sits in the structlog chain, after exceptions are rendered to text and before
the JSON renderer, and rewrites every event dictionary — keys, values, and the message itself —
through three mechanisms:

1. **By value.** Every credential this process is configured with is replaced wherever its text
   occurs. The list is read off `Settings` by looking for `SecretStr` fields, so a credential added
   to the configuration later is covered without anyone editing this module.
2. **By shape.** Strings matching a Devin service-user token, a GitHub token, or a URL carrying
   `user:password@` are replaced whatever their source — including credentials this process was
   never configured with.
3. **By key.** A field whose name says `token`, `secret`, `password`, `api_key`, `authorization`,
   `credential`, `database_url` or `dsn` loses its value regardless of what it holds.

In the same pass every value is reduced to JSON-native data: anything else is rendered with `str()`
*here*, where the result is still scrubbed, rather than by the renderer's fallback, which this
module never sees.

## Alternatives considered

| Option | Why not |
|---|---|
| Rely on `SecretStr` alone | Covers the object and not the string; `.get_secret_value()` is one keystroke away, and a token that arrives from an API response was never a `SecretStr` |
| Redact by field name only | The dangerous fields are the unlabelled ones — `detail`, `body`, a traceback |
| A logger API that refuses to accept a `SecretStr` | Rejects the one shape that was already safe, and accepts every shape that is not |
| A lint rule against logging secrets | Cannot see a value that is only a credential at runtime, and stops at the boundary of our own source |
| Redact at the collector, outside the process | The credential has already been written to stdout, which is where the leak is |

## Consequences

The guarantee holds for code that knows nothing about it, which is the only way it can hold across
parallel sessions: a call site cannot leak a configured credential by accident, and reviewing a new
log line does not require thinking about redaction.

The costs are real and accepted. Every value on every line is walked and every string is scanned
once per configured credential — a handful of comparisons on a path that already serialises JSON. A
value with no JSON form is rendered by `str()` rather than by whatever the renderer would have
done. And a *short* configured secret — a four-character webhook secret in development — redacts
its occurrences in unrelated text as well; a mangled line is the safe direction to fail in.

**What would tell us this was wrong:** a credential appearing in stdout despite the processor,
which would mean a route out of the process that does not go through structlog; or operators
routinely reading around the redaction, which would mean it is eating fields they need.
