import { act, renderHook } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  AnalyticsApiError,
  FRESHNESS_AMBER_AFTER_SECONDS,
  NO_VALUE,
  POLL_INTERVAL_MS,
  fetchRemediations,
  fetchSummary,
  formatDurationSeconds,
  formatNumber,
  formatPercent,
  freshness,
  isEmptySummary,
  parseSummary,
  seriesColor,
  showData,
  useAnalyticsSummary,
  useRemediations,
  type AnalyticsSummary,
  type RemediationRow,
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

describe('parseSummary', () => {
  it('accepts the payload from the spec', () => {
    expect(parseSummary(summaryFixture)).toEqual(summaryFixture)
  })

  it('names the fields a wrong-shaped payload is missing', () => {
    const { rates: _rates, cycles: _cycles, ...partial } = summaryFixture

    expect(() => parseSummary(partial)).toThrow(/missing rates, cycles/)
  })

  it('rejects a body that is not a JSON object at all', () => {
    // What a proxy or a login page returns: valid JSON, or an array, but not a summary.
    expect(() => parseSummary('<html>')).toThrow(/did not return a JSON object/)
    expect(() => parseSummary([])).toThrow(/did not return a JSON object/)
  })

  it('rejects a funnel it cannot read, since the empty state is decided from it', () => {
    expect(() => parseSummary({ ...summaryFixture, funnel: { labelled: '8' } })).toThrow(
      /numeric funnel.labelled/,
    )
  })

  it('surfaces a wrong shape through fetchSummary as an API error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"nope":1}', { status: 200 }))

    await expect(fetchSummary('7d', new AbortController().signal)).rejects.toBeInstanceOf(
      AnalyticsApiError,
    )
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

    expect(result.current).toEqual({
      status: 'loading',
      data: null,
      error: null,
      receivedAt: null,
    })

    await act(async () => {})
    expect(result.current.status).toBe('ready')
    expect(result.current.data).toEqual(summaryFixture)
    expect(result.current.receivedAt).toBe(generatedAt)
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

  it('never has more than one request in flight, however slow the API is', async () => {
    let inFlight = 0
    let peak = 0
    const resolvers: (() => void)[] = []
    const fetcher = vi.fn(async () => {
      peak = Math.max(peak, ++inFlight)
      await new Promise<void>((resolve) => resolvers.push(resolve))
      inFlight--
      return summaryFixture
    })

    pollingHarness(fetcher)

    // Four intervals pass while the first request has still not answered.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 4)
    })

    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(peak).toBe(1)

    // Once it answers, polling resumes on the next tick rather than firing the skipped ticks.
    await act(async () => {
      resolvers.forEach((resolve) => resolve())
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(peak).toBe(1)
  })

  it('never applies a stale response over a newer one', async () => {
    // The scenario overlap creates: the first poll is slow and answers with an older snapshot,
    // while the polls behind it are fast and current.
    const older: AnalyticsSummary = { ...summaryFixture, generated_at: '2026-08-08T04:00:00Z' }
    let call = 0
    const fetcher = vi.fn(async () => {
      call += 1
      if (call === 1) {
        await new Promise<void>((resolve) => setTimeout(resolve, 9_000))
        return older
      }
      return summaryFixture
    })

    vi.useFakeTimers({ now: generatedAt })
    const committed: number[] = []
    renderHook(() => {
      const state = useAnalyticsSummary({ fetcher })
      useEffect(() => {
        if (state.data) committed.push(Date.parse(state.data.generated_at))
      }, [state.data])
      return state
    })

    // One second at a time, so that every intermediate state commits and can be inspected;
    // advancing in one jump would let React coalesce a regression out of existence.
    for (let second = 0; second < 12; second++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
    }

    expect(committed.length).toBeGreaterThan(1)
    expect(committed).toEqual([...committed].sort((a, b) => a - b))
  })

  it('discards a response that arrives after the window changed', async () => {
    const otherWindow: AnalyticsSummary = { ...summaryFixture, generated_at: '2026-08-08T04:00:00Z' }
    const fetcher = vi.fn(async (window: string) => {
      if (window === '7d') {
        await new Promise<void>((resolve) => setTimeout(resolve, POLL_INTERVAL_MS * 2))
        return otherWindow
      }
      return summaryFixture
    })

    vi.useFakeTimers({ now: generatedAt })
    const { result, rerender } = renderHook(
      ({ window }: { window: string }) => useAnalyticsSummary({ window, fetcher }),
      { initialProps: { window: '7d' } },
    )

    rerender({ window: '30d' })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3)
    })

    expect(result.current.data!.generated_at).toBe(summaryFixture.generated_at)
  })
})

describe('useRemediations', () => {
  const rows: RemediationRow[] = [
    {
      id: 12,
      repo: 'taxpon/superset',
      issue_number: 42,
      issue_class: 'security',
      state: 'IN_REVIEW',
      cycle: 1,
      acus_consumed: 12.5,
      elapsed_seconds: 6480,
      devin_session_url: 'https://app.devin.ai/sessions/devin-1',
      pr_number: 7,
      pr_url: 'https://github.com/taxpon/superset/pull/7',
      blocked_reason: null,
      labeled_at: '2026-08-08T02:12:03Z',
    },
  ]

  it('polls the live table on the same 5-second loop', async () => {
    vi.useFakeTimers({ now: Date.parse(summaryFixture.generated_at) })
    const fetcher = vi.fn().mockResolvedValue(rows)
    const { result, unmount } = renderHook(() => useRemediations({ fetcher }))

    await act(async () => {})
    expect(result.current.status).toBe('ready')
    expect(result.current.data).toEqual(rows)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(fetcher).toHaveBeenCalledTimes(2)

    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('reports no remediations as empty rather than as a blank table', async () => {
    vi.useFakeTimers({ now: Date.parse(summaryFixture.generated_at) })
    const { result } = renderHook(() => useRemediations({ fetcher: vi.fn().mockResolvedValue([]) }))

    await act(async () => {})

    expect(result.current.status).toBe('empty')
    expect(result.current.data).toEqual([])
  })
})

describe('fetchRemediations', () => {
  it('returns the rows the API sent', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('[]', { status: 200 }))

    await expect(fetchRemediations(new AbortController().signal)).resolves.toEqual([])
    expect(fetchMock.mock.calls[0][0]).toBe('/api/remediations')
  })

  it('rejects a response that is not an array', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }))

    await expect(fetchRemediations(new AbortController().signal)).rejects.toThrow(
      'did not return an array',
    )
  })
})

describe('showData', () => {
  it('is true whenever a payload has arrived, including while the API is unreachable', () => {
    expect(showData({ status: 'loading', data: null, error: null, receivedAt: null })).toBe(false)
    expect(
      showData({ status: 'error', data: null, error: new Error('x'), receivedAt: null }),
    ).toBe(false)
    // The case the panels must not get wrong: an error over the top of a good payload.
    expect(
      showData({ status: 'error', data: summaryFixture, error: new Error('x'), receivedAt: 1 }),
    ).toBe(true)
    expect(showData({ status: 'empty', data: emptySummaryFixture, error: null, receivedAt: 1 })).toBe(
      true,
    )
  })
})

describe('freshness', () => {
  const generatedAt = '2026-08-08T04:12:03Z'
  const generated = Date.parse(generatedAt)

  it('renders the age in seconds', () => {
    expect(freshness(generatedAt, generated, generated).label).toBe('updated 0s ago')
    expect(freshness(generatedAt, generated + 5_000, generated).label).toBe('updated 5s ago')
    expect(freshness(generatedAt, generated + 59_000, generated).label).toBe('updated 59s ago')
  })

  it('switches to minutes and hours once seconds stop being useful', () => {
    expect(freshness(generatedAt, generated + 90_000, generated).label).toBe('updated 1m ago')
    expect(freshness(generatedAt, generated + 7_200_000, generated).label).toBe('updated 2h ago')
  })

  it('turns amber past 30 seconds, and not at 30', () => {
    const amberAt = FRESHNESS_AMBER_AFTER_SECONDS * 1000
    expect(freshness(generatedAt, generated + amberAt - 1000, generated).stale).toBe(false)
    expect(freshness(generatedAt, generated + amberAt, generated).stale).toBe(false)
    expect(freshness(generatedAt, generated + amberAt + 1000, generated).stale).toBe(true)
  })

  it('clamps a clock running behind the API rather than showing a negative age', () => {
    expect(freshness(generatedAt, generated - 10_000, generated - 10_000)).toMatchObject({
      ageSeconds: 0,
      label: 'updated 0s ago',
      stale: false,
    })
  })

  it('keeps ageing on the local clock when the API timestamp is in the future', () => {
    // The API's clock runs five minutes ahead, and the poller then dies. Trusting `generated_at`
    // alone would leave the indicator reading "updated 0s ago" in grey for those five minutes,
    // which is exactly the window in which someone needs to be told the pipeline stopped.
    const skewed = new Date(generated + 300_000).toISOString()
    const received = generated

    expect(freshness(skewed, generated + 10_000, received)).toMatchObject({
      ageSeconds: 10,
      stale: false,
    })
    expect(freshness(skewed, generated + 45_000, received)).toMatchObject({
      ageSeconds: 45,
      label: 'updated 45s ago',
      stale: true,
    })
  })

  it('takes the server age when it is the larger of the two', () => {
    // A payload the API already generated a minute before this browser received it.
    expect(freshness(generatedAt, generated + 61_000, generated + 60_000)).toMatchObject({
      ageSeconds: 61,
      stale: true,
    })
  })

  it('treats an unparseable timestamp with nothing received as stale', () => {
    expect(freshness('not a date', generated, null)).toMatchObject({
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

  it('never renders NaN or Infinity, which an empty window produces', () => {
    for (const format of [formatPercent, formatNumber, formatDurationSeconds]) {
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
