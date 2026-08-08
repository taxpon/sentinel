// Throughput: merged per day, stacked by issue class. The stack is the point — sustained capacity
// concentrated in one easy class is a different answer to "is this working?" than the same total
// spread across real problem types.

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

/** Ascending by day. The API is not specified to send them in order, and a bar chart implies one. */
function sortByDay(throughput: ThroughputDay[]): ThroughputDay[] {
  return [...throughput].sort((a, b) => a.day.localeCompare(b.day))
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

function mergedOn(day: ThroughputDay): number {
  return Object.values(day.by_class).reduce((total, count) => total + count, 0)
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

  const days = sortByDay(state.data.throughput)
  const total = days.reduce((sum, day) => sum + mergedOn(day), 0)
  if (total === 0) {
    return <p className="slot-empty">Nothing was merged in this window.</p>
  }

  const classes = classesIn(days)

  return (
    <>
      <BarChart
        width={CHART_WIDTH}
        height={CHART_HEIGHT}
        data={days}
        margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
      >
        <XAxis dataKey="day" tickFormatter={dayTick} tickLine={false} />
        <YAxis allowDecimals={false} width={32} tickLine={false} axisLine={false} />
        <Legend verticalAlign="bottom" height={24} />
        {classes.map((issueClass, index) => (
          <Bar
            key={issueClass}
            // A function key rather than a flattened row, so a class named `day` cannot collide
            // with the axis field. A day missing a class merged none of it, which is a zero.
            dataKey={(day: ThroughputDay) => day.by_class[issueClass] ?? 0}
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
          {days.map((day) => (
            <tr key={day.day}>
              <th scope="row">{day.day}</th>
              {classes.map((issueClass) => (
                <td key={issueClass}>{day.by_class[issueClass] ?? 0}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p style={CAPTION}>
        {total} merged across {days.length} {days.length === 1 ? 'day' : 'days'}
      </p>
    </>
  )
}
