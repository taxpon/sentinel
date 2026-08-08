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

Every panel receives the whole `SummaryState` — `status`, `data` and `error` — not just the payload.
When a poll fails the shell keeps the previous payload and reports `status: 'error'`, so a single
failed request annotates a working dashboard rather than blanking it.

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
nothing else. The cost is one Vite-specific feature in the shell — the glob is resolved at build
time, so a panel file that exports the wrong shape fails to compile rather than failing to appear,
which is the right way round.

**What would tell us this was wrong:** a panel that needs data the summary endpoint does not carry —
the live table's `/api/remediations` rows are already a second source. If more than one panel needs
its own request, the shell should own that fetch too and widen the props, not push fetching back
into the panels.
