// The KPI row: the three figures docs/07-observability.md puts above the fold — success rate, merge
// rate and MTTR p50. One module, three tiles: App.tsx mounts one <section> per panel module and
// theme.css gives the kpi region a single column, so the tile grid (.kpi-tiles) belongs inside this
// panel rather than in three sibling modules.
//
// A fourth tile reported cost per fix. It was removed with the rest of the spend reporting: Devin
// reports consumption in ACUs, this account is not billed in ACUs, and the figure was therefore
// structurally zero — see docs/07-observability.md#analytics-api. Every figure left here is
// computed from Sentinel's own timestamps.

import type { CSSProperties } from 'react'
import {
  NO_VALUE,
  formatDurationSeconds,
  formatPercent,
  showData,
  type AnalyticsSummary,
  type PanelProps,
  type PanelSlot,
} from '../api'

export const slot: PanelSlot = 'kpi'
export const title = 'Key metrics'

const LABEL: CSSProperties = { margin: 0, fontSize: 12, color: 'var(--muted)' }
const VALUE: CSSProperties = {
  margin: '2px 0 0',
  fontSize: 28,
  fontWeight: 600,
  lineHeight: 1.1,
  fontVariantNumeric: 'tabular-nums',
}
const CAPTION: CSSProperties = { margin: '2px 0 0', fontSize: 12, color: 'var(--muted)' }

interface TileProps {
  label: string
  value: string
  caption: string
}

/**
 * The three tiles, in the order the spec lists them.
 *
 * Each value is the figure the API computed; the funnel decides only whether that figure exists.
 * Every metric here is a ratio or a percentile over a funnel stage, so a stage of zero makes the
 * figure undefined rather than zero — an empty window would otherwise read as a 0% success rate,
 * which says the pipeline failed rather than that it was never asked to do anything. See
 * docs/adr/2026-08-08-an-undefined-figure-is-an-em-dash.md.
 */
function tiles(summary: AnalyticsSummary): TileProps[] {
  const { funnel, rates, durations_seconds } = summary

  return [
    {
      // merged / labelled
      label: 'Success rate',
      value: funnel.labelled > 0 ? formatPercent(rates.success) : NO_VALUE,
      caption:
        funnel.labelled > 0
          ? `${funnel.merged} merged of ${funnel.labelled} labelled`
          : 'nothing labelled in this window',
    },
    {
      // merged / pr_opened
      label: 'Merge rate',
      value: funnel.pr_opened > 0 ? formatPercent(rates.merge) : NO_VALUE,
      caption:
        funnel.pr_opened > 0
          ? `${funnel.merged} merged of ${funnel.pr_opened} opened`
          : 'no pull requests opened',
    },
    {
      // p50 of merged_at − labeled_at
      label: 'MTTR p50',
      value: funnel.merged > 0 ? formatDurationSeconds(durations_seconds.to_merge.p50) : NO_VALUE,
      caption:
        funnel.merged > 0
          ? `p90 ${formatDurationSeconds(durations_seconds.to_merge.p90)}`
          : 'nothing merged yet',
    },
  ]
}

function Tile({ label, value, caption }: TileProps) {
  return (
    <div className="kpi-tile" role="group" aria-label={label}>
      <p style={LABEL}>{label}</p>
      <p style={VALUE}>{value}</p>
      <p style={CAPTION}>{caption}</p>
    </div>
  )
}

export default function Kpi({ state }: PanelProps) {
  // Not `status === 'error'`: the shell keeps the last good payload through a failed poll and says
  // so in its banner, so a panel holding data draws it whatever the status is.
  if (!showData(state)) {
    return (
      <p className="slot-empty">
        {state.status === 'loading' ? 'Loading…' : 'No figures have arrived yet.'}
      </p>
    )
  }

  return (
    <div className="kpi-tiles">
      {tiles(state.data).map((tile) => (
        <Tile key={tile.label} {...tile} />
      ))}
    </div>
  )
}
