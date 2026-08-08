---
title: The Devin client composes the create-session body itself
status: accepted
date: 2026-08-08
type: architecture
areas: [devin]
tasks: [T11, T23]
files: [src/sentinel/devin/client.py, src/sentinel/devin/schemas.py]
specs: [docs/05-devin-integration.md]
supersedes:
---

# The Devin client composes the create-session body itself

## Context

Ten fields go into `POST /v3/organizations/{org_id}/sessions`, and every one of them is derived:
the prompt from a template, the title from the issue, the tags from the registered vocabulary, the
playbook id from the issue class and `DEVIN_PLAYBOOK_IDS`, the ACU ceiling from the playbook, the
schema and `structured_output_required` from constants, `resumable` always true.

Two of them fail in ways that are invisible at the time. A tag outside the vocabulary the
organisation registered is a `422` at creation (B7). A session created without `resumable` or
without the schema is created successfully and fails much later — at the first review-fix cycle, or
when the report cannot be parsed.

The worker (T23) is what has the issue; `playbooks.py` (T15) is what knows how each field is built.
The question was where the two meet.

## Decision

`create_session` takes the facts of an issue — number, title, body, class, delivery id — and builds
the body from `playbooks.py` and the configuration. It does not accept a request body, and no
caller assembles one. Tags go through `session_tags` and `validate_tag`, so an unregistered tag
raises before anything is sent; `tag_session` validates for the same reason.

The body itself is a `CreateSessionRequest` model whose field set is the spec's table, so an extra
or missing field is a type error rather than a discrepancy someone finds in the dashboard.

## Alternatives considered

| Option | Why not |
|---|---|
| The worker builds a `CreateSessionRequest`; the client posts it | The transport layer stays pure, but the guarantee moves to the caller — and the guarantee is the point. A second caller (T40's probe, a backfill) would repeat the composition and could repeat it differently |
| The client takes a plain `dict` body | Gives up both the field set and the tag check, for nothing in return |
| Compose in a third module between the worker and the client | A module whose only job is to call two other modules in order, when there is exactly one composition and one caller |

## Consequences

There is one place where a session's request body is built, and it is the same place the tests
assert against the spec's table field by field. The client is not a thin transport wrapper — it
imports `playbooks.py` and reads configuration — which is the cost: a caller that genuinely needs a
different prompt or a different ACU ceiling has to add a parameter rather than build its own body.

**What would tell us this was wrong:** a second kind of session with a materially different body —
the scheduled sweep is already separate — or `create_session` growing enough overrides that callers
are effectively passing a body again.
