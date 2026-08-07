---
title: Use the Devin v3 API exclusively
status: accepted
date: 2026-08-07
type: architecture
areas: [devin]
tasks: [T11]
files: [src/sentinel/devin/client.py]
specs: [docs/05-devin-integration.md]
supersedes:
---

# Use the Devin v3 API exclusively

## Context

Devin exposes v1, v2 and v3 session APIs. v1 and v2 remain reachable and much published example code
still uses them. v3 is organisation-scoped and is the only version exposing the features this design
depends on: session tags, structured output schemas, `max_acu_limit`, playbook binding and
schedules.

## Decision

Every call goes to a `/v3/...` path. No v1 or v2 endpoint appears anywhere in the codebase, and this
is asserted by a test over the client's route table.

## Alternatives considered

| Option | Why not |
|---|---|
| v1 for simple session creation, v3 for the rest | Two request shapes, two response shapes, and sessions created through v1 would not carry the tags the audit trail depends on |
| Wrap both behind an abstraction | Abstracting over an API we have decided not to use is pure cost |

## Consequences

The client is uniform and the whole feature set is available. Some endpoints are enterprise-scoped
and may be unreachable with organisation-level credentials; those have documented fallbacks rather
than a downgrade to older versions.

**What would tell us this was wrong:** a needed capability existing only on an older version, which
has not been observed.
