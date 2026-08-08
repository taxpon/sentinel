import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App, { type PanelRegistry } from './App'
import {
  POLL_INTERVAL_MS,
  type PanelModule,
  type PanelProps,
  type PanelSlot,
  type SummaryFetcher,
} from './api'
import { emptySummaryFixture, summaryFixture } from './fixtures/summary'

const GENERATED_AT = Date.parse(summaryFixture.generated_at)

afterEach(() => {
  vi.useRealTimers()
})

/** Renders with the clock pinned to `generated_at`, so "updated Ns ago" is deterministic. */
async function renderApp(options: { fetcher: SummaryFetcher; panels?: PanelRegistry; now?: number }) {
  vi.useFakeTimers({ now: options.now ?? GENERATED_AT })
  const result = render(<App panels={options.panels ?? {}} options={{ fetcher: options.fetcher }} />)
  await act(async () => {})
  return result
}

const resolving = (): SummaryFetcher => vi.fn().mockResolvedValue(summaryFixture)

/** A stand-in for the panels T31-T33 will add: it renders the state it was handed. */
function panel(title: string, slot: PanelSlot, order?: number): PanelModule {
  return {
    default: ({ state }: PanelProps) => (
      <p>
        {title} sees {state.status}
      </p>
    ),
    slot,
    title,
    order,
  }
}

describe('the shell', () => {
  it('renders the three regions panels mount into', async () => {
    await renderApp({ fetcher: resolving() })

    expect(screen.getByRole('heading', { level: 1, name: 'Sentinel' })).toBeInTheDocument()
    expect(screen.getByLabelText('Key metrics')).toBeInTheDocument()
    expect(screen.getByLabelText('Charts')).toBeInTheDocument()
    expect(screen.getByLabelText('Live remediations')).toBeInTheDocument()
  })

  it('says a region is empty while no panel has been mounted into it', async () => {
    await renderApp({ fetcher: resolving() })

    expect(screen.getByText('No kpi panels are mounted yet.')).toBeInTheDocument()
    expect(screen.getByText('No chart panels are mounted yet.')).toBeInTheDocument()
    expect(screen.getByText('No table panels are mounted yet.')).toBeInTheDocument()
  })

  it('mounts discovered panels into their slot, in order, with the summary state', async () => {
    await renderApp({
      fetcher: resolving(),
      panels: {
        './panels/Throughput.tsx': panel('Throughput', 'chart', 2),
        './panels/Funnel.tsx': panel('Funnel', 'chart', 1),
        './panels/Kpi.tsx': panel('KPI', 'kpi'),
      },
    })

    const charts = screen.getByLabelText('Charts')
    expect(charts).toHaveTextContent('Funnel sees ready')
    const titles = [...charts.querySelectorAll('.panel__title')].map((node) => node.textContent)
    expect(titles).toEqual(['Funnel', 'Throughput'])

    expect(screen.getByLabelText('Key metrics')).toHaveTextContent('KPI sees ready')
    expect(screen.getByLabelText('Live remediations')).toHaveTextContent(
      'No table panels are mounted yet.',
    )
  })

  it('passes the loading, empty and error state through to the panels', async () => {
    const panels: PanelRegistry = { './panels/Kpi.tsx': panel('KPI', 'kpi') }

    const { unmount } = await renderApp({
      fetcher: vi.fn().mockResolvedValue(emptySummaryFixture),
      panels,
    })
    expect(screen.getByLabelText('Key metrics')).toHaveTextContent('KPI sees empty')
    unmount()

    await renderApp({ fetcher: vi.fn().mockRejectedValue(new Error('offline')), panels })
    expect(screen.getByLabelText('Key metrics')).toHaveTextContent('KPI sees error')
  })
})

describe('the status banner', () => {
  it('shows a loading message until the first response lands', async () => {
    vi.useFakeTimers({ now: GENERATED_AT })
    render(<App panels={{}} options={{ fetcher: () => new Promise(() => {}) }} />)

    expect(screen.getByText('Loading analytics…')).toBeInTheDocument()
  })

  it('says the API is unreachable, and that the figures shown are the last good ones', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(summaryFixture)
      .mockRejectedValue(new Error('offline'))
    await renderApp({ fetcher })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not reach the analytics API — showing the last successful update.',
    )
  })

  it('does not claim to be showing stale figures when there never were any', async () => {
    await renderApp({ fetcher: vi.fn().mockRejectedValue(new Error('offline')) })

    expect(screen.getByRole('alert')).toHaveTextContent('Could not reach the analytics API.')
  })

  it('explains an empty window instead of showing a blank dashboard', async () => {
    await renderApp({ fetcher: vi.fn().mockResolvedValue(emptySummaryFixture) })

    expect(screen.getByText('No issues were labelled in this window yet.')).toBeInTheDocument()
  })
})

describe('the freshness indicator', () => {
  function indicator() {
    return document.querySelector('.freshness') as HTMLElement
  }

  it('reports the age of the payload, not the age of the page', async () => {
    await renderApp({ fetcher: resolving(), now: GENERATED_AT + 5_000 })

    expect(indicator()).toHaveTextContent('updated 5s ago')
    expect(indicator()).toHaveAttribute('data-stale', 'false')
  })

  it('counts up between polls rather than waiting for the next one', async () => {
    await renderApp({ fetcher: resolving() })
    expect(indicator()).toHaveTextContent('updated 0s ago')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000)
    })

    expect(indicator()).toHaveTextContent('updated 3s ago')
  })

  it('turns amber once the payload is more than 30 seconds old', async () => {
    // The poll keeps failing, so `generated_at` stops advancing and the age keeps growing.
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(summaryFixture)
      .mockRejectedValue(new Error('offline'))
    await renderApp({ fetcher })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(indicator()).toHaveAttribute('data-stale', 'false')
    expect(indicator()).not.toHaveClass('freshness--stale')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(indicator()).toHaveTextContent('updated 31s ago')
    expect(indicator()).toHaveAttribute('data-stale', 'true')
    expect(indicator()).toHaveClass('freshness--stale')
  })

  it('says so, in amber, when no payload has ever arrived', async () => {
    await renderApp({ fetcher: vi.fn().mockRejectedValue(new Error('offline')) })

    expect(indicator()).toHaveTextContent('never updated')
    expect(indicator()).toHaveAttribute('data-stale', 'true')
  })

  it('stops its clock on unmount', async () => {
    const { unmount } = await renderApp({ fetcher: resolving() })
    unmount()

    // A leaked interval would call setState on an unmounted tree; vitest fails the run on the
    // resulting React warning only if it throws, so assert on the timer count directly.
    expect(vi.getTimerCount()).toBe(0)
  })
})
