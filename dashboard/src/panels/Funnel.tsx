// The funnel: counts at each stage, labelled → session created → PR opened → CI green → merged.
// The question it answers is "where does work stop?", so each bar carries its share of `labelled`
// and the panel names the largest single drop rather than leaving the reader to subtract.

import type { CSSProperties } from 'react'
import { Bar, BarChart, LabelList, XAxis, YAxis } from 'recharts'
import {
  formatPercent,
  showData,
  type Funnel as FunnelCounts,
  type PanelProps,
  type PanelSlot,
} from '../api'

export const slot: PanelSlot = 'chart'
export const title = 'Funnel'
/** The spec lists the panels in this order; the KPI row is first and sits in its own slot. */
export const order = 2

// Explicit dimensions, not ResponsiveContainer: a container that measures itself renders nothing in
// jsdom, and component tests are the only automated check these panels have
// (docs/adr/2026-08-08-recharts-for-the-dashboard-charts.md). The height leaves room for the
// caption inside the 240px the spec allows a chart.
const CHART_WIDTH = 620
const CHART_HEIGHT = 200

const CAPTION: CSSProperties = { margin: '2px 0 0', fontSize: 12, color: 'var(--muted)' }

const STAGES: { key: keyof FunnelCounts; label: string }[] = [
  { key: 'labelled', label: 'Labelled' },
  { key: 'session_created', label: 'Session created' },
  { key: 'pr_opened', label: 'PR opened' },
  { key: 'ci_green', label: 'CI green' },
  { key: 'merged', label: 'Merged' },
]

interface StageRow {
  stage: string
  count: number
  label: string
}

function stageRows(funnel: FunnelCounts): StageRow[] {
  return STAGES.map(({ key, label }) => ({
    stage: label,
    count: funnel[key],
    label: `${funnel[key]} (${formatPercent(funnel[key] / funnel.labelled)})`,
  }))
}

/**
 * The largest stage-to-stage loss, or null when nothing was lost. Ties go to the earliest pair:
 * where work first stops is what a reader would act on, and it keeps the caption deterministic.
 */
function largestDrop(rows: StageRow[]): { from: string; to: string; lost: number } | null {
  let worst: { from: string; to: string; lost: number } | null = null
  for (let i = 1; i < rows.length; i += 1) {
    const lost = rows[i - 1].count - rows[i].count
    if (lost > 0 && (worst === null || lost > worst.lost)) {
      worst = { from: rows[i - 1].stage, to: rows[i].stage, lost }
    }
  }
  return worst
}

export default function Funnel({ state }: PanelProps) {
  if (!showData(state)) {
    return (
      <p className="slot-empty">
        {state.status === 'loading' ? 'Loading…' : 'No figures have arrived yet.'}
      </p>
    )
  }

  const { funnel } = state.data
  if (funnel.labelled === 0) {
    // Every share divides by `labelled`, and five bars of zero would be an empty box either way.
    return <p className="slot-empty">No issues were labelled in this window.</p>
  }

  const rows = stageRows(funnel)
  const drop = largestDrop(rows)

  return (
    <>
      <BarChart
        width={CHART_WIDTH}
        height={CHART_HEIGHT}
        data={rows}
        layout="vertical"
        margin={{ top: 4, right: 96, bottom: 4, left: 4 }}
      >
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="stage" width={110} tickLine={false} axisLine={false} />
        {/* One accent colour: the funnel is a single series, so the --series-* palette stays
            reserved for the stacked throughput bars. */}
        <Bar dataKey="count" fill="var(--accent)" barSize={22} isAnimationActive={false}>
          <LabelList dataKey="label" position="right" />
        </Bar>
      </BarChart>
      <p style={CAPTION}>
        {drop === null
          ? 'No work was lost between stages.'
          : `Largest drop: ${drop.from} → ${drop.to} (${drop.lost} lost)`}
      </p>
    </>
  )
}
