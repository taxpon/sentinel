---
title: Enterprise degradation is a returned value, not an exception
status: accepted
date: 2026-08-08
type: architecture
areas: [devin]
tasks: [T11]
files: [src/sentinel/devin/client.py, src/sentinel/devin/schemas.py]
specs: [docs/05-devin-integration.md]
supersedes:
---

# Enterprise degradation is a returned value, not an exception

## Context

Three of the capabilities in `docs/05-devin-integration.md#degradation` may be unreachable with the
credentials Sentinel will hold: session metrics and playbook CRUD are enterprise-scoped (B5, B6) and
consumption may not be exposed to the organisation. Each has a defined fallback, and the dashboard
labels any figure served by a fallback so that a reader can tell Devin's numbers from Sentinel's own.

Whether the credentials carry the scope is unknown until B8 is resolved, so the answer arrives at
runtime, on a path — the analytics endpoint and the budget guard — where the alternative to Devin's
figure is not an error page but a figure computed from the `remediation` table.

The rest of the client's failures are genuine failures: a `422` from an unregistered tag, a `401`
from a bad token, a `503`. Those raise, and the worker records the response body in
`remediation_event.detail`.

## Decision

The two degradable endpoints return `Available[T] | Unavailable` rather than raising. `403` and
`404` become `Unavailable`, carrying the capability, the reason, the status and the fallback text
of the spec's table. A missing `DEVIN_ENTERPRISE_ID` returns `Unavailable` without a request at
all. Every other status raises `DevinAPIError` as elsewhere — `401` deliberately included.

## Alternatives considered

| Option | Why not |
|---|---|
| Raise a `DevinUnavailable` exception the caller catches | A fallback that only runs if someone remembered a `try` is a fallback that will be missing from one of the three call sites. The type system cannot ask for the branch, and the failure mode is a dashboard panel that errors instead of degrading |
| Return `None` for an unavailable capability | Indistinguishable from "Devin has no data yet", and it carries neither the reason an operator needs nor the fallback the dashboard labels with |
| Probe the scope once at startup and configure the fallback | The probe can only be run where the credentials are, which is not where the tests run, and a permission granted or revoked later would not be noticed until a restart |
| Treat `401` as a degradation too | A rejected token would silently become "derived figures", hiding the one fault that stops everything else working |

## Consequences

The caller cannot use the value without deciding what to do when it is absent, and the reason and
the fallback text travel with it, so the log line, the operator report from `make bootstrap-devin`
and the dashboard's label all say the same thing. The cost is a second shape at two call sites and
a client that returns two different kinds of thing depending on the endpoint — justified only
because the spec defines a fallback for exactly these and for nothing else.

**What would tell us this was wrong:** credentials that turn out to carry enterprise scope
permanently, making the fallback path dead code; or a third failure mode — a `503` on the metrics
endpoint, say — that callers also want to degrade rather than fail, which would mean the split is
drawn at the wrong place.
