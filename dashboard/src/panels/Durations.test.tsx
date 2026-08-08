import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import Durations, { durationRows, order, slot, title } from './Durations'
import type { AnalyticsSummary, SummaryState } from '../api'
import {
  emptySummaryFixture,
  summaryFixture,
  unmergedSummaryFixture,
} from '../fixtures/summary'

/**
 * Every expected string in this file is worked out from the definitions in
 * docs/07-observability.md by hand — 1980 seconds is 33 minutes — rather than by calling the same
 * formatter the panel calls. A test that reuses the implementation cannot fail when it is wrong.
 */
function ready(summary: AnalyticsSummary): SummaryState {
  return { status: 'ready', data: summary, error: null, receivedAt: 1 }
}

const loading: SummaryState = { status: 'loading', data: null, error: null, receivedAt: null }

function errored(data: AnalyticsSummary | null): SummaryState {
  return { status: 'error', data, error: new Error('offline'), receivedAt: data ? 1 : null }
}

describe('the panel module', () => {
  it('self-registers as a chart panel', () => {
    expect(title).toBe('Duration distribution')
    expect(slot).toBe('chart')
    expect(order).toBeTypeOf('number')
  })
})

describe('durationRows', () => {
  it('pairs each percentile with the funnel stage that produced its samples', () => {
    const rows = durationRows(summaryFixture)

    expect(rows.map((row) => row.key)).toEqual(['to_pr', 'to_merge', 'review_latency'])
    // to-PR is measured over opened pull requests; both merge-relative figures over merges.
    expect(rows.map((row) => row.sampleCount)).toEqual([
      summaryFixture.funnel.pr_opened,
      summaryFixture.funnel.merged,
      summaryFixture.funnel.merged,
    ])
    expect(rows.map((row) => [row.p50, row.p90])).toEqual([
      [1980, 3600],
      [6480, 14400],
      [2700, 7200],
    ])
  })
})

describe('rendering a representative payload', () => {
  it('draws all three metrics with p50 and p90 as human durations', () => {
    render(<Durations state={ready(summaryFixture)} />)

    expect(screen.getByText('Time to PR')).toBeInTheDocument()
    expect(screen.getByText('MTTR')).toBeInTheDocument()
    expect(screen.getByText('Review latency')).toBeInTheDocument()

    // 1980s = 33m, 3600s = 1h; 6480s = 1h 48m, 14400s = 4h; 2700s = 45m, 7200s = 2h.
    for (const label of ['33m', '1h', '1h 48m', '4h', '45m', '2h']) {
      expect(screen.getByText(label), `${label} is not drawn`).toBeInTheDocument()
    }

    // Never the raw seconds.
    expect(screen.queryByText('1980')).not.toBeInTheDocument()
    expect(screen.queryByText('6480')).not.toBeInTheDocument()
  })

  it('distinguishes the two percentiles', () => {
    render(<Durations state={ready(summaryFixture)} />)

    expect(screen.getByText('p50')).toBeInTheDocument()
    expect(screen.getByText('p90')).toBeInTheDocument()
  })

  it('formats a sub-minute and a multi-day duration', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      durations_seconds: {
        to_pr: { p50: 45, p90: 90 },
        to_merge: { p50: 172_800, p90: 194_400 },
        review_latency: { p50: 59, p90: 3661 },
      },
    }
    render(<Durations state={ready(summary)} />)

    expect(screen.getByText('45s')).toBeInTheDocument() // under a minute stays in seconds
    expect(screen.getByText('1m')).toBeInTheDocument() // 90s rounds down to whole minutes
    expect(screen.getByText('59s')).toBeInTheDocument() // the second before it becomes a minute
    expect(screen.getByText('1h 1m')).toBeInTheDocument() // 3661s
    expect(screen.getByText('2d')).toBeInTheDocument() // 172800s, exactly two days
    expect(screen.getByText('2d 6h')).toBeInTheDocument() // 194400s
  })
})

describe('figures with no sample behind them', () => {
  it('blanks the merge-relative metrics when nothing merged, and says why', () => {
    render(<Durations state={ready(unmergedSummaryFixture)} />)

    // Two pull requests were opened, so to-PR is a real measurement and is drawn: 2400s = 40m.
    expect(screen.getByText('Time to PR')).toBeInTheDocument()
    expect(screen.getByText('40m')).toBeInTheDocument()

    // Nothing merged, so the API's zero is not a duration.
    const mttr = document.querySelector('[data-duration="to_merge"]')
    expect(mttr).toHaveTextContent('MTTR — nothing merged in this window')
    expect(document.querySelector('[data-duration="review_latency"]')).toHaveTextContent(
      'Review latency — nothing merged in this window',
    )

    // The trap: a percentile over an empty sample arrives as 0 and must not be drawn as "0s".
    expect(screen.queryByText('0s')).not.toBeInTheDocument()
  })

  it('shows no figures at all, and no NaN, for an empty window', () => {
    const { container } = render(<Durations state={{ ...ready(emptySummaryFixture), status: 'empty' }} />)

    expect(
      screen.getByText(
        'No remediation has reached a pull request in this window, so there is nothing to time yet.',
      ),
    ).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    expect(screen.queryByText('0s')).not.toBeInTheDocument()
    expect(container).not.toBeEmptyDOMElement()
  })
})

describe('loading and error states', () => {
  it('says it is loading before the first payload', () => {
    const { container } = render(<Durations state={loading} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/NaN|—/)
  })

  it('says so when the API is unreachable and nothing has ever arrived', () => {
    render(<Durations state={errored(null)} />)

    expect(screen.getByText('No durations — the analytics API is unreachable.')).toBeInTheDocument()
  })

  it('keeps drawing the last good figures while the API is unreachable', () => {
    // api.ts fixes this for all nine panels: draw whenever `showData(state)`, not when status is
    // 'ready'. A failed poll annotates the dashboard; it does not blank it.
    render(<Durations state={errored(summaryFixture)} />)

    expect(screen.getByText('1h 48m')).toBeInTheDocument()
    expect(screen.queryByText(/unreachable/)).not.toBeInTheDocument()
  })
})
