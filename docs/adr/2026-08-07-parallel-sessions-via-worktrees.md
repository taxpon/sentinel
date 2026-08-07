---
title: Isolate parallel sessions with git worktrees and exclusive file ownership
status: accepted
date: 2026-08-07
type: process
areas: [ops]
tasks: []
files: []
specs: [docs/implementation-plan.md]
supersedes:
---

# Isolate parallel sessions with git worktrees and exclusive file ownership

## Context

The work is split into small tasks so that several Claude Code sessions can implement them at once.
Two problems follow: sessions must not edit the same working tree, and they must not edit the same
files on different branches, or every merge becomes a conflict resolution exercise.

## Decision

One git worktree per task, branched as `task/T<id>-<slug>`, sharing a single `.git`. Every task
declares an exclusive `owns` list in `docs/tasks.yaml`, and a session must not modify files outside
it. The few genuinely shared files have named owners or append-only rules.

## Alternatives considered

| Option | Why not |
|---|---|
| One checkout, switching branches | Two sessions editing the same tree overwrite each other. Not viable |
| A full clone per session | Works, but duplicates dependencies and containers, and multiplies disk and setup cost for no benefit over worktrees |
| Shared ownership with conflict resolution at merge | Turns parallelism into merge work and puts the cost at the end, where it is most expensive |

## Consequences

Sessions are genuinely independent and conflicts are rare by construction. The task breakdown must
be drawn along file boundaries rather than purely by feature, which shaped the decomposition. Local
services must be namespaced per worktree — `COMPOSE_PROJECT_NAME` and a distinct Postgres port — or
test runs collide.

**What would tell us this was wrong:** repeated conflicts in files that are supposed to be
exclusively owned, meaning the ownership map is wrong rather than the mechanism.
