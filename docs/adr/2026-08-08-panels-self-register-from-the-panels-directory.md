---
title: Panels self-register from src/panels and receive the whole request state
status: accepted
date: 2026-08-08
type: architecture
areas: [dashboard]
tasks: [T30, T31, T32, T33]
files: [dashboard/src/App.tsx, dashboard/src/api.ts]
specs: [docs/07-observability.md]
supersedes:
---

# Panels self-register from src/panels and receive the whole request state

## Context

T31, T32 and T33 add nine panels to the shell that T30 builds. They run in parallel, in separate
worktrees, and each owns only its own files
([2026-08-07-parallel-sessions-via-worktrees](./2026-08-07-parallel-sessions-via-worktrees.md)).
`App.tsx` belongs to T30, so the obvious arrangement — the shell imports each panel and places it —
would need three tasks to edit one file they do not own, in three branches, at the same time.

The shell polls once for all of them: nine panels each fetching `/api/analytics/summary` would be
nine requests every five seconds for one payload. So the data reaches panels as props, and the
question is what those props are. Every panel has a loading case, an error case and an empty-window
case that it must render itself — `.claude/rules/ui-testing.md` requires a test for each.

## Decision

`App.tsx` discovers panels with `import.meta.glob(['./panels/*.tsx', '!./panels/*.test.tsx'])`. A
panel is a module that default-exports a component and exports `slot`, `title` and an optional
`order`; the contract is the `PanelModule` type in `src/api.ts`. Adding a panel means adding a file.
No task edits a registry, and no two tasks touch the same file.

Panels are placed in one of three slots — `kpi`, `chart`, `table` — matching the layout regions the
spec fixes, and are ordered within a slot by `order`, ties broken by module path so the result does
not depend on filesystem order.

Every panel receives the whole `SummaryState` — `status`, `data`, `error` and `receivedAt` — not
just the payload. When a poll fails the shell keeps the previous payload and reports
`status: 'error'`, so a single failed request annotates a working dashboard rather than blanking it.
Panels decide what to draw with `showData(state)` instead of branching on `status`, so that all nine
inherit one answer to "there is an error, and also usable data".

Two guards come with the arrangement, because neither is free:

- **The shape of a discovered module is checked at runtime**, by `isPanelModule` in the shell. A
  module that fails is named on screen rather than vanishing.
- **Each panel renders inside an error boundary.** The shell mounts components written by three
  tasks it cannot see, so a render that throws costs that panel and nothing else.

`useRemediations` runs the live table's `/api/remediations` rows on the same polling primitive, so
the second endpoint the spec names does not become a second design.

`App` also accepts the registry and the fetcher as props. That is how the component tests drive it,
and it is the same seam `main.tsx` leaves at its defaults.

## Alternatives considered

| Option | Why not |
|---|---|
| The shell imports each panel by name | Three parallel tasks editing `App.tsx`, which none of them owns. The exact collision worktrees exist to prevent |
| A registry module the panels push into | Same collision, moved to a smaller file, plus an import-order dependency for the side effect to run |
| Each panel fetches its own data | Nine requests per poll for one payload, nine unsynchronised freshness values, and nothing left for the freshness indicator to describe |
| Pass the payload alone, or `null` | Loses the difference between "still loading" and "the API is down but this is what it last said" — the second is exactly what a leader looking at a stale number needs to be told |

## Consequences

The three panel tasks are genuinely independent: each adds files under `dashboard/src/panels/` and
nothing else.

The cost is that **the compiler does not enforce the contract**. `import.meta.glob<PanelModule>` is
an assertion about what the glob returns; nothing connects `PanelModule` to any file in
`src/panels/`. A module that exports no `slot`, or an unannotated `slot = 'charts'`, passes
`tsc --noEmit` and then simply does not appear. That is why the check is a runtime one, and why a
rejected module is announced rather than dropped — the failure it has to survive is a task adding a
file that nobody else can see, and a panel silently missing from a dashboard is the one failure
mode this design could otherwise hide.

**What would tell us this was wrong:** a panel arriving in review that the shell rejected, and the
author not having noticed. The on-screen notice is aimed at exactly that, and it is checked by a
test that walks the real registry; if it still happens, the answer is a build-time check over
`src/panels/`, not a return to a hand-maintained registry.
