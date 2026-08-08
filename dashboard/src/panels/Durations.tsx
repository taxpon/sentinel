// The duration distribution panel — "where is the time going now?" in docs/07-observability.md.
//
// Three figures, each taken from the metric table in that document:
//
//   Time to PR      percentile of `pr_opened_at − labeled_at`, p50 and p90
//   MTTR            percentile of `merged_at − labeled_at`, p50 and p90
//   Review latency  percentile of `merged_at − ci_green_at`
//
// A metric is drawn only when the funnel says a sample exists. A percentile over an empty sample
// arrives from the API as `0`, and a bar reading "MTTR p50 0s" claims every issue merged instantly
// — see docs/adr/2026-08-08-blank-a-figure-whose-denominator-is-empty.md.

import type { CSSProperties } from 'react'
import { Bar, BarChart, LabelList, Legend, XAxis, YAxis, type RenderableText } from 'recharts'
import {
  NO_VALUE,
  formatDurationSeconds,
  showData,
  type AnalyticsSummary,
  type PanelProps,
  type PanelSlot,
} from '../api'

export const title = 'Duration distribution'
export const slot: PanelSlot = 'chart'
export const order = 30

export interface DurationRow {
  key: 'to_pr' | 'to_merge' | 'review_latency'
  label: string
  p50: number
  p90: number
  /** The funnel count the percentiles were computed over. Zero means there was no sample. */
  sampleCount: number
  /** Shown in place of the bars when `sampleCount` is zero. */
  missing: string
}

/**
 * The three rows in spec order, each paired with the funnel stage that produced its samples:
 * to-PR needs a pull request, and both merge-relative figures need a merge.
 */
export function durationRows(summary: AnalyticsSummary): DurationRow[] {
  const { durations_seconds: durations, funnel } = summary
  return [
    {
      key: 'to_pr',
      label: 'Time to PR',
      ...durations.to_pr,
      sampleCount: funnel.pr_opened,
      missing: 'no pull request opened in this window',
    },
    {
      key: 'to_merge',
      label: 'MTTR',
      ...durations.to_merge,
      sampleCount: funnel.merged,
      missing: 'nothing merged in this window',
    },
    {
      key: 'review_latency',
      label: 'Review latency',
      ...durations.review_latency,
      sampleCount: funnel.merged,
      missing: 'nothing merged in this window',
    },
  ]
}

/** Fixed, because a self-measuring container renders nothing in jsdom (the Recharts ADR). */
const CHART_WIDTH = 460
const ROW_HEIGHT = 46
const CHART_CHROME = 44

const note: CSSProperties = { margin: 0, color: 'var(--muted)' }
const missingList: CSSProperties = {
  margin: '4px 0 0',
  padding: 0,
  listStyle: 'none',
  color: 'var(--muted)',
}

/** Recharts hands a label whatever was in the data row, so the seconds are narrowed here. */
const labelDuration = (value: RenderableText): string =>
  typeof value === 'number' ? formatDurationSeconds(value) : NO_VALUE

export default function Durations({ state }: PanelProps) {
  if (!showData(state)) {
    return (
      <p style={note}>
        {state.status === 'error' ? 'No durations — the analytics API is unreachable.' : 'Loading…'}
      </p>
    )
  }

  const rows = durationRows(state.data)
  const measured = rows.filter((row) => row.sampleCount > 0)
  const missing = rows.filter((row) => row.sampleCount === 0)

  return (
    <div>
      {measured.length === 0 ? (
        <p style={note}>
          No remediation has reached a pull request in this window, so there is nothing to time yet.
        </p>
      ) : (
        <BarChart
          layout="vertical"
          width={CHART_WIDTH}
          height={CHART_CHROME + measured.length * ROW_HEIGHT}
          data={measured}
          margin={{ top: 4, right: 78, bottom: 0, left: 0 }}
        >
          <XAxis type="number" hide domain={[0, 'dataMax']} />
          <YAxis
            type="category"
            dataKey="label"
            width={110}
            tickLine={false}
            axisLine={false}
            tick={{ fill: 'var(--text)', fontSize: 12 }}
          />
          {/* One accent colour: p90 is the same hue at a lower opacity, not a second colour. The
              --series-* palette stays reserved for the stacked throughput series.

              Animation is off deliberately, and not only because Recharts draws nothing until the
              first frame in jsdom: the shell re-renders this panel every five seconds, and bars
              that grow from zero on every poll would make the panel unreadable while it is being
              watched — which is the only time it is being watched. */}
          <Bar
            dataKey="p50"
            name="p50"
            fill="var(--accent)"
            radius={[0, 3, 3, 0]}
            isAnimationActive={false}
          >
            <LabelList dataKey="p50" position="right" formatter={labelDuration} />
          </Bar>
          <Bar
            dataKey="p90"
            name="p90"
            fill="var(--accent)"
            fillOpacity={0.4}
            radius={[0, 3, 3, 0]}
            isAnimationActive={false}
          >
            <LabelList dataKey="p90" position="right" formatter={labelDuration} />
          </Bar>
          <Legend verticalAlign="bottom" height={24} iconType="square" iconSize={9} />
        </BarChart>
      )}

      {missing.length > 0 && (
        <ul style={missingList}>
          {missing.map((row) => (
            <li key={row.key} data-duration={row.key}>
              {row.label} {NO_VALUE} {row.missing}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
