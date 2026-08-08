---
title: Draw every dashboard chart with Recharts
status: accepted
date: 2026-08-08
type: architecture
areas: [dashboard, analytics]
tasks: [T30, T31, T32, T33]
files: [dashboard/package.json, dashboard/src/api.ts]
specs: [docs/07-observability.md]
supersedes:
---

# Draw every dashboard chart with Recharts

## Context

Four dashboard tasks run in parallel. T30 builds the shell; T31, T32 and T33 each add panels to it
and start from whatever T30 installed. A charting library chosen now is effectively permanent —
changing it later means rewriting every panel in three tasks at once.

The panels named in [07](../07-observability.md) need a funnel, a stacked bar series by issue class,
a duration comparison, a cycle distribution and a cost line over time. No chart may exceed 240px in
height, and per-class colours are reserved for the stacked series.

Rule 2 in `CLAUDE.md` makes component tests mandatory for everything under `dashboard/`, and
[2026-08-08-no-browser-level-tests](./2026-08-08-no-browser-level-tests.md) leaves those component
tests as the only automated check on the panels. Whatever a chart renders in jsdom is therefore the
whole of what can be asserted about it.

## Decision

Recharts, installed by T30 in `dashboard/package.json` and inherited by T31-T33.

Charts are given explicit dimensions rather than `ResponsiveContainer` wherever a test needs to read
them: the height is fixed at 240px by the spec anyway, and a container that measures itself renders
nothing in jsdom. `src/setup-tests.ts` stubs `ResizeObserver` for the cases that do use it.

Series colours come from `seriesColor()` in `src/api.ts`, which returns the `--series-N` custom
properties from `theme.css`, so the palette stays in one place.

## Alternatives considered

| Option | Why not |
|---|---|
| Chart.js / ECharts | Canvas. A canvas chart is one opaque element to Testing Library — no bars, no labels, nothing to assert. With browser-level tests ruled out, that would leave the panels effectively untested |
| visx | Renders SVG and tests well, but is a toolkit rather than a chart library: each of the six panel types would be assembled from scales and shapes by hand, in three tasks that cannot see each other's code |
| Nivo | Comparable SVG output and API, but a heavier dependency tree for the same result |
| Hand-written SVG | Fine for the funnel, poor for stacked bars with an open-ended set of issue classes, and every panel would reinvent axes and ticks |

## Consequences

Panels are declarative React trees, so a component test can query a bar or a label by role or text
and assert the value that was drawn — which is where the formatting defects the testing rules warn
about actually show up. The bundle carries Recharts and its D3 dependencies; it is tree-shaken out
entirely while no panel imports it, and the shell alone builds to 62 kB gzipped.

**What would tell us this was wrong:** panels whose tests can only assert that *something* rendered,
because the chart's numbers never reach the DOM in a queryable form. If that happens across several
panels, the library is not paying for the constraint it imposed, and the fix is to render the values
alongside the chart as a table — accessible anyway — rather than to swap libraries mid-wave.
