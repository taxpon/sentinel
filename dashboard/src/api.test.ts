import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  AnalyticsApiError,
  FRESHNESS_AMBER_AFTER_SECONDS,
  NO_VALUE,
  POLL_INTERVAL_MS,
  fetchSummary,
  formatDurationSeconds,
  formatNumber,
  formatPercent,
  formatUsd,
  freshness,
  isEmptySummary,
  seriesColor,
  useAnalyticsSummary,
  type AnalyticsSummary,
} from './api'
import { emptySummaryFixture, summaryFixture } from './fixtures/summary'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('fetchSummary', () => {
  it('requests the window it was given and returns the parsed payload', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify(summaryFixture), { status: 200 }))

    const summary = await fetchSummary('30d', new AbortController().signal)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/analytics/summary?window=30d')
    expect(summary).toEqual(summaryFixture)
  })

  it('throws with the status code when the API responds with an error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('nope', { status: 503 }))

    await expect(fetchSummary('7d', new AbortController().signal)).rejects.toThrow(
      AnalyticsApiError,
    )
    await expect(fetchSummary('7d', new AbortController().signal)).rejects.toMatchObject({
      status: 503,
    })
  })
})

describe('isEmptySummary', () => {
  it('is empty when nothing was labelled in the window', () => {
    expect(isEmptySummary(emptySummaryFixture)).toBe(true)
    expect(isEmptySummary(summaryFixture)).toBe(false)
  })
})

describe('useAnalyticsSummary', () => {
  const generatedAt = Date.parse(summaryFixture.generated_at)

  function pollingHarness(fetcher: (window: string, signal: AbortSignal) => Promise<AnalyticsSummary>) {
    vi.useFakeTimers({ now: generatedAt })
    return renderHook(() => useAnalyticsSummary({ fetcher }))
  }

  it('starts in the loading state and reaches ready with the payload', async () => {
    const fetcher = vi.fn().mockResolvedValue(summaryFixture)
    const { result } = pollingHarness(fetcher)

    expect(result.current).toEqual({ status: 'loading', data: null, error: null })

    await act(async () => {})
    expect(result.current.status).toBe('ready')
    expect(result.current.data).toEqual(summaryFixture)
  })

  it('reports the empty state for a window with nothing labelled', async () => {
    const { result } = pollingHarness(vi.fn().mockResolvedValue(emptySummaryFixture))

    await act(async () => {})

    expect(result.current.status).toBe('empty')
    expect(result.current.data).toEqual(emptySummaryFixture)
  })

  it('reports the error state, and keeps the last good payload', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(summaryFixture)
      .mockRejectedValue(new AnalyticsApiError('boom', 500))
    const { result } = pollingHarness(fetcher)

    await act(async () => {})
    expect(result.current.status).toBe('ready')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(result.current.status).toBe('error')
    expect(result.current.error).toBeInstanceOf(AnalyticsApiError)
    // A single failed poll must not blank a dashboard that was working a moment ago.
    expect(result.current.data).toEqual(summaryFixture)
  })

  it('has no payload to fall back on when the very first poll fails', async () => {
    const { result } = pollingHarness(vi.fn().mockRejectedValue(new Error('offline')))

    await act(async () => {})

    expect(result.current.status).toBe('error')
    expect(result.current.data).toBeNull()
  })

  it('polls every 5 seconds', async () => {
    const fetcher = vi.fn().mockResolvedValue(summaryFixture)
    pollingHarness(fetcher)

    await act(async () => {})
    expect(fetcher).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(fetcher).toHaveBeenCalledTimes(2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)
    })
    expect(fetcher).toHaveBeenCalledTimes(5)
  })

  it('stops polling and aborts the request in flight on unmount', async () => {
    const fetcher = vi.fn().mockResolvedValue(summaryFixture)
    const { unmount } = pollingHarness(fetcher)

    await act(async () => {})
    const signal: AbortSignal = fetcher.mock.calls[0][1]
    expect(signal.aborted).toBe(false)

    unmount()
    expect(signal.aborted).toBe(true)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 4)
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('does not restart polling when the caller passes a new fetcher on every render', async () => {
    vi.useFakeTimers({ now: generatedAt })
    const calls: string[] = []
    const { rerender } = renderHook(() =>
      useAnalyticsSummary({
        fetcher: async () => {
          calls.push('called')
          return summaryFixture
        },
      }),
    )

    await act(async () => {})
    rerender()
    rerender()
    // Re-subscribing on every render would poll far faster than every 5 seconds.
    expect(calls).toHaveLength(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(calls).toHaveLength(2)
  })
})

describe('freshness', () => {
  const generatedAt = '2026-08-08T04:12:03Z'
  const generated = Date.parse(generatedAt)

  it('renders the age in seconds', () => {
    expect(freshness(generatedAt, generated).label).toBe('updated 0s ago')
    expect(freshness(generatedAt, generated + 5_000).label).toBe('updated 5s ago')
    expect(freshness(generatedAt, generated + 59_000).label).toBe('updated 59s ago')
  })

  it('switches to minutes and hours once seconds stop being useful', () => {
    expect(freshness(generatedAt, generated + 90_000).label).toBe('updated 1m ago')
    expect(freshness(generatedAt, generated + 7_200_000).label).toBe('updated 2h ago')
  })

  it('turns amber past 30 seconds, and not at 30', () => {
    const amberAt = FRESHNESS_AMBER_AFTER_SECONDS * 1000
    expect(freshness(generatedAt, generated + amberAt - 1000).stale).toBe(false)
    expect(freshness(generatedAt, generated + amberAt).stale).toBe(false)
    expect(freshness(generatedAt, generated + amberAt + 1000).stale).toBe(true)
  })

  it('clamps a clock running behind the API rather than showing a negative age', () => {
    expect(freshness(generatedAt, generated - 10_000)).toMatchObject({
      ageSeconds: 0,
      label: 'updated 0s ago',
      stale: false,
    })
  })

  it('treats an unparseable timestamp as stale', () => {
    expect(freshness('not a date', generated)).toMatchObject({
      label: 'never updated',
      stale: true,
    })
  })
})

describe('formatting', () => {
  it('formats percentages from the rates in the payload', () => {
    expect(formatPercent(summaryFixture.rates.success)).toBe('63%')
    expect(formatPercent(summaryFixture.rates.merge, 1)).toBe('71.4%')
    expect(formatPercent(0)).toBe('0%')
  })

  it('formats seconds as a human duration', () => {
    expect(formatDurationSeconds(45)).toBe('45s')
    expect(formatDurationSeconds(summaryFixture.durations_seconds.to_pr.p50)).toBe('33m')
    expect(formatDurationSeconds(summaryFixture.durations_seconds.to_merge.p50)).toBe('1h 48m')
    expect(formatDurationSeconds(7200)).toBe('2h')
    expect(formatDurationSeconds(90_000)).toBe('1d 1h')
    expect(formatDurationSeconds(0)).toBe('0s')
  })

  it('formats currency to the cent', () => {
    expect(formatUsd(summaryFixture.cost.usd_per_fix)).toBe('$27.60')
    expect(formatUsd(0)).toBe('$0.00')
  })

  it('never renders NaN or Infinity, which an empty window produces', () => {
    for (const format of [formatPercent, formatUsd, formatNumber, formatDurationSeconds]) {
      expect(format(Number.NaN)).toBe(NO_VALUE)
      expect(format(Number.POSITIVE_INFINITY)).toBe(NO_VALUE)
    }
  })
})

describe('seriesColor', () => {
  it('gives each class its own colour and wraps rather than running out', () => {
    expect(seriesColor(0)).toBe('var(--series-1)')
    expect(seriesColor(5)).toBe('var(--series-6)')
    expect(seriesColor(6)).toBe('var(--series-1)')
  })
})
