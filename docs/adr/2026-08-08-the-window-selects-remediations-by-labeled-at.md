---
title: The window selects remediations by labeled_at, and every figure is computed over that set
status: accepted
date: 2026-08-08
type: architecture
areas: [analytics]
tasks: [T16, T25]
files: [src/sentinel/analytics/metrics.py]
specs: [docs/07-observability.md]
supersedes:
---

# The window selects remediations by labeled_at, and every figure is computed over that set

## Context

[07](../07-observability.md) says the figures are "scoped to a time window on `labeled_at`", and
then defines metrics over five other timestamps. A remediation labelled on the 7th and merged on the
9th is inside a window ending on the 8th by that rule, while its merge is not — so "merged per day"
and "the window" mean different things unless one of them yields.

The same ambiguity reaches the fix-cycle figures. `docs/07-observability.md` defines fix cycles as a
"mean count of `remediation_event` rows" without naming what the mean is taken over, and the sample
payload in that document does not settle it: its funnel counts eight labelled and five merged, while
its cycle distribution sums to five and its mean of `0.8` matches neither population.

## Decision

One set of rows underlies the whole payload: the remediations whose `labeled_at` falls in the
half-open interval `[from, to)`. Every figure is a reduction of that set and of nothing else.

- The boundary is half-open, so consecutive windows tile the timeline and no remediation is counted
  in two of them.
- `throughput` groups those remediations by the UTC day of their `merged_at`, which may fall outside
  the window. A merge on the 9th is reported on the 9th.
- `cycles.mean` and `cycles.distribution` are taken over every remediation in the set, merged or
  not, so the distribution sums to `funnel.labelled`.
- A remediation labelled outside the window contributes nothing, whatever happened to it inside.

## Alternatives considered

| Option | Why not |
|---|---|
| Scope each figure by its own timestamp — merges by `merged_at`, PRs by `pr_opened_at` | The funnel stops being a funnel: `merged` would count remediations absent from `labelled`, and a success rate of `merged / labelled` could exceed 1. The dashboard divides these counts by each other in four places |
| Clip `throughput` to the window | Days would silently lose merges, and the panel's own total would drift below `funnel.merged` for a reason no caption could explain |
| Average fix cycles over merged remediations only | A remediation that looped three times and was then abandoned would count for nothing — the most flattering possible reading of the window, and the opposite of what "how much self-correction each fix needed" is asked to reveal |

## Consequences

Every figure in the payload is a statement about one identifiable set of issues, which is what makes
the funnel counts usable as denominators by panels that did not compute them — the property the
dashboard's own rule for undefined figures depends on. The cost is that the throughput series can
name a day outside the window it is published in, and that a window ending now under-reports merges
for work labelled near its end; both are properties of measuring a pipeline by when work arrived
rather than by when it finished, which is the measurement the spec asks for.

**What would tell us this was wrong:** a reader comparing two adjacent windows and finding the same
merge in both, or a throughput panel whose days are so far outside the window that the series stops
reading as "this window's work".
