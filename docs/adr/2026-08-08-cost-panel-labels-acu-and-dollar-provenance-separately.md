---
title: The cost panel labels the provenance of its ACUs and of its dollars separately
status: deprecated
date: 2026-08-08
type: architecture
areas: [dashboard, analytics, devin]
tasks: [T32]
files:
  - dashboard/src/panels/Cost.tsx
specs:
  - docs/07-observability.md
  - docs/05-devin-integration.md
  - docs/09-operations.md
supersedes:
---

# The cost panel labels the provenance of its ACUs and of its dollars separately

## Context

[05](../05-devin-integration.md#degradation) requires the dashboard to label any figure served by a
fallback, "so a reader always knows which numbers came from Devin and which Sentinel derived
itself". [07](../07-observability.md) carries that into the payload as `cost.source`, a two-member
union: `devin_consumption_api` or `derived`. `.claude/rules/ui-testing.md` makes the label a
required test on this panel and no other.

`cost.source` describes one thing: whether the ACU counts came from
`GET /v3/organizations/{org_id}/consumption/daily` or were summed from Sentinel's own `acu_ledger`.

The dollar figures have a different and unconditional provenance. [07](../07-observability.md)
defines cost per fix as `ACU per merged fix × ACU_UNIT_COST_USD`, and
[09](../09-operations.md#configuration) describes `ACU_UNIT_COST_USD` as "**Set from your own
contract** — it only scales the cost panel". It is a local guess at a contract price with a default
of `2.25`. Devin never reports dollars, so no dollar on this panel is ever a figure Devin sent, and
`source: devin_consumption_api` is silent about that.

A single provenance badge therefore has a failure mode: rendering "from Devin's consumption API"
above `$27.60` attributes to Devin a number Devin did not produce, on the one panel whose entire
purpose is to be comparable to an engineer's salary.

The same document's panel table asks for "Cost — ACU per fix over time", but the summary schema it
fixes carries no daily cost series; only `throughput` is per-day.

## Decision

The panel states two provenances in one sentence, and always both:

1. **Where the ACU counts came from**, keyed off `cost.source` — Devin's consumption API, or
   Sentinel's own `acu_ledger` because that endpoint was not available.
2. **That the dollars are Sentinel's own conversion**, at the `unit_cost_usd` the payload actually
   carried, naming `ACU_UNIT_COST_USD` as a locally configured contract price and saying explicitly
   that it is not a figure Devin reported. This sentence does not vary with `source`, because the
   fact it states does not.

The unit cost is rendered from `cost.unit_cost_usd` rather than from the documented default, so an
operator who set their own contract price sees their own price in the caption.

The panel reports the window totals — ACU spend, ACUs per merged fix, cost per fix — rather than a
trend line, because the payload has no daily cost series to draw one from. That gap belongs to the
summary schema, not to the panel, and is not worked around here.

## Alternatives considered

| Option | Why not |
|---|---|
| One badge driven by `cost.source` | Reads as covering every figure in the panel, and so attributes the dollars to Devin whenever the ACUs came from Devin. The most consequential number on the panel would be the one whose provenance is misstated |
| Label only the `derived` case | Provenance a reader has to notice the absence of is not provenance. The two states must be equally visible, or the unlabelled one is read as authoritative |
| Show only ACUs and drop dollars entirely | [07](../07-observability.md) chose dollars deliberately — "directly comparable to engineer cost" — and a labelled estimate is more useful than no estimate |
| Derive a daily ACU-per-fix series from `throughput` | `throughput` carries merges per day and no ACUs. Any trend line drawn from it would be invented |

## Consequences

A reader can tell exactly how much of `$27.60` is measurement and how much is configuration, which
is the difference between a number they can take to a budget conversation and one they cannot. The
panel is honest in its best case as well as its degraded one.

The cost is a sentence of prose on a 240px panel, and a caption that repeats the unit cost. It also
means this panel does not answer the "over time" question [07](../07-observability.md) poses for it;
the window figure answers "what does a fix cost", not "is it getting cheaper".

**What would tell us this was wrong:** a reader treating the dollar figure as a Devin invoice
despite the sentence, which would mean the label is not where the eye lands and the caption should
sit against the figure rather than under the panel. Separately, if the summary payload gains a daily
ACU series, the trend line the spec asked for should replace the single figure, and this record
should be revisited rather than the gap left standing.
