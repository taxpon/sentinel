---
title: Retry jitter takes away at most half the delay, so the ten-minute cap stays a ceiling
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T13]
files: [src/sentinel/queue.py]
specs: [docs/06-event-pipeline.md]
supersedes:
---

# Retry jitter takes away at most half the delay, so the ten-minute cap stays a ceiling

## Context

The retry row of [06](../06-event-pipeline.md#reliability-policy) reads
`run_after = now() + 2^attempts × 5 s` with jitter, capped at 10 min. It fixes the schedule and the
ceiling but not the shape of the jitter, and the shape is not a detail: it decides both how far
apart a fleet of workers retries a rate-limited API and whether the stated cap is still true.

The failures this backoff exists for — `429` and `5xx` from Devin — are exactly the ones that arrive
for several jobs at once, so identical delays would send the whole fleet back at the same instant.

## Decision

Equal jitter: half the scheduled delay, plus a random half of it. `backoff_delay()` computes
`min(2^attempts × 5 s, 10 min)` and multiplies it by a factor drawn uniformly from `[0.5, 1.0]`, so
the delay always lies between half the schedule and the schedule itself. The random source is a
parameter with `random.random` as its default, so the schedule can be asserted exactly rather than
statistically.

## Alternatives considered

| Option | Why not |
|---|---|
| Full jitter, uniform over `[0, delay]` | Spreads the fleet the widest, but a draw near zero retries a rate-limited API almost immediately, which is the one thing the backoff is there to prevent. It also makes the mean delay half the published schedule |
| A fixed percentage, `delay ± 10 %` | Barely spreads anything: three workers failing together retry within a couple of seconds of each other, which for a rate limit is the same instant |
| Jitter added on top, `delay + rand(0, delay)` | Pushes the last retries to twenty minutes and makes "capped at 10 min" false — the ceiling is the part of the spec an operator reads when asking how long a job can sit |
| Decorrelated jitter, seeded from the previous delay | Well behaved, but it needs the previous delay carried on the row, and `attempts` is the only state the schema keeps |

## Consequences

The published schedule is the upper bound of what any job actually waits, so the cap in the spec is
the number an operator can rely on. Two workers failing at the same moment come back at least a few
seconds apart on the very first retry, and further apart as the delay grows.

The ten minutes is a ceiling on the formula rather than on anything a default deployment reaches:
`fail()` gives up at `MAX_JOB_ATTEMPTS` without computing a delay, so at the spec default of 5 the
schedule is only ever evaluated for `attempts` 1 to 4 and the longest a job actually waits before
its next try is **80 seconds**. The cap begins to bite only where `MAX_JOB_ATTEMPTS` is raised to 8
or beyond. Worth stating, because anything sized against the larger number — a lease timeout, an
alert on queue age — would be over-provisioned by an order of magnitude.

Because the source is injectable, the schedule is pinned by a table-driven test at both ends of the
jitter range rather than by sampling. It covers `attempts` 1 to 8, which is past what the default
configuration reaches, so the doubling and the cap are both pinned rather than only the four rows in
use today.

**What would tell us this was wrong:** retries still arriving in a burst — visible as `429`s
clustered in `remediation_event` — which would mean the spread has to be wider than half the delay
and full jitter with a floor is the better shape.
