import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import Kpi, { slot, title } from './Kpi'
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

/** A tile by its label, so an assertion cannot accidentally read the neighbouring figure. */
function tile(label: string): HTMLElement {
  return screen.getByRole('group', { name: label })
}

/** A tile's lines, so an assertion can say which line a claim was attached to. */
function tileLines(label: string): string[] {
  return [...tile(label).querySelectorAll('p')].map((line) => line.textContent ?? '')
}

describe('the panel contract', () => {
  it('mounts into the KPI slot with a title', () => {
    expect(slot).toBe('kpi')
    expect(title).toBe('Key metrics')
  })

  it('renders all four tiles itself, because the kpi region is a single column', () => {
    const { container } = render(<Kpi state={ready(summaryFixture)} />)

    expect(container.querySelectorAll('.kpi-tiles')).toHaveLength(1)
    expect(container.querySelectorAll('.kpi-tile')).toHaveLength(4)
  })
})

describe('the four figures', () => {
  it('shows success rate as merged / labelled', () => {
    const { funnel } = summaryFixture
    render(<Kpi state={ready(summaryFixture)} />)

    // 5 / 8 = 62.5%, which the panel rounds to whole percent.
    expect(funnel.merged / funnel.labelled).toBeCloseTo(0.625, 6)
    expect(tile('Success rate')).toHaveTextContent('63%')
    expect(tile('Success rate')).toHaveTextContent('5 merged of 8 labelled')
  })

  it('shows merge rate as merged / pr_opened, separately from success rate', () => {
    const { funnel } = summaryFixture
    render(<Kpi state={ready(summaryFixture)} />)

    // 5 / 7 = 71.4%. The two rates differ, so a tile reading the wrong one is visible here.
    expect(funnel.merged / funnel.pr_opened).toBeCloseTo(0.714, 3)
    expect(tile('Merge rate')).toHaveTextContent('71%')
    expect(tile('Merge rate')).toHaveTextContent('5 merged of 7 opened')
    expect(tile('Merge rate')).not.toHaveTextContent('63%')
  })

  it('shows MTTR as the p50 of merged_at − labeled_at, as a duration', () => {
    render(<Kpi state={ready(summaryFixture)} />)

    // to_merge.p50 is 6480 seconds. A tile showing "6480" would be unreadable, and one showing
    // to_pr.p50 (1980s = 33m) would be a different metric entirely.
    expect(tile('MTTR p50')).toHaveTextContent('1h 48m')
    expect(tile('MTTR p50')).toHaveTextContent('p90 4h')
    expect(tile('MTTR p50')).not.toHaveTextContent('33m')
  })

  it('shows cost per fix in dollars, over the ACU figure it was priced from', () => {
    render(<Kpi state={ready(summaryFixture)} />)

    expect(tileLines('Cost per fix')).toEqual([
      'Cost per fix',
      '$27.60',
      '12.3 ACU per fix from Devin',
      'priced locally at $2.25 per ACU',
    ])
  })

  it('attributes only the ACU count to Devin, never the dollars', () => {
    // `cost.source` says where the ACU figure came from and nothing else: the dollars are always
    // acus × ACU_UNIT_COST_USD, and docs/09-operations.md has that constant set from the reader's
    // own contract. A provenance badge on the dollar line would be a claim Devin never made — and
    // one the cost panel contradicts on the same screen.
    render(<Kpi state={ready(summaryFixture)} />)

    const lines = tileLines('Cost per fix')
    expect(lines.filter((line) => line.includes('Devin'))).toEqual(['12.3 ACU per fix from Devin'])
    expect(lines.filter((line) => line.includes('$27.60'))).toEqual(['$27.60'])
    expect(lines).toContain('priced locally at $2.25 per ACU')
  })

  it('labels an ACU count Sentinel derived rather than one Devin reported', () => {
    render(<Kpi state={ready(busyWindowSummaryFixture)} />)

    expect(tile('Cost per fix')).toHaveTextContent('26.7 ACU per fix derived by Sentinel')
    expect(tile('Cost per fix')).not.toHaveTextContent('from Devin')
    // The conversion is local whichever way the ACU count was obtained.
    expect(tile('Cost per fix')).toHaveTextContent('priced locally at $2.25 per ACU')
  })
})

describe('formatting', () => {
  it('formats a second window, including durations that cross into days', () => {
    const { funnel, durations_seconds } = busyWindowSummaryFixture
    render(<Kpi state={ready(busyWindowSummaryFixture)} />)

    expect(funnel.merged / funnel.labelled).toBeCloseTo(0.45, 6)
    expect(tile('Success rate')).toHaveTextContent('45%')
    expect(funnel.merged / funnel.pr_opened).toBeCloseTo(0.643, 3)
    expect(tile('Merge rate')).toHaveTextContent('64%')

    // 93600s is 26 hours, and 172800s is exactly two days.
    expect(durations_seconds.to_merge.p50).toBe(26 * 3600)
    expect(tile('MTTR p50')).toHaveTextContent('1d 2h')
    expect(tile('MTTR p50')).toHaveTextContent('p90 2d')

    expect(tile('Cost per fix')).toHaveTextContent('$60.08')
    expect(tile('Cost per fix')).toHaveTextContent('26.7 ACU per fix')
  })

  it('never renders a raw ratio or a raw second count', () => {
    const { container } = render(<Kpi state={ready(summaryFixture)} />)

    expect(container.textContent).not.toContain('0.625')
    expect(container.textContent).not.toContain('6480')
    expect(container.textContent).not.toContain('14400')
  })
})

describe('the empty window', () => {
  it('renders an em dash for every figure, and explains each one', () => {
    render(<Kpi state={ready(emptySummaryFixture)} />)

    // The API sends zeros, but every figure here is a ratio or a percentile over a funnel stage
    // that is zero, so none of them exists. "0% success" would read as a failed pipeline.
    expect(tile('Success rate')).toHaveTextContent('—')
    expect(tile('Success rate')).toHaveTextContent('nothing labelled in this window')
    expect(tile('Merge rate')).toHaveTextContent('—')
    expect(tile('Merge rate')).toHaveTextContent('no pull requests opened')
    expect(tile('MTTR p50')).toHaveTextContent('—')
    expect(screen.getAllByText('nothing merged yet')).toHaveLength(2)
    // Nothing was priced, so the tile does not explain a conversion it did not make.
    expect(tileLines('Cost per fix')).toEqual(['Cost per fix', '—', 'nothing merged yet'])
  })

  it('shows no NaN, no Infinity and no blank tile', () => {
    const { container } = render(<Kpi state={ready(emptySummaryFixture)} />)

    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    for (const node of container.querySelectorAll('.kpi-tile')) {
      expect(node.textContent?.trim()).not.toBe('')
    }
  })

  it('keeps a figure whose own denominator is non-zero', () => {
    // Three issues labelled, none of which reached a pull request: the success rate is a real 0%,
    // while merge rate, MTTR and cost per fix have nothing to divide by.
    const stalled: AnalyticsSummary = {
      ...summaryFixture,
      funnel: { labelled: 3, session_created: 3, pr_opened: 0, ci_green: 0, merged: 0 },
      rates: { success: 0, merge: 0, autonomy: 0 },
    }
    render(<Kpi state={ready(stalled)} />)

    expect(tile('Success rate')).toHaveTextContent('0%')
    expect(tile('Success rate')).toHaveTextContent('0 merged of 3 labelled')
    expect(tile('Merge rate')).toHaveTextContent('—')
    expect(tile('MTTR p50')).toHaveTextContent('—')
    expect(tile('Cost per fix')).toHaveTextContent('—')
  })

  it('renders an em dash rather than a non-finite figure from the API', () => {
    const broken: AnalyticsSummary = {
      ...summaryFixture,
      rates: { ...summaryFixture.rates, success: Number.POSITIVE_INFINITY },
    }
    render(<Kpi state={ready(broken)} />)

    expect(tile('Success rate')).toHaveTextContent('—')
    expect(tile('Success rate')).not.toHaveTextContent('Infinity')
  })
})

describe('loading and error', () => {
  it('says it is loading before the first payload arrives', () => {
    const { container } = render(<Kpi state={LOADING} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(container.querySelectorAll('.kpi-tile')).toHaveLength(0)
  })

  it('keeps showing the last good figures while the API is unreachable', () => {
    // The shell holds the previous payload through a failed poll and says so in its banner; a
    // panel that blanked itself on `status === 'error'` would throw away usable figures.
    render(<Kpi state={failed(summaryFixture)} />)

    expect(tile('Success rate')).toHaveTextContent('63%')
    expect(tile('Cost per fix')).toHaveTextContent('$27.60')
  })

  it('says so when the API failed before any payload arrived', () => {
    render(<Kpi state={failed(null)} />)

    expect(screen.getByText('No figures have arrived yet.')).toBeInTheDocument()
  })
})
