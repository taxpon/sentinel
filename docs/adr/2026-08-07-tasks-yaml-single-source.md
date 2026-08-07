---
title: Keep the task graph in one machine-readable file
status: accepted
date: 2026-08-07
type: process
areas: [ops]
tasks: []
files: [docs/tasks.yaml]
specs: [docs/implementation-plan.md]
supersedes:
---

# Keep the task graph in one machine-readable file

## Context

The same breakdown is needed in three places: GitHub issues that sessions claim, the session-start
hook that reports which tasks are ready, and a document a human can read. Maintaining three copies
by hand guarantees they diverge, and a stale dependency list silently causes work to start before
its prerequisites are merged.

## Decision

`docs/tasks.yaml` holds the graph — id, title, wave, area, spec link, owned files, dependencies,
related ADRs. Issue seeding, the hook and the plan document all derive from it. The seeding script
topologically sorts the graph, so a dependency cycle fails loudly at seed time.

## Alternatives considered

| Option | Why not |
|---|---|
| GitHub issues as the source of truth | Dependencies would live in prose; nothing could compute the ready set, and the graph could not be validated |
| A project board | Good for status, but it cannot express file ownership or dependency edges in a form a script can read |
| Markdown tables as the source | Readable but not reliably parseable, and the ready-set computation would be guesswork |

## Consequences

Ready-task computation and cycle detection are mechanical, and the human-readable plan cannot
silently disagree with what the tooling does. Adding a task means editing the YAML and re-running
the seeder rather than typing an issue, which is slightly more ceremony for considerably more
consistency. Task *status* deliberately stays on the issues, not in the file, because status changes
constantly and a checked-in checklist would be stale immediately.

**What would tell us this was wrong:** the file drifting out of sync with the issues, which would
mean the seeder is not being used.
