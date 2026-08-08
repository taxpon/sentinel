// Throughput: merged per day, stacked by issue class. The stack is the point — sustained capacity
// concentrated in one easy class is a different answer to "is this working?" than the same total
// spread across real problem types.
//
// This panel breaks `funnel.merged` down by day; it is not a second count of it. Where the series
// does not account for the whole funnel, the panel says so rather than presenting its own total as
// the window's — the KPI row reads `funnel.merged` from the same payload and the two are read
// together (docs/adr/2026-08-08-an-undefined-figure-is-an-em-dash.md).

import type { CSSProperties } from 'react'
import { Bar, BarChart, Legend, XAxis, YAxis } from 'recharts'
import {
  seriesColor,
  showData,
  type PanelProps,
  type PanelSlot,
  type ThroughputDay,
} from '../api'

export const slot: PanelSlot = 'chart'
export const title = 'Throughput by day'
/** After the funnel, per the panel order in docs/07-observability.md. */
export const order = 3

// Explicit dimensions rather than ResponsiveContainer, which measures itself and so renders nothing
// in jsdom (docs/adr/2026-08-08-recharts-for-the-dashboard-charts.md). The legend is drawn inside
// the chart, and the whole panel stays under the 240px the spec allows.
const CHART_WIDTH = 620
const CHART_HEIGHT = 200

const CAPTION: CSSProperties = { margin: '2px 0 0', fontSize: 12, color: 'var(--muted)' }

/** One day's counts, aligned with `classes` so the chart and the table read the same numbers. */
interface DayRow {
  day: string
  counts: number[]
  total: number
}

/** Issue classes are open-ended, so the series are the union across days, sorted for a stable
 *  colour assignment: a class must not change colour because it happened to merge first today. */
function classesIn(throughput: ThroughputDay[]): string[] {
  const classes = new Set<string>()
  for (const day of throughput) {
    for (const issueClass of Object.keys(day.by_class)) classes.add(issueClass)
  }
  return [...classes].sort()
}

/**
 * Ascending by day — the API is not specified to send them in order, and a bar chart implies one.
 *
 * A class absent from a day merged none of it that day, which is a zero and not a gap. That is
 * resolved once, here, so the bars and the table below them cannot disagree about it.
 */
function dayRows(throughput: ThroughputDay[], classes: string[]): DayRow[] {
  return [...throughput]
    .sort((a, b) => a.day.localeCompare(b.day))
    .map(({ day, by_class }) => {
      const counts = classes.map((issueClass) => by_class[issueClass] ?? 0)
      return { day, counts, total: counts.reduce((sum, count) => sum + count, 0) }
    })
}

/** `2026-08-06` → `08-06`. Sliced rather than localised so the axis reads the same everywhere. */
function dayTick(day: string): string {
  return day.slice(5)
}

export default function Throughput({ state }: PanelProps) {
  if (!showData(state)) {
    return (
      <p className="slot-empty">
        {state.status === 'loading' ? 'Loading…' : 'No figures have arrived yet.'}
      </p>
    )
  }

  const { funnel, throughput } = state.data
  const classes = classesIn(throughput)
  const rows = dayRows(throughput, classes)
  const shown = rows.reduce((sum, row) => sum + row.total, 0)

  if (shown === 0) {
    // There is no chart to draw either way, but what the panel says is the funnel's to decide: a
    // series that arrived empty while the funnel counts merges is a gap in this panel, not a quiet
    // window, and "nothing was merged" next to a KPI tile reading five would be the wrong one.
    return (
      <p className="slot-empty">
        {funnel.merged === 0
          ? 'Nothing was merged in this window.'
          : `The daily series is empty, though the funnel counts ${funnel.merged} merged.`}
      </p>
    )
  }

  const days = rows.length === 1 ? 'day' : 'days'

  return (
    <>
      <BarChart
        width={CHART_WIDTH}
        height={CHART_HEIGHT}
        data={rows}
        margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
      >
        <XAxis dataKey="day" tickFormatter={dayTick} tickLine={false} />
        <YAxis allowDecimals={false} width={32} tickLine={false} axisLine={false} />
        <Legend verticalAlign="bottom" height={24} />
        {classes.map((issueClass, index) => (
          <Bar
            key={issueClass}
            // Indexed rather than keyed by class name, so a class called `day` cannot collide with
            // the axis field.
            dataKey={(row: DayRow) => row.counts[index]}
            name={issueClass}
            stackId="merged"
            fill={seriesColor(index)}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
      {/* The stacked bars carry no labels — there is nowhere to put five of them in 240px — so the
          counts reach the DOM here instead. This is the fallback the Recharts decision record names,
          and it is what a screen reader gets in place of the chart. */}
      <table className="visually-hidden">
        <caption>Merged per day by issue class</caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            {classes.map((issueClass) => (
              <th key={issueClass} scope="col">
                {issueClass}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.day}>
              <th scope="row">{row.day}</th>
              {row.counts.map((count, index) => (
                <td key={classes[index]}>{count}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p style={CAPTION}>
        {shown === funnel.merged
          ? `${shown} merged across ${rows.length} ${days}`
          : `${shown} of ${funnel.merged} merged across ${rows.length} ${days}`}
      </p>
    </>
  )
}
