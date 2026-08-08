---
title: The timeline panel selects its own remediation
status: accepted
date: 2026-08-08
type: architecture
areas: [dashboard]
tasks: [T33]
files:
  - dashboard/src/panels/Timeline.tsx
specs:
  - docs/07-observability.md
supersedes:
---

# The timeline panel selects its own remediation

## Context

`GET /api/remediations/{id}` returns the event log for *one* remediation
([07](../07-observability.md)), so the timeline panel needs an id before it can render anything.

The obvious source of that id is the live table sitting next to it: click a row, see its history.
That requires state shared between two panels, and there is nowhere to put it. Panels receive
`SummaryState` and nothing else, the shell owns no selection, and
[panels-self-register-from-the-panels-directory](./2026-08-08-panels-self-register-from-the-panels-directory.md)
makes the panel contract deliberately one-way — a panel is handed the request state and returns a
tree. `App.tsx` belongs to T30 and is not editable from this task.

## Decision

The timeline panel chooses for itself. It polls `/api/remediations` through `useRemediations` — the
same hook and the same guarded loop the live table uses — and renders a `select` over the result.

The default is the first row in the live table's own order: the most recently labelled remediation
still in flight, which is what a demo is looking at. An explicit choice is remembered until it falls
out of the window, at which point the default takes over rather than the panel showing nothing.

Switching selection clears the previous events before the first response for the new one arrives, so
one remediation's history is never displayed under another one's heading.

The panel also polls `/api/remediations/{id}` on the shared interval, with the same one-request-at-a-
time guard `api.ts` uses. That guard is reimplemented locally: `api.ts` keeps `usePolledResource`
private and exposes the two hooks it already needed, and it belongs to T30.

## Alternatives considered

| Option | Why not |
|---|---|
| Click a live-table row | The arrangement a reader will expect, and the one this cannot have: it needs selection state above both panels, in a file this task does not own, added by three parallel tasks at once |
| Always show the newest remediation, with no control | Fine for the demo's first minute and useless afterwards — the interesting history is usually the one that went wrong, which by then is not the newest |
| Take the id from the URL hash | A second, invisible source of truth for the selection, and routing the SPA otherwise does not have |
| Merge the timeline into the live table as an expandable row | One panel instead of two, but it makes the table's own layout conditional on a selection, and the spec lists the live table as its own panel with its own job |

## Consequences

The timeline works standalone, which is also what makes it testable: the selection is a `select`
element with a label, so a component test can switch remediations and assert what was fetched.

Two panels now poll `/api/remediations`, so the dashboard makes two requests to it every five seconds
instead of one. That is the cost of the panel independence the panels ADR bought; it is two requests
for a list of tens of rows, not the nine-for-one duplication that ADR was avoiding.

The reader's likely first instinct — clicking a table row — does nothing. The select is directly
above the timeline and lists every remediation, so the control is visible, but the gesture is missing.

**What would tell us this was wrong:** anyone trying to click a live-table row during the demo, or a
third panel needing the same selection. Either means the shell should own a selected remediation and
pass it down, which is a change to `App.tsx` and the `PanelProps` contract — cheap to make once the
parallel wave has landed, and deliberately not made from inside it.
