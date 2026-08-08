---
title: Panels render the figure the API computed, and the funnel decides whether it exists
status: accepted
date: 2026-08-08
type: architecture
areas: [dashboard, analytics]
tasks: [T31, T32, T33]
files: [dashboard/src/panels/Kpi.tsx, dashboard/src/panels/Funnel.tsx, dashboard/src/panels/Throughput.tsx]
specs: [docs/07-observability.md]
supersedes:
---

# Panels render the figure the API computed, and the funnel decides whether it exists

## Context

Almost every metric in [07](../07-observability.md) is a ratio or a percentile over a funnel stage:
success rate is `merged / labelled`, merge rate is `merged / pr_opened`, MTTR is a percentile taken
over merged remediations, cost per fix is `sum(acus_consumed) / merged × ACU_UNIT_COST_USD`. The
summary payload carries both the inputs and the computed figure, so a panel can either read the
figure or recompute it.

An empty window is a normal response, not an error: `funnel.labelled` is 0 and the API sends `0` for
every rate, because 0/0 has no other representation in JSON. `api.ts` already provides `NO_VALUE`
for "a number this panel does not have", and `.claude/rules/ui-testing.md` requires that no panel
show `NaN`, `Infinity` or a blank box.

The two sources also disagree in the third decimal. In the spec's own sample payload
`acus_per_merged_fix` is `12.3` and `unit_cost_usd` is `2.25`, whose product is `27.675`, while
`usd_per_fix` is `27.6` — the API multiplies before rounding and publishes a display-rounded ACU
figure alongside. Nine panels built by three tasks that cannot see each other's code have to agree
on which of the two a reader is looking at.

## Decision

A panel renders the figure the API computed. It reads the funnel only to decide whether that figure
exists at all:

- `funnel.labelled === 0` — success rate is undefined;
- `funnel.pr_opened === 0` — merge rate is undefined;
- `funnel.merged === 0` — MTTR and cost per fix are undefined.

An undefined figure renders as `NO_VALUE` — an em dash — with a caption saying why ("nothing merged
yet"), never as `0%` or `$0.00`. The same rule at chart scale replaces a chart with a sentence: a
funnel of five zero-length bars and a throughput chart of no days are both blank boxes, so they say
"No issues were labelled in this window." and "Nothing was merged in this window." instead.

The same authority settles a panel that breaks a funnel quantity down rather than restating it. The
throughput chart is `funnel.merged` split by day, so the funnel decides what it may say about
merges: a daily series that arrived empty beside a funnel counting five merges is a gap in that
panel, not a quiet window, and it says so. Where the series does not add up to `funnel.merged` — as
it does not in the sample payload in [07](../07-observability.md), whose funnel counts five and
whose `throughput` lists two — the caption names both numbers ("2 of 5 merged across 1 day") instead
of presenting its own total as the window's.

The formatters in `api.ts` are the second line of the same rule: each returns `NO_VALUE` for a
non-finite input, so an API defect cannot put `Infinity%` on the dashboard even where the funnel
looked healthy.

## Alternatives considered

| Option | Why not |
|---|---|
| Recompute each figure in the panel from the funnel | The panel's inputs are display-rounded, so it would print a *different* number from the one the cost and impact panels show — and be less accurate than both. Nine panels recomputing nine ways is exactly the divergence the single payload was meant to avoid |
| Render the API's zeros as `0%` and `$0.00` | "0% success rate" is a claim that the pipeline failed. An empty window means nobody labelled an issue, which is a different fact and would lead a reader to a different action |
| Hide a tile whose figure is undefined | The KPI row would reflow between polls, and the reader could not tell a missing metric from a broken panel. A dashboard that hides what it cannot answer cannot be evaluated — the same argument [07](../07-observability.md) makes for showing escalations |
| Let the API send `null` for an undefined figure | The schema in [07](../07-observability.md) is fixed and typed as numbers; changing it is T25's to make, and the panels would still need this rule for a window where only some stages are zero |

## Consequences

An empty window renders as a complete dashboard whose figures read "—", and a window where issues
were labelled but none reached a pull request shows a real `0%` success rate next to an undefined
merge rate — the distinction between "the agent rarely finishes" and "there was nothing to finish"
survives to the screen. Because the panels agree on where the figures come from, two panels showing
the same quantity cannot disagree in the last digit.

The cost is that a panel is only as correct as the API: if `rates.success` were wrong, no panel
would notice. That is deliberate — the analytics endpoint is where the metric definitions are
implemented and tested (T25) — and the component tests hold the line from the other side by
asserting each rendered figure against the formula in [07](../07-observability.md) computed from the
funnel counts in the fixture, so a payload whose rates contradict its own funnel fails the suite.

**What would tell us this was wrong:** a reader mistaking an em dash for a broken panel. The captions
exist to prevent exactly that; if they do not, the answer is to say "no data yet" in words on the
tile, not to print a zero that is not true.
