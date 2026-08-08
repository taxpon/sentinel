import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import Cost, { order, slot, title } from './Cost'
import type { AnalyticsSummary, SummaryState } from '../api'
import { emptySummaryFixture, summaryFixture, unmergedSummaryFixture } from '../fixtures/summary'

/**
 * Expected strings are worked out from docs/07-observability.md by hand — 12.3 ACU at $2.25 is the
 * definition of cost per fix, and $27.60 is what two decimal places of 27.6 look like — rather than
 * by calling the formatters the panel calls.
 */
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

describe('the panel module', () => {
  it('self-registers as a chart panel', () => {
    expect(title).toBe('Cost per fix')
    expect(slot).toBe('chart')
    expect(order).toBeTypeOf('number')
  })
})

describe('rendering a representative payload', () => {
  it('shows cost per fix, ACU spend and ACUs per fix', () => {
    render(<Cost state={ready(summaryFixture)} />)

    expect(metric('usd_per_fix')).toHaveTextContent('$27.60')
    expect(metric('acus_total')).toHaveTextContent('61.4')
    expect(metric('acus_per_merged_fix')).toHaveTextContent('12.3')
    expect(metric('unit_cost_usd')).toHaveTextContent('$2.25')
  })

  it('states the formula and the denominator behind cost per fix', () => {
    // docs/07: Cost per fix = ACU per merged fix × ACU_UNIT_COST_USD, over the merged count.
    render(<Cost state={ready(summaryFixture)} />)

    expect(
      screen.getByText('per merged fix — 12.3 ACU × $2.25 per ACU, over 5 merged'),
    ).toBeInTheDocument()
  })

  it('renders currency to two decimals and ACUs to one, whatever the payload carries', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      cost: {
        acus_total: 400,
        acus_per_merged_fix: 80,
        usd_per_fix: 240,
        unit_cost_usd: 3,
        source: 'devin_consumption_api',
      },
    }
    render(<Cost state={ready(summary)} />)

    expect(metric('usd_per_fix')).toHaveTextContent('$240.00')
    expect(metric('unit_cost_usd')).toHaveTextContent('$3.00')
    expect(metric('acus_total')).toHaveTextContent('400.0')
    expect(metric('acus_per_merged_fix')).toHaveTextContent('80.0')
  })
})

describe('provenance', () => {
  it('says so when the ACU counts came from Devin', () => {
    render(<Cost state={ready(summaryFixture)} />)

    const label = document.querySelector('[data-provenance]')
    expect(label).toHaveAttribute('data-provenance', 'devin_consumption_api')
    expect(label).toHaveTextContent("ACU counts come from Devin's consumption API.")
  })

  it('says so when the ACU counts were derived locally instead', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      cost: { ...summaryFixture.cost, source: 'derived' },
    }
    render(<Cost state={ready(summary)} />)

    const label = document.querySelector('[data-provenance]')
    expect(label).toHaveAttribute('data-provenance', 'derived')
    expect(label).toHaveTextContent(
      "ACU counts are derived from Sentinel's own acu_ledger — Devin's consumption API was not available.",
    )
  })

  it('attributes the dollars to the local unit cost even when Devin supplied the ACUs', () => {
    // The trap this panel exists to avoid: `source: devin_consumption_api` says where the ACU
    // counts came from, and nothing more. ACU_UNIT_COST_USD is a locally configured contract price
    // (docs/09-operations.md), so no dollar figure here was ever reported by Devin.
    render(<Cost state={ready(summaryFixture)} />)

    expect(document.querySelector('[data-provenance]')).toHaveTextContent(
      "Dollars are Sentinel's own conversion at $2.25 per ACU (ACU_UNIT_COST_USD), a locally configured contract price — not a figure Devin reported.",
    )
  })

  it('names the unit cost it actually used, not the default', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      cost: { ...summaryFixture.cost, unit_cost_usd: 4.5 },
    }
    render(<Cost state={ready(summary)} />)

    expect(document.querySelector('[data-provenance]')).toHaveTextContent('$4.50 per ACU')
  })
})

describe('figures with no denominator', () => {
  it('blanks cost per fix when nothing merged, while still reporting the spend', () => {
    render(<Cost state={ready(unmergedSummaryFixture)} />)

    // 24.5 ACUs were spent and nothing merged. "$0.00 per fix" would read as free.
    expect(metric('acus_total')).toHaveTextContent('24.5')
    expect(metric('usd_per_fix')).toHaveTextContent('—')
    expect(metric('acus_per_merged_fix')).toHaveTextContent('—')
    expect(
      screen.getByText(
        'per merged fix — nothing merged in this window, so there is no cost per fix yet',
      ),
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('$0.00')
  })

  it('renders an empty window without NaN, Infinity or a blank box', () => {
    const { container } = render(
      <Cost state={{ ...ready(emptySummaryFixture), status: 'empty' }} />,
    )

    expect(container).not.toBeEmptyDOMElement()
    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    expect(metric('usd_per_fix')).toHaveTextContent('—')
    expect(metric('acus_per_merged_fix')).toHaveTextContent('—')
    // Zero ACUs really were spent, so that one is a measurement rather than a missing value.
    expect(metric('acus_total')).toHaveTextContent('0.0')
    // The unit cost is configuration; it exists whether or not anything ran.
    expect(metric('unit_cost_usd')).toHaveTextContent('$2.25')
    expect(document.querySelector('[data-provenance]')).toHaveAttribute(
      'data-provenance',
      'derived',
    )
  })

  it('shows an em dash rather than NaN if the API sends a non-finite figure', () => {
    const summary: AnalyticsSummary = {
      ...summaryFixture,
      cost: { ...summaryFixture.cost, usd_per_fix: Number.NaN, acus_total: Number.POSITIVE_INFINITY },
    }
    const { container } = render(<Cost state={ready(summary)} />)

    expect(container.textContent).not.toMatch(/NaN|Infinity/)
    expect(metric('usd_per_fix')).toHaveTextContent('—')
    expect(metric('acus_total')).toHaveTextContent('—')
  })
})

describe('loading and error states', () => {
  it('says it is loading before the first payload', () => {
    render(<Cost state={loading} />)

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('says so when the API is unreachable and nothing has ever arrived', () => {
    render(<Cost state={errored(null)} />)

    expect(
      screen.getByText('No cost figures — the analytics API is unreachable.'),
    ).toBeInTheDocument()
  })

  it('keeps showing the last good figures, with their provenance, while the API is unreachable', () => {
    render(<Cost state={errored(summaryFixture)} />)

    expect(metric('usd_per_fix')).toHaveTextContent('$27.60')
    expect(document.querySelector('[data-provenance]')).toHaveAttribute(
      'data-provenance',
      'devin_consumption_api',
    )
  })
})
