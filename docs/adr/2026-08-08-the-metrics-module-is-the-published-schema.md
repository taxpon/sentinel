---
title: The analytics API serves the metrics module's own schema rather than restating it
status: accepted
date: 2026-08-08
type: architecture
areas: [api, analytics]
tasks: [T25]
files: [src/sentinel/api/analytics.py]
specs: [docs/07-observability.md]
supersedes:
---

# The analytics API serves the metrics module's own schema rather than restating it

## Context

The JSON body of `GET /api/analytics/summary` is fixed in [07](../07-observability.md) and was fixed
before either side of the endpoint existed. Both sides were then built against that document
independently: `sentinel.analytics.metrics` computes the figures and declares the schema as a tree
of `TypedDict`s ending in `SummaryJson`, and `dashboard/src/api.ts` declares the same tree as
TypeScript interfaces, with nine panels indexing into it and `parseSummary` refusing a response
missing any of ten top-level keys.

That leaves the API layer joining two transcriptions that already agree, and FastAPI's idiom is to
add a third: Pydantic models in the router, declared as `response_model`. A third statement of a
schema nobody is free to change is a third place for it to drift, and the drift is invisible on this
side of the wire — a renamed key still serialises, still returns `200`, and fails in a browser this
suite cannot see.

The window parameter has the same shape of problem in miniature. `parse_window` already decides what
a well-formed window is, and raises `ValueError` — and only `ValueError`, including for the counts
too large to be a `timedelta` at all — so that a malformed one can be answered rather than crashed
on. Re-expressing "days or hours, at most 365" as a `Query` pattern in the router would be a second
definition of the same rule, and the two would answer differently the first time either moved.

## Decision

The API layer states no schema and no validation of its own.

`metrics.SummaryJson` is handed to FastAPI directly as the handler's return annotation, so it is the
response model. The two supporting endpoints have no schema in [07](../07-observability.md) to
inherit, so `RemediationRowJson` and `RemediationEventJson` are declared in the router — but as the
`remediation` and `remediation_event` columns of [03](../03-data-model.md), which is where
`dashboard/src/api.ts` says it took its own field names from.

`parse_window` remains the only definition of a valid window. The router calls it in a dependency
and translates the `ValueError` into `400` carrying the message it raised, so the reason a window
was rejected — malformed, empty, or past the ceiling, with the ceiling named — reaches the client
from the module that owns the rule.

## Alternatives considered

| Option | Why not |
|---|---|
| Pydantic models in the router, the FastAPI idiom | A third transcription of a schema fixed in the spec, and the one furthest from the code that fills it in. Its drift is silent: the wrong shape still returns `200` |
| `response_model=None`, return the dictionary | Nothing then checks that what leaves the process is the published schema. A dropped key reaches nine panels that index into it, and the first report is a blank dashboard |
| Validate `window` with a `Query(pattern=…)` | FastAPI answers `422` and a message about a regex, not about days and hours, and the 365-day ceiling cannot be expressed as a pattern at all. Two definitions of one rule, one of which is unable to state the interesting half |
| Answer a malformed window with `422` for consistency with FastAPI | `422` is what FastAPI produces when a parameter fails its *declared* type, and `window` is declared as a string. The constraint is the metrics module's, so the status is chosen here rather than inherited |

## Consequences

A summary response missing a key, or carrying one whose type moved, is a `500` from this process
rather than a `200` the dashboard has to detect — which is the failure being bought, deliberately:
the endpoint is only useful if its shape is right, and the nine panels have no way to recover from a
shape that is not. `pydantic` also coerces on the way out, so the `Decimal` that `numeric(10,3)`
returns leaves as a JSON number without the router converting it.

The test suite carries the other half of the contract, because a `TypedDict` cannot know what
TypeScript was written against: `tests/test_analytics_api.py` reads `SUMMARY_KEYS` out of
`dashboard/src/api.ts` and compares the response's full nesting and types against a transcription of
`dashboard/src/fixtures/summary.ts`.

**What would tell us this was wrong:** a second consumer of the summary wanting a different
representation — a CSV export, a paginated variant — at which point the endpoint would be serving
two shapes and the metrics module's `TypedDict` would no longer be one of them.
