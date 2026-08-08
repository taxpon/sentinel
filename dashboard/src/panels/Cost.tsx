// The cost panel — "are the unit economics improving?" in docs/07-observability.md.
//
// The figures and their definitions, from the metric table in that document:
//
//   ACU spend            `sum(acus_consumed)` over the window
//   ACU per merged fix   `sum(acus_consumed) / merged`
//   Cost per fix         ACU per merged fix × `ACU_UNIT_COST_USD`
//
// Two things this panel has to say out loud, both of them about provenance:
//
//   1. Where the ACU counts came from. `cost.source` distinguishes Devin's own consumption endpoint
//      from Sentinel's local fallback, which docs/05-devin-integration.md#degradation requires the
//      dashboard to label.
//   2. That the dollar figures are Sentinel's own conversion in every case. `ACU_UNIT_COST_USD` is
//      a locally configured contract price — docs/09-operations.md says "set from your own
//      contract" — so a dollar figure is never something Devin reported, whatever `source` says.
//
// See docs/adr/2026-08-08-cost-panel-labels-acu-and-dollar-provenance-separately.md.
//
// The spec's panel table asks for "ACU per fix over time". The summary payload it fixes in the same
// document carries no daily cost series — only `throughput` is per-day — so this panel reports the
// window totals rather than inventing a trend line the API cannot support.

import type { CSSProperties, ReactNode } from 'react'
import {
  NO_VALUE,
  formatNumber,
  formatUsd,
  showData,
  type CostSource,
  type PanelProps,
  type PanelSlot,
} from '../api'

export const title = 'Cost per fix'
export const slot: PanelSlot = 'chart'
export const order = 50

/** What the reader is told about where the ACU counts came from. */
export const ACU_PROVENANCE: Record<CostSource, string> = {
  devin_consumption_api: "ACU counts come from Devin's consumption API.",
  derived: "ACU counts are derived from Sentinel's own acu_ledger — Devin's consumption API was not available.",
}

const note: CSSProperties = { margin: 0, color: 'var(--muted)' }
const headline: CSSProperties = {
  margin: 0,
  fontSize: 30,
  fontWeight: 600,
  lineHeight: 1.1,
  fontVariantNumeric: 'tabular-nums',
}
const caption: CSSProperties = { margin: '2px 0 0', color: 'var(--muted)' }
const figures: CSSProperties = {
  display: 'flex',
  gap: 'var(--gap)',
  margin: '10px 0 0',
  padding: 0,
  listStyle: 'none',
}
const figureValue: CSSProperties = { fontWeight: 600, fontVariantNumeric: 'tabular-nums' }
const provenance: CSSProperties = {
  margin: '10px 0 0',
  paddingTop: 8,
  borderTop: '1px solid var(--border)',
  color: 'var(--muted)',
}

function Figure({ metric, label, children }: { metric: string; label: string; children: ReactNode }) {
  return (
    <li data-metric={metric}>
      <span style={figureValue}>{children}</span>
      <br />
      <span style={{ color: 'var(--muted)' }}>{label}</span>
    </li>
  )
}

export default function Cost({ state }: PanelProps) {
  if (!showData(state)) {
    return (
      <p style={note}>
        {state.status === 'error'
          ? 'No cost figures — the analytics API is unreachable.'
          : 'Loading…'}
      </p>
    )
  }

  const { cost, funnel } = state.data
  // Both per-fix figures divide by `merged`. With nothing merged the API sends 0, and "$0.00 per
  // fix" alongside real ACU spend is the most expensive wrong number this dashboard could show.
  const hasMergedFix = funnel.merged > 0

  return (
    <div>
      <p style={headline} data-metric="usd_per_fix">
        {hasMergedFix ? formatUsd(cost.usd_per_fix) : NO_VALUE}
      </p>
      <p style={caption}>
        {hasMergedFix
          ? `per merged fix — ${formatNumber(cost.acus_per_merged_fix)} ACU × ${formatUsd(cost.unit_cost_usd)} per ACU, over ${funnel.merged} merged`
          : 'per merged fix — nothing merged in this window, so there is no cost per fix yet'}
      </p>

      <ul style={figures}>
        <Figure metric="acus_total" label="ACUs spent">
          {formatNumber(cost.acus_total)}
        </Figure>
        <Figure metric="acus_per_merged_fix" label="ACUs per merged fix">
          {hasMergedFix ? formatNumber(cost.acus_per_merged_fix) : NO_VALUE}
        </Figure>
        <Figure metric="unit_cost_usd" label="per ACU (configured)">
          {formatUsd(cost.unit_cost_usd)}
        </Figure>
      </ul>

      <p style={provenance} data-provenance={cost.source}>
        {ACU_PROVENANCE[cost.source]} Dollars are Sentinel's own conversion at{' '}
        {formatUsd(cost.unit_cost_usd)} per ACU (ACU_UNIT_COST_USD), a locally configured contract
        price — not a figure Devin reported.
      </p>
    </div>
  )
}
