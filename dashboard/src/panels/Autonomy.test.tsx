import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import Autonomy, { cycleBars, order, slot, title } from './Autonomy'
import type { AnalyticsSummary, SummaryState } from '../api'
import { emptySummaryFixture, summaryFixture, unmergedSummaryFixture } from '../fixtures/summary'

function ready(summary: AnalyticsSummary): SummaryState {
  return { status: 'ready', data: summary, error: null, receivedAt: 1 }
}

const loading: SummaryState = { status: 'loading', data: null, error: null, receivedAt: null }

function errored(data: AnalyticsSummary | null): SummaryState {
  return { status: 'error', data, error: new Error('offline'), receivedAt: data ? 1 : null }
}

function metric(name: string): HTMLElement {
  const node = document.querySelector(`[data-metric="${name}"]`)
  if (node === null) throw new Error(`no [data-metric="${name}"] was rendered`)
  return node as HTMLElement
}

/** The cycle counts along the x axis, in the order they were drawn. */
function axisTicks(): (string | null)[] {
  return [...document.querySelectorAll('.recharts-cartesian-axis-tick-value tspan')].map(
    (node) => node.textContent,
  )
}

/** The count printed above each bar, in the order they were drawn. */
function barLabels(): (string | null)[] {
  return [...document.querySelectorAll('.recharts-label-list text tspan')].map(
    (node) => node.textContent,
  )
}

describe('the panel module', () => {
  it('self-registers as a chart panel', () => {
    expect(title).toBe('Autonomy')
    expect(slot).toBe('chart')
    expect(order).toBeTypeOf('number')
  })
})

describe('cycleBars', () => {
  it('orders the distribution by cycle count, not by the string the key arrived as', () => {
    // Sorted as text, 10 would sit between 1 and 2 — and the panel would claim most fixes needed
    // ten laps of self-correction.
    expect(cycleBars({ '2': 1, '10': 4, '0': 9, '1': 3 })).toEqual([
      { cycles: '0', count: 9 },
      { cycles: '1', count: 3 },
      { cycles: '2', count: 1 },
      { cycles: '10', count: 4 },
    ])
  })

  it('is empty when the distribution is', () => {
    expect(cycleBars({})).toEqual([])
  })
})

describe('rendering a representative payload', () => {
  it('states the autonomy rate as a percentage over the merged count', () => {
    render(<Autonomy state={ready(summaryFixture)} />)

    // docs/07: merged with cycle = 0 and human_message_count = 0, over merged. 0.6 -> 60%, of 5.
    expect(metric('autonomy_rate')).toHaveTextContent('60%')
    expect(
      screen.getByText(
        'of 5 merged fixes ran with cycle = 0 and human_message_count = 0 — no fix cycle, no human message',
      ),
    ).toBeInTheDocument()
  })

  it('names what a fix cycle is rather than leaving the mean unexplained', () => {
    render(<Autonomy state={ready(summaryFixture)} />)

    expect(metric('cycles_mean')).toHaveTextContent('0.8 fix cycles per remediation on average')
    expect(metric('cycles_mean')).toHaveTextContent(
      'a cycle is a return to RUNNING from CI_FAILED or CHANGES_REQUESTED',
    )
  })

  it('draws the cycle distribution in ascending cycle order', () => {
    render(<Autonomy state={ready(summaryFixture)} />)

    // { '0': 3, '1': 1, '2': 1 }
    expect(axisTicks()).toEqual(['0', '1', '2'])
    expect(barLabels()).toEqual(['3', '1', '1'])
  })

  it('rounds the rate rather than printing the raw fraction', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      rates: { ...summaryFixture.rates, autonomy: 0.3333333 },
    }
    render(<Autonomy state={ready(summary)} />)

    expect(metric('autonomy_rate')).toHaveTextContent('33%')
    expect(metric('autonomy_rate').textContent).not.toContain('0.3333')
  })

  it('renders a rate of one as 100%, not as 1', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      rates: { ...summaryFixture.rates, autonomy: 1 },
    }
    render(<Autonomy state={ready(summary)} />)

    expect(metric('autonomy_rate')).toHaveTextContent('100%')
  })
})

describe('figures with no denominator', () => {
  it('blanks the rate when nothing merged, instead of claiming zero autonomy', () => {
    render(<Autonomy state={ready(unmergedSummaryFixture)} />)

    expect(metric('autonomy_rate')).toHaveTextContent('—')
    expect(
      screen.getByText('nothing merged in this window, so there is no autonomy rate yet'),
    ).toBeInTheDocument()
    // "0% autonomous" would read as a system that always needed a person.
    expect(document.body.textContent).not.toContain('0%')

    // Cycles are counted over remediations, not merges, so the distribution is still real.
    expect(axisTicks()).toEqual(['1', '2'])
    expect(barLabels()).toEqual(['2', '2'])
    expect(metric('cycles_mean')).toHaveTextContent('1.5 fix cycles per remediation on average')
  })

  it('renders an empty window without NaN, Infinity or a blank box', () => {
    const { container } = render(
      <Autonomy state={{ ...ready(emptySummaryFixture), status: 'empty' }} />,
    )

    expect(container).not.toBeEmptyDOMElement()
    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    expect(metric('autonomy_rate')).toHaveTextContent('—')
    expect(metric('cycles_mean')).toHaveTextContent(
      'No fix-cycle distribution yet — no remediation has run in this window.',
    )
    expect(document.querySelector('.recharts-wrapper')).toBeNull()
    expect(container.textContent).not.toContain('0%')
  })

  it('shows an em dash rather than NaN if the API sends a non-finite rate', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      rates: { ...summaryFixture.rates, autonomy: Number.NaN },
      cycles: { ...summaryFixture.cycles, mean: Number.POSITIVE_INFINITY },
    }
    const { container } = render(<Autonomy state={ready(summary)} />)

    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    expect(metric('autonomy_rate')).toHaveTextContent('—')
    expect(metric('cycles_mean')).toHaveTextContent('— fix cycles per remediation on average')
  })
})

describe('loading and error states', () => {
  it('says it is loading before the first payload', () => {
    render(<Autonomy state={loading} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('says so when the API is unreachable and nothing has ever arrived', () => {
    render(<Autonomy state={errored(null)} />)

    expect(
      screen.getByText('No autonomy figures — the analytics API is unreachable.'),
    ).toBeInTheDocument()
  })

  it('keeps showing the last good figures while the API is unreachable', () => {
    render(<Autonomy state={errored(summaryFixture)} />)

    expect(metric('autonomy_rate')).toHaveTextContent('60%')
    expect(barLabels()).toEqual(['3', '1', '1'])
  })
})
