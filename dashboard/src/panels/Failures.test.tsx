import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import Failures, { order, rankedFailures, slot, title, totalFailures } from './Failures'
import type { AnalyticsSummary, SummaryState } from '../api'
import { emptySummaryFixture, summaryFixture, unmergedSummaryFixture } from '../fixtures/summary'

function ready(summary: AnalyticsSummary): SummaryState {
  return { status: 'ready', data: summary, error: null, receivedAt: 1 }
}

const loading: SummaryState = { status: 'loading', data: null, error: null, receivedAt: null }

function errored(data: AnalyticsSummary | null): SummaryState {
  return { status: 'error', data, error: new Error('offline'), receivedAt: data ? 1 : null }
}

/** The headline count, which mixes a <strong> into its text. */
function summaryLine(): HTMLElement {
  const node = document.querySelector('[data-metric="failures_total"]')
  if (node === null) throw new Error('no [data-metric="failures_total"] was rendered')
  return node as HTMLElement
}

/** The body rows, as `[reason, count, issues]`. */
function rows(): string[][] {
  return within(screen.getByRole('table'))
    .getAllByRole('row')
    .slice(1)
    .map((row) => [
      within(row).getByRole('rowheader').textContent ?? '',
      ...within(row).getAllByRole('cell').map((cell) => cell.textContent ?? ''),
    ])
}

describe('the panel module', () => {
  it('self-registers as a chart panel', () => {
    expect(title).toBe('Failure breakdown')
    expect(slot).toBe('chart')
    expect(order).toBeTypeOf('number')
  })
})

describe('rankedFailures and totalFailures', () => {
  it('puts the largest bucket first and breaks ties by reason', () => {
    const buckets = [
      { reason: 'requires_upstream_decision', count: 1, issues: [37] },
      { reason: 'max_fix_cycles_exceeded', count: 4, issues: [41, 42, 43, 44] },
      { reason: 'devin_session_failed', count: 1, issues: [50] },
    ]

    expect(rankedFailures(buckets).map((bucket) => bucket.reason)).toEqual([
      'max_fix_cycles_exceeded',
      'devin_session_failed',
      'requires_upstream_decision',
    ])
    // The API's array is not reordered in place; other panels read the same payload.
    expect(buckets[0].reason).toBe('requires_upstream_decision')
  })

  it('adds the buckets up', () => {
    expect(
      totalFailures([
        { reason: 'a', count: 2, issues: [1, 2] },
        { reason: 'b', count: 3, issues: [3, 4, 5] },
      ]),
    ).toBe(5)
    expect(totalFailures([])).toBe(0)
  })
})

describe('rendering a representative payload', () => {
  it('counts the blocked and failed remediations against the labelled total', () => {
    render(<Failures state={ready(summaryFixture)} />)

    expect(summaryLine()).toHaveTextContent('1 of 8 labelled issues ended blocked or failed')
  })

  it('counts against issues labelled, not against sessions that got started', () => {
    // Every shared fixture holds `labelled` and `session_created` equal, so nothing else in this
    // file can tell the two apart. They diverge constantly in production — an issue labelled whose
    // Devin session was never created is exactly what the funnel exists to expose — and "1 of 9
    // labelled" and "1 of 5 that reached a session" are different claims. Only the first is the
    // denominator docs/07-observability.md gives the failure breakdown.
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      funnel: { ...summaryFixture.funnel, labelled: 9, session_created: 5 },
    }
    render(<Failures state={ready(summary)} />)

    expect(summaryLine()).toHaveTextContent('1 of 9 labelled issues ended blocked or failed')
    expect(summaryLine().textContent).not.toContain('of 5')
  })

  it('lists each reason verbatim with its count and its issues', () => {
    render(<Failures state={ready(summaryFixture)} />)

    expect(rows()).toEqual([['requires_upstream_decision', '1', '#37']])
  })

  it('ranks the reasons and links every issue behind each one', () => {
    render(<Failures state={ready(unmergedSummaryFixture)} />)

    // Both buckets hold one, so the tie is broken by reason.
    expect(rows()).toEqual([
      ['max_fix_cycles_exceeded', '1', '#41'],
      ['requires_upstream_decision', '1', '#37'],
    ])
    expect(summaryLine()).toHaveTextContent('2 of 4 labelled issues ended blocked or failed')
  })

  it('lists several issue numbers under one reason', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      failures: [
        { reason: 'requires_upstream_decision', count: 1, issues: [37] },
        { reason: 'max_fix_cycles_exceeded', count: 3, issues: [41, 42, 43] },
      ],
    }
    render(<Failures state={ready(summary)} />)

    expect(rows()).toEqual([
      ['max_fix_cycles_exceeded', '3', '#41, #42, #43'],
      ['requires_upstream_decision', '1', '#37'],
    ])
    expect(summaryLine()).toHaveTextContent('4 of 8 labelled issues ended blocked or failed')
  })
})

describe('the two empty states, which mean different things', () => {
  it('reports nothing blocked or failed as the good news it is', () => {
    const summary: AnalyticsSummary = { ...summaryFixture, failures: [] }
    const { container } = render(<Failures state={ready(summary)} />)

    expect(container).not.toBeEmptyDOMElement()
    expect(screen.getByText(/Nothing was blocked or failed/)).toHaveTextContent(
      'Nothing was blocked or failed — all 8 labelled issues are still on a path through the pipeline.',
    )
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(container.textContent).not.toMatch(/NaN|Infinity/)
  })

  it('is the clean-record state, not the nothing-labelled one, when no session has started yet', () => {
    // Nine issues labelled and not one session created. The panel's guard is on `labelled`, so
    // this is "nothing has failed" — a real, early, healthy window. Read off `session_created` it
    // would become "no issues were labelled", which is false and hides nine waiting issues.
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      funnel: { labelled: 9, session_created: 0, pr_opened: 0, ci_green: 0, merged: 0 },
      failures: [],
    }
    render(<Failures state={ready(summary)} />)

    expect(screen.getByText(/Nothing was blocked or failed/)).toHaveTextContent(
      'Nothing was blocked or failed — all 9 labelled issues are still on a path through the pipeline.',
    )
    expect(
      screen.queryByText('No issues were labelled in this window, so nothing could fail yet.'),
    ).not.toBeInTheDocument()
  })

  it('does not read as a clean record when nothing was labelled at all', () => {
    const { container } = render(
      <Failures state={{ ...ready(emptySummaryFixture), status: 'empty' }} />,
    )

    expect(container).not.toBeEmptyDOMElement()
    expect(
      screen.getByText('No issues were labelled in this window, so nothing could fail yet.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Nothing was blocked or failed/)).not.toBeInTheDocument()
    expect(container.textContent).not.toMatch(/NaN|Infinity/)
  })
})

describe('loading and error states', () => {
  it('says it is loading before the first payload', () => {
    render(<Failures state={loading} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('says so when the API is unreachable and nothing has ever arrived', () => {
    render(<Failures state={errored(null)} />)

    expect(
      screen.getByText('No failure breakdown — the analytics API is unreachable.'),
    ).toBeInTheDocument()
  })

  it('keeps showing the last good breakdown while the API is unreachable', () => {
    render(<Failures state={errored(summaryFixture)} />)

    expect(rows()).toEqual([['requires_upstream_decision', '1', '#37']])
  })
})
