import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import FunnelPanel, { order, slot, title } from './Funnel'
import { isEmptySummary, type AnalyticsSummary, type SummaryState } from '../api'
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

/** Every string the chart drew, in document order. */
function chartText(container: HTMLElement): string[] {
  return [...container.querySelectorAll('svg text')].map((node) => node.textContent ?? '')
}

/** The bar labels — `count (share of labelled)` — and nothing else. */
function barLabels(container: HTMLElement): string[] {
  return chartText(container).filter((text) => /^\d+ \(-?\d+%\)$/.test(text))
}

describe('the panel contract', () => {
  it('mounts into the chart slot, after the KPI row and before throughput', () => {
    expect(slot).toBe('chart')
    expect(title).toBe('Funnel')
    expect(order).toBe(2)
  })

  it('draws no taller than the 240px the spec allows a chart', () => {
    const { container } = render(<FunnelPanel state={ready(summaryFixture)} />)

    const svg = container.querySelector('.recharts-wrapper > svg')
    expect(svg).not.toBeNull()
    expect(Number(svg?.getAttribute('height'))).toBeLessThanOrEqual(240)
  })

  it('uses the one accent colour, leaving the series palette to the stacked charts', () => {
    const { container } = render(<FunnelPanel state={ready(summaryFixture)} />)

    expect(container.innerHTML).toContain('var(--accent)')
    expect(container.innerHTML).not.toContain('var(--series-')
  })
})

describe('the five stages', () => {
  it('labels every stage in the order work passes through them', () => {
    const { container } = render(<FunnelPanel state={ready(summaryFixture)} />)

    const stages = chartText(container).filter((text) => !/\(/.test(text))
    expect(stages).toEqual(['Labelled', 'Session created', 'PR opened', 'CI green', 'Merged'])
  })

  it('shows each count with its share of labelled', () => {
    const { funnel } = summaryFixture
    const { container } = render(<FunnelPanel state={ready(summaryFixture)} />)

    // Shares are of `labelled`, not of the previous stage: 7 of 8 is 88%, 6 of 8 is 75%, 5 of 8
    // is 63%. A panel dividing by the previous stage would print 88%, 86%, 83%.
    expect(funnel.ci_green / funnel.labelled).toBeCloseTo(0.75, 6)
    expect(barLabels(container)).toEqual([
      '8 (100%)',
      '8 (100%)',
      '7 (88%)',
      '6 (75%)',
      '5 (63%)',
    ])
  })

  it('counts a second window, where the stages fall away faster', () => {
    const { container } = render(<FunnelPanel state={ready(busyWindowSummaryFixture)} />)

    expect(barLabels(container)).toEqual([
      '20 (100%)',
      '19 (95%)',
      '14 (70%)',
      '12 (60%)',
      '9 (45%)',
    ])
  })
})

describe('where work stops', () => {
  it('names the largest drop, taking the earliest of equal drops', () => {
    // 8 → 8 → 7 → 6 → 5: three drops of one, and the first is where work first stopped.
    render(<FunnelPanel state={ready(summaryFixture)} />)

    expect(
      screen.getByText('Largest drop: Session created → PR opened (1 lost)'),
    ).toBeInTheDocument()
  })

  it('names the biggest drop when one stage dominates', () => {
    // 20 → 19 → 14 → 12 → 9: drops of 1, 5, 2 and 3.
    render(<FunnelPanel state={ready(busyWindowSummaryFixture)} />)

    expect(screen.getByText('Largest drop: Session created → PR opened (5 lost)')).toBeInTheDocument()
  })

  it('says so when nothing was lost, rather than naming a drop of zero', () => {
    const clean: AnalyticsSummary = {
      ...summaryFixture,
      funnel: { labelled: 4, session_created: 4, pr_opened: 4, ci_green: 4, merged: 4 },
    }
    render(<FunnelPanel state={ready(clean)} />)

    expect(screen.getByText('No work was lost between stages.')).toBeInTheDocument()
    expect(screen.queryByText(/Largest drop/)).not.toBeInTheDocument()
  })
})

describe('the empty window', () => {
  it('explains the empty window instead of drawing five bars of zero', () => {
    const { container } = render(<FunnelPanel state={ready(emptySummaryFixture)} />)

    expect(screen.getByText('No issues were labelled in this window.')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
  })

  it('shows no NaN and no Infinity where every share divides by zero', () => {
    const { container } = render(<FunnelPanel state={ready(emptySummaryFixture)} />)

    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    expect(container.textContent?.trim()).not.toBe('')
  })
})

describe('loading and error', () => {
  it('says it is loading before the first payload arrives', () => {
    const { container } = render(<FunnelPanel state={LOADING} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(container.querySelector('svg')).toBeNull()
  })

  it('keeps drawing the last good funnel while the API is unreachable', () => {
    const { container } = render(<FunnelPanel state={failed(summaryFixture)} />)

    expect(barLabels(container)).toEqual([
      '8 (100%)',
      '8 (100%)',
      '7 (88%)',
      '6 (75%)',
      '5 (63%)',
    ])
  })

  it('says so when the API failed before any payload arrived', () => {
    render(<FunnelPanel state={failed(null)} />)

    expect(screen.getByText('No figures have arrived yet.')).toBeInTheDocument()
  })
})
