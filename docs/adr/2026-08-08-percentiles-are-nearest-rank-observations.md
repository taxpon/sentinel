---
title: A published percentile is an observed duration, taken at the nearest rank
status: accepted
date: 2026-08-08
type: architecture
areas: [analytics]
tasks: [T16, T25]
files: [src/sentinel/analytics/metrics.py]
specs: [docs/07-observability.md]
supersedes:
---

# A published percentile is an observed duration, taken at the nearest rank

## Context

Three of the figures in [07](../07-observability.md) are percentiles: time to PR, MTTR and review
latency, each published at p50 and p90. The document names the percentiles and not a method for
computing one, and the common methods disagree on exactly the sample sizes this dashboard reports.

The portfolio is eight remediations ([01](../01-overview.md)), and a seven-day window holds fewer.
With four merges, linear interpolation — what `numpy.percentile` and `statistics.quantiles` default
to — puts p50 between the second and third observation and p90 between the third and fourth. Both
answers are durations that no remediation took.

The p90 is read as a claim. "MTTR p90 is four hours" is heard as *nine out of ten fixes landed
within four hours*, and every row on the dashboard is meant to be checkable at source: the live
table links each remediation to its Devin session and its pull request precisely so a reader can go
and look.

## Decision

`percentile(values, rank)` returns the value at position `ceil(rank / 100 × n)` of the sorted
sample, one-based — the nearest-rank definition. The result is always a member of the sample, at
least `rank` percent of the observations are at or below it, and an empty sample is `0`.

With one observation, both percentiles are that observation. With two, p50 is the lower and p90 the
upper. No published figure is ever an average of two remediations.

## Alternatives considered

| Option | Why not |
|---|---|
| Linear interpolation between ranks | Publishes a duration nobody measured, and breaks the reading a leader gives p90 — with four merges the interpolated p90 is *below* the second-slowest fix, so "nine out of ten within X" would be false |
| The mean, for the p50 | A single stalled remediation moves it by hours. The spec asks for percentiles for exactly that reason, and MTTR is the headline figure |
| Refuse to publish a percentile below some sample size | The window would show an em dash for its headline number in every early week, which is when the system most needs to be shown working. The sample size is visible next to it — the funnel is in the same payload |

## Consequences

Every duration on the dashboard is a duration something actually took, so a reader who clicks
through to the slowest remediation finds the p90 sitting on a real pull request. On small samples
the p90 is frequently the maximum, which overstates the tail — and is the conservative direction for
a figure that gets quoted.

**What would tell us this was wrong:** windows growing to hundreds of remediations, where
interpolation is both defensible and materially different, and where the p90 landing on a single
outlier row would start to mislead rather than to caution.
