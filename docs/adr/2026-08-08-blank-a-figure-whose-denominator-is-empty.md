---
title: Blank a dashboard figure whose denominator is empty rather than drawing the zero the API sent
status: accepted
date: 2026-08-08
type: architecture
areas: [dashboard, analytics]
tasks: [T32]
files:
  - dashboard/src/panels/Durations.tsx
  - dashboard/src/panels/Cost.tsx
  - dashboard/src/panels/Autonomy.tsx
  - dashboard/src/panels/Failures.tsx
specs: [docs/07-observability.md]
supersedes:
---

# Blank a dashboard figure whose denominator is empty rather than drawing the zero the API sent

## Context

Most figures in the metric table of [07](../07-observability.md) are ratios or percentiles over a
funnel stage:

| Figure | Divides by |
|---|---|
| MTTR, review latency | merges |
| Autonomy rate | merges |
| ACU per merged fix, cost per fix | merges |
| Time to PR | pull requests opened |

The response schema in the same document types every one of them as a plain `number`. There is no
null and no sample count, so a window in which nothing merged is transmitted as
`"to_merge": {"p50": 0, "p90": 0}`, `"autonomy": 0` and `"usd_per_fix": 0` — the same bytes that a
window of instant, free, fully autonomous merges would produce. `emptySummaryFixture` in
`dashboard/src/fixtures/summary.ts` is exactly this payload.

Rendered literally, those zeros read as measurements: "MTTR p50 0s", "0% autonomous", "$0.00 per
fix" next to 24.5 ACUs of real spend. Each is a specific, confident, wrong claim about the
system — and the autonomy and cost figures are the two a leader is most likely to repeat.

`.claude/rules/ui-testing.md` already forbids `NaN`, `Infinity` and a blank box. It does not cover
this, because nothing here is non-finite: the arithmetic succeeded, on no data.

## Decision

A panel draws a ratio or a percentile only when the funnel count it was computed over is greater
than zero. Otherwise it renders `NO_VALUE` (`—`, already in `api.ts`) and, next to it, the reason:
"nothing merged in this window, so there is no cost per fix yet".

Each panel reads the denominator from `funnel` in the same payload, so no second request and no
schema change are involved:

- `funnel.merged` gates MTTR, review latency, autonomy rate, ACU per merged fix and cost per fix;
- `funnel.pr_opened` gates time to PR.

Figures that are **not** ratios keep their zero, because zero is the measurement: `acus_total` of
`0.0` means nothing was spent, and `unit_cost_usd` is configuration that exists whether or not
anything ran. The distinction is "no sample" versus "a sample that came to zero", and only the
first is blanked.

The failure breakdown draws the same line without a denominator: an empty `failures` array with
`labelled > 0` is the good news that nothing was blocked, while an empty array with `labelled = 0`
is no news at all, and the two get different sentences.

## Alternatives considered

| Option | Why not |
|---|---|
| Render the zeros as sent | The dashboard's whole claim is that its numbers can be trusted; "0% autonomous" for a window that merged nothing is a wrong answer stated confidently, which is worse than no answer |
| Make the fields nullable in the API schema | The right long-term fix, but the schema in [07](../07-observability.md) is fixed and three panel tasks and T25 are building against it in parallel. A panel can derive the same fact from `funnel`, which is already in the payload |
| Hide the whole panel when its denominator is empty | A panel that vanishes reads as a broken dashboard, and it removes the one thing worth saying — that nothing has merged yet |
| Show the zero with an asterisk or a tooltip | The figure is still the first thing read and the first thing quoted; a footnote does not undo it, and a tooltip does not exist for a reader scanning a wall display |

## Consequences

Every ratio on the dashboard carries its sample with it, so a reader can tell "we have not merged
anything" from "we merge instantly and for free". The cost panel can report real ACU spend against
an empty merge count — the single most useful thing it can say in a bad week — without implying the
work was free.

The cost is one more branch per figure, and a panel that must know which funnel stage produced each
number. That knowledge is not incidental: it is the definition from [07](../07-observability.md),
and writing it down in the panel is what makes the definition checkable by a test.

**What would tell us this was wrong:** a reader asking why the dashboard is "broken" or "missing
numbers" when it is deliberately blank, or an em dash appearing where a real measurement existed
because a funnel count and a percentile disagreed about their sample. The first is a wording
problem in the sentence next to the dash; the second means the denominator is genuinely on the
server side and the schema should carry a sample count.
