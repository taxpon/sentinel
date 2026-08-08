// The autonomy panel — "how much human attention does each fix still need?" in
// docs/07-observability.md.
//
// This is the panel that claims work happened without people, so it states what it counted rather
// than showing a bare percentage. From the metric table in that document:
//
//   Autonomy rate  `merged with cycle = 0 and human_message_count = 0 / merged`
//   Fix cycles     mean count of `remediation_event` rows with `to_state = RUNNING` and
//                  `from_state ∈ {CI_FAILED, CHANGES_REQUESTED}`
//
// The rate divides by `merged`. With nothing merged the API sends 0, and "0% autonomous" reads as a
// system that always needed a person rather than one that has not finished anything yet — see
// docs/adr/2026-08-08-blank-a-figure-whose-denominator-is-empty.md.

import type { CSSProperties } from 'react'
import { Bar, BarChart, LabelList, XAxis, YAxis } from 'recharts'
import {
  NO_VALUE,
  formatNumber,
  formatPercent,
  showData,
  type Cycles,
  type PanelProps,
  type PanelSlot,
} from '../api'

export const title = 'Autonomy'
export const slot: PanelSlot = 'chart'
export const order = 40

export interface CycleBar {
  /** The cycle count, as the JSON object key it arrived as. */
  cycles: string
  count: number
}

/**
 * The distribution in ascending cycle order. The keys arrive as strings, so they are compared as
 * numbers: sorted as text, 10 fix cycles would sit between 1 and 2.
 */
export function cycleBars(distribution: Cycles['distribution']): CycleBar[] {
  return Object.entries(distribution)
    .map(([cycles, count]) => ({ cycles, count }))
    .sort((a, b) => Number(a.cycles) - Number(b.cycles))
}

const CHART_WIDTH = 440
const CHART_HEIGHT = 118

const note: CSSProperties = { margin: 0, color: 'var(--muted)' }
const headline: CSSProperties = {
  margin: 0,
  fontSize: 30,
  fontWeight: 600,
  lineHeight: 1.1,
  fontVariantNumeric: 'tabular-nums',
}
/* Both explanatory lines are set smaller so the headline, the two sentences that say what was
   counted and the distribution all fit the 240px the spec allows a chart panel. */
const caption: CSSProperties = {
  margin: '2px 0 6px',
  color: 'var(--muted)',
  fontSize: 12,
  lineHeight: 1.35,
}

export default function Autonomy({ state }: PanelProps) {
  if (!showData(state)) {
    return (
      <p style={note}>
        {state.status === 'error'
          ? 'No autonomy figures — the analytics API is unreachable.'
          : 'Loading…'}
      </p>
    )
  }

  const { rates, cycles, funnel } = state.data
  const hasMerged = funnel.merged > 0
  const bars = cycleBars(cycles.distribution)

  return (
    <div>
      <p style={headline} data-metric="autonomy_rate">
        {hasMerged ? formatPercent(rates.autonomy) : NO_VALUE}
      </p>
      <p style={caption}>
        {hasMerged
          ? `of ${funnel.merged} merged fixes ran with cycle = 0 and human_message_count = 0 — no fix cycle, no human message`
          : 'nothing merged in this window, so there is no autonomy rate yet'}
      </p>

      {bars.length === 0 ? (
        <p style={note} data-metric="cycles_mean">
          No fix-cycle distribution yet — no remediation has run in this window.
        </p>
      ) : (
        <>
          <p style={note} data-metric="cycles_mean">
            {formatNumber(cycles.mean)} fix cycles per remediation on average — a cycle is a return
            to RUNNING from CI_FAILED or CHANGES_REQUESTED.
          </p>
          <BarChart
            width={CHART_WIDTH}
            height={CHART_HEIGHT}
            data={bars}
            margin={{ top: 16, right: 8, bottom: 4, left: 0 }}
          >
            <XAxis
              dataKey="cycles"
              tickLine={false}
              axisLine={false}
              tick={{ fill: 'var(--muted)', fontSize: 12 }}
            />
            <YAxis hide />
            {/* Animation off: the shell re-renders every five seconds, and Recharts draws nothing
                until the first animation frame, which is also all a component test can see. */}
            <Bar
              dataKey="count"
              name="remediations"
              fill="var(--accent)"
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
            >
              <LabelList dataKey="count" position="top" />
            </Bar>
          </BarChart>
        </>
      )}
    </div>
  )
}
