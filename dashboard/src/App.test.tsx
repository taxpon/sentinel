import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App, { DISCOVERED_PANELS, isPanelModule, type PanelRegistry } from './App'
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
  vi.restoreAllMocks()
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

describe('panel discovery', () => {
  it('accepts a well-formed module and rejects the ways one is malformed', () => {
    const Component = () => null

    expect(isPanelModule({ default: Component, slot: 'kpi', title: 'KPI' })).toBe(true)
    expect(isPanelModule({ default: Component, slot: 'kpi', title: 'KPI', order: 2 })).toBe(true)

    // The two shapes that type-check clean and then silently fail to appear: no `slot` export at
    // all, and a `slot` that is a plain string the union would have rejected if it were annotated.
    expect(isPanelModule({ default: Component, title: 'KPI' })).toBe(false)
    expect(isPanelModule({ default: Component, slot: 'charts', title: 'KPI' })).toBe(false)

    expect(isPanelModule({ slot: 'kpi', title: 'KPI' })).toBe(false)
    expect(isPanelModule({ default: Component, slot: 'kpi' })).toBe(false)
    expect(isPanelModule(undefined)).toBe(false)
    expect(isPanelModule('./panels/Kpi.tsx')).toBe(false)
  })

  it('discovers only well-formed panel modules, and no test files', () => {
    // Walks what the glob actually resolved. Vacuous until T31-T33 add files, and a failing guard
    // for each of them from the moment they do.
    for (const [path, module] of Object.entries(DISCOVERED_PANELS)) {
      expect(path, `${path} is a test file and must not be mounted as a panel`).not.toMatch(
        /\.test\.tsx$/,
      )
      expect(isPanelModule(module), `${path} does not export a component, a title and a slot`).toBe(
        true,
      )
    }
  })

  it('names a module it cannot mount instead of dropping it', async () => {
    await renderApp({
      fetcher: resolving(),
      panels: {
        './panels/Kpi.tsx': panel('KPI', 'kpi'),
        './panels/Broken.tsx': { default: () => null, title: 'Broken' } as unknown as PanelModule,
        './panels/Typo.tsx': {
          default: () => null,
          slot: 'charts',
          title: 'Typo',
        } as unknown as PanelModule,
      },
    })

    const alert = screen.getByText(/panels could not be mounted/)
    expect(alert).toHaveTextContent('./panels/Broken.tsx')
    expect(alert).toHaveTextContent('./panels/Typo.tsx')
    // The working panel is unaffected.
    expect(screen.getByLabelText('Key metrics')).toHaveTextContent('KPI sees ready')
  })
})

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

  it('contains a panel that throws, and keeps the rest of the dashboard up', async () => {
    // React logs the caught error; the test asserts the containment, not the console.
    vi.spyOn(console, 'error').mockImplementation(() => {})

    const exploding: PanelModule = {
      default: () => {
        throw new Error('cannot read mean of undefined')
      },
      slot: 'chart',
      title: 'Autonomy',
    }

    await renderApp({
      fetcher: resolving(),
      panels: {
        './panels/Autonomy.tsx': exploding,
        './panels/Funnel.tsx': panel('Funnel', 'chart'),
        './panels/Kpi.tsx': panel('KPI', 'kpi'),
      },
    })

    expect(screen.getByText('Autonomy panel failed.')).toBeInTheDocument()
    // Everything the reader needs to tell whether the pipeline is running is still there.
    expect(screen.getByRole('heading', { level: 1, name: 'Sentinel' })).toBeInTheDocument()
    expect(document.querySelector('.freshness')).toHaveTextContent('updated 0s ago')
    expect(screen.getByLabelText('Charts')).toHaveTextContent('Funnel sees ready')
    expect(screen.getByLabelText('Key metrics')).toHaveTextContent('KPI sees ready')
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

  it('announces staleness once, rather than announcing every tick of the clock', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(summaryFixture)
      .mockRejectedValue(new Error('offline'))
    await renderApp({ fetcher })

    // The visible label changes every second, so it must not be a live region itself.
    expect(indicator()).not.toHaveAttribute('role', 'status')
    const live = screen.getByRole('status')
    expect(live).toBeEmptyDOMElement()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(31_000)
    })
    expect(live).toHaveTextContent('Analytics data is out of date.')

    // Still one message a second later, not a new one per tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(live).toHaveTextContent('Analytics data is out of date.')
  })

  it('goes amber on the local clock when the API timestamp is in the future', async () => {
    // A payload stamped five minutes ahead, and then the poller dies.
    const skewed = { ...summaryFixture, generated_at: new Date(GENERATED_AT + 300_000).toISOString() }
    const fetcher = vi.fn().mockResolvedValueOnce(skewed).mockRejectedValue(new Error('offline'))
    await renderApp({ fetcher })

    expect(indicator()).toHaveTextContent('updated 0s ago')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000)
    })

    expect(indicator()).toHaveTextContent('updated 45s ago')
    expect(indicator()).toHaveAttribute('data-stale', 'true')
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
