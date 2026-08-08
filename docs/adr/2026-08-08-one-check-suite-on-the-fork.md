---
title: Disable the inherited Superset workflows so a fork head SHA yields one conclusion
status: accepted
date: 2026-08-08
type: process
areas: [remediation, github]
tasks: [T42, T41]
files: []
specs: [docs/08-testing.md, docs/09-operations.md]
supersedes:
---

# Disable the inherited Superset workflows so a fork head SHA yields one conclusion

## Context

Adding a fast workflow to the fork does not, on its own, make the review-fix loop fast. The 49
workflows `taxpon/superset` inherited from `apache/superset` also trigger on `pull_request`, and
once they have run once they are registered and will run on every remediation pull request
([B2](../blockers.md#b2)).

Sentinel advances on `check_suite` events ([06](../06-event-pipeline.md)). How GitHub groups a
SHA's Actions check runs into check suites decides what those events mean, and it has not been
observed on this fork — nothing has ever run there. Both readings are damaging, in opposite ways:

- **One suite per head SHA.** Its conclusion is gated by the slowest inherited workflow —
  `superset-e2e`, `superset-playwright` and the integration matrix are tens of minutes each. The
  scoped workflow finishes early and changes nothing.
- **One suite per workflow run.** Sentinel receives roughly 49 `check_suite.completed` events per
  SHA. The first trivially-passing one transitions the remediation to `CI_PASSED` before the real
  tests finish — a false green, and worse than a slow one.

## Decision

Leave exactly one workflow active on the fork. After the throwaway pull request that registers
workflows, disable every workflow except `devin-autofix-ci.yml` (`gh workflow disable`); if the
inherited ones never register, delete them from `.github/workflows/` on the fork's `master`
instead. Re-enable a specific heavier workflow, on the specific pull request that needs it, when a
remediation touches an area it covers ([08](../08-testing.md#ci-on-the-fork)).

This is deliberately independent of which grouping GitHub uses — the remedy is the same either way,
so it is not a prerequisite to settle it. The activation pull request records the answer via
`GET /repos/taxpon/superset/commits/{sha}/check-suites`.

## Alternatives considered

| Option | Why not |
|---|---|
| Leave the inherited workflows enabled | Either caps loop latency at the slowest of them, or floods Sentinel with conclusions it cannot distinguish. Defeats the purpose of [2026-08-07-scoped-ci-on-the-fork](./2026-08-07-scoped-ci-on-the-fork.md) |
| Match on check-*run* events and filter by name instead of `check_suite` | More precise, but replaces a documented, already-specified event mapping with per-run bookkeeping, and still leaves 49 workflows consuming runner minutes on every push |
| Have Sentinel ignore conclusions from other workflows by app or name | Does not help under the one-suite-per-SHA reading, where there is only one conclusion and it is the aggregate |

## Consequences

A head SHA on the fork produces one conclusion, and it is the scoped one, so loop latency is what
this workflow costs and nothing else. Runner minutes drop accordingly.

The cost is that the fork's `master` no longer gets any of upstream's CI, and that heavier coverage
becomes an explicit manual step before merge instead of a default. Coupled with the narrowing this
already accepts, the fork's CI is entirely opt-in above the scoped signal — which is why the
narrowing is stated wherever results are reported rather than presented as validation.

**What would tell us this was wrong:** needing to re-enable a heavier workflow by hand on most
remediations rather than on a few. That would mean the scoped signal is too narrow for the issue
classes actually being fixed, and the right response is to widen the scope mapping, not to switch
the inherited workflows back on wholesale.
