import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import ThroughputPanel, { order, slot, title } from './Throughput'
import { isEmptySummary, seriesColor, type AnalyticsSummary, type SummaryState } from '../api'
import {
  busyWindowSummaryFixture,
  emptySummaryFixture,
  summaryFixture,
} from '../fixtures/summary'

function ready(data: AnalyticsSummary): SummaryState {
  return {
    status: isEmptySummary(data) ? 'empty' : 'ready',
    data,
    error: null,
    receivedAt: Date.now(),
  }
}

function failed(data: AnalyticsSummary | null): SummaryState {
  return {
    status: 'error',
    data,
    error: new Error('offline'),
    receivedAt: data === null ? null : Date.now(),
  }
}

const LOADING: SummaryState = { status: 'loading', data: null, error: null, receivedAt: null }

/** The chart itself. The legend swatches are `.recharts-surface` too, so take the wrapper's own. */
function chartSurface(container: HTMLElement): SVGSVGElement | null {
  return container.querySelector('.recharts-wrapper > svg')
}

/** The x axis ticks, in the order they were drawn. */
function dayTicks(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.recharts-xAxis-tick-labels text')].map(
    (node) => node.textContent ?? '',
  )
}

function legendEntries(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.recharts-legend-item-text')].map(
    (node) => node.textContent ?? '',
  )
}

/** The hidden data table, as a header row followed by one row per day. */
function tableRows(container: HTMLElement): string[][] {
  return [...container.querySelectorAll('table tr')].map((row) =>
    [...row.querySelectorAll('th, td')].map((cell) => cell.textContent ?? ''),
  )
}

describe('the panel contract', () => {
  it('mounts into the chart slot, after the funnel', () => {
    expect(slot).toBe('chart')
    expect(title).toBe('Throughput by day')
    expect(order).toBe(3)
  })

  it('draws no taller than the 240px the spec allows a chart', () => {
    const { container } = render(<ThroughputPanel state={ready(busyWindowSummaryFixture)} />)

    const svg = chartSurface(container)
    expect(svg).not.toBeNull()
    expect(Number(svg?.getAttribute('height'))).toBeLessThanOrEqual(240)
  })

  it('colours the classes from the series palette, not from the accent', () => {
    // The spec reserves the per-class palette for the stacked series and allows one accent colour
    // elsewhere; seriesColor() is the only way to reach it.
    const { container } = render(<ThroughputPanel state={ready(busyWindowSummaryFixture)} />)

    expect(container.innerHTML).toContain(seriesColor(0))
    expect(container.innerHTML).toContain(seriesColor(1))
    expect(container.innerHTML).toContain(seriesColor(2))
    expect(container.innerHTML).not.toContain('var(--accent)')
  })
})

describe('the daily series', () => {
  it('draws one bar group per day, in date order whatever order the API sent', () => {
    // The fixture lists 08-05 first; a chart that trusted the array would draw time backwards.
    expect(busyWindowSummaryFixture.throughput.map((day) => day.day)).toEqual([
      '2026-08-05',
      '2026-08-03',
      '2026-08-04',
    ])

    const { container } = render(<ThroughputPanel state={ready(busyWindowSummaryFixture)} />)

    expect(dayTicks(container)).toEqual(['08-03', '08-04', '08-05'])
  })

  it('stacks one series per issue class, in a stable order across days', () => {
    const { container } = render(<ThroughputPanel state={ready(busyWindowSummaryFixture)} />)

    // The union of the classes seen in the window, sorted — `dependency` merged on one day only
    // and still gets its own series and its own colour.
    expect(legendEntries(container)).toEqual(['dependency', 'flaky-test', 'security'])
    expect(container.querySelectorAll('.recharts-bar')).toHaveLength(3)
  })

  it('puts the counts in the DOM as a table, where the bars cannot carry labels', () => {
    const { container } = render(<ThroughputPanel state={ready(busyWindowSummaryFixture)} />)

    // A class absent from a day merged none of it that day, which is a zero rather than a gap.
    expect(tableRows(container)).toEqual([
      ['Day', 'dependency', 'flaky-test', 'security'],
      ['2026-08-03', '0', '1', '2'],
      ['2026-08-04', '0', '0', '2'],
      ['2026-08-05', '3', '1', '0'],
    ])
  })

  it('totals the merges it drew, and counts the days in singular and plural', () => {
    const total = busyWindowSummaryFixture.throughput.reduce(
      (sum, day) => sum + Object.values(day.by_class).reduce((a, b) => a + b, 0),
      0,
    )
    // The series accounts for every merge the funnel counted.
    expect(total).toBe(busyWindowSummaryFixture.funnel.merged)

    const { unmount } = render(<ThroughputPanel state={ready(busyWindowSummaryFixture)} />)
    expect(screen.getByText('9 merged across 3 days')).toBeInTheDocument()
    unmount()

    render(<ThroughputPanel state={ready(summaryFixture)} />)
    expect(screen.getByText('2 merged across 1 day')).toBeInTheDocument()
  })

  it('renders the single-day window from the spec', () => {
    const { container } = render(<ThroughputPanel state={ready(summaryFixture)} />)

    expect(dayTicks(container)).toEqual(['08-06'])
    expect(legendEntries(container)).toEqual(['flaky-test', 'security'])
    expect(tableRows(container)).toEqual([
      ['Day', 'flaky-test', 'security'],
      ['2026-08-06', '1', '1'],
    ])
  })
})

describe('the empty window', () => {
  it('explains an empty series instead of drawing an empty box', () => {
    const { container } = render(<ThroughputPanel state={ready(emptySummaryFixture)} />)

    expect(screen.getByText('Nothing was merged in this window.')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).not.toMatch(/NaN|Infinity/)
  })

  it('treats days that merged nothing as an empty window, not as a chart of zeros', () => {
    const nothingMerged: AnalyticsSummary = {
      ...summaryFixture,
      throughput: [
        { day: '2026-08-05', by_class: {} },
        { day: '2026-08-06', by_class: { security: 0 } },
      ],
    }
    render(<ThroughputPanel state={ready(nothingMerged)} />)

    expect(screen.getByText('Nothing was merged in this window.')).toBeInTheDocument()
  })
})

describe('loading and error', () => {
  it('says it is loading before the first payload arrives', () => {
    const { container } = render(<ThroughputPanel state={LOADING} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
  })

  it('keeps drawing the last good series while the API is unreachable', () => {
    const { container } = render(<ThroughputPanel state={failed(busyWindowSummaryFixture)} />)

    expect(dayTicks(container)).toEqual(['08-03', '08-04', '08-05'])
    expect(screen.getByText('9 merged across 3 days')).toBeInTheDocument()
  })

  it('says so when the API failed before any payload arrived', () => {
    render(<ThroughputPanel state={failed(null)} />)

    expect(screen.getByText('No figures have arrived yet.')).toBeInTheDocument()
  })
})
