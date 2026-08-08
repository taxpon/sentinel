import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  POLL_INTERVAL_MS,
  type RemediationEvent,
  type RemediationRow,
  type RemediationsFetcher,
} from '../api'
import { remediationRowsFixture, remediationTimelineFixture } from '../fixtures/summary'
import Timeline, { orderEvents, slot, title, type TimelineFetcher } from './Timeline'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const ONLY_RUNNING: RemediationRow[] = remediationRowsFixture.filter(
  (candidate) => candidate.id === 12,
)

/** The one event the fixture does not cover: a remediation that has only just been queued. */
function queuedEvent(remediationId: number): RemediationEvent {
  return {
    id: 200 + remediationId,
    remediation_id: remediationId,
    from_state: null,
    to_state: 'QUEUED',
    kind: 'transition',
    detail: null,
    created_at: '2026-08-08T03:40:00Z',
  }
}

const rowsResolving = (rows: RemediationRow[] = remediationRowsFixture): RemediationsFetcher =>
  vi.fn().mockResolvedValue(rows)

/** Remediation 12 has the fixture's history; anything else has just been queued. */
const timelineResolving: TimelineFetcher = (id) =>
  Promise.resolve(id === 12 ? remediationTimelineFixture : [queuedEvent(id)])

async function renderTimeline(options: {
  rows?: RemediationsFetcher
  fetcher?: TimelineFetcher
} = {}) {
  vi.useFakeTimers()
  const result = render(
    <Timeline
      options={{ fetcher: options.rows ?? rowsResolving(ONLY_RUNNING) }}
      fetcher={options.fetcher ?? timelineResolving}
    />,
  )
  await act(async () => {})
  return result
}

/** The event ids in the order they were drawn. */
function renderedEventIds(): number[] {
  return [...document.querySelectorAll('li[data-event-id]')].map((item) =>
    Number(item.getAttribute('data-event-id')),
  )
}

describe('the panel module', () => {
  it('registers itself in the table slot', () => {
    expect(slot).toBe('table')
    expect(title).toBe('Remediation timeline')
  })
})

describe('rendering a representative payload', () => {
  it('draws one entry per event, oldest first, with the states it moved between', async () => {
    await renderTimeline()

    const entries = [...document.querySelectorAll('li[data-event-id]')].map(
      (item) => item.textContent ?? '',
    )
    expect(entries).toHaveLength(remediationTimelineFixture.length)
    expect(entries[0]).toContain('Queued')
    expect(entries[3]).toContain('Session created → ')
    expect(entries[3]).toContain('Running')
    expect(entries[4]).toContain('Running → ')
    expect(entries[4]).toContain('PR opened')
  })

  it('stamps each event in UTC and states how far into the run it happened', async () => {
    await renderTimeline()

    const items = [...document.querySelectorAll('li[data-event-id]')].map(
      (item) => item.textContent ?? '',
    )
    // The clock, then the offset from the first event as a duration, not raw seconds.
    expect(items[0]).toContain('03:40:00Z +0s')
    expect(items[1]).toContain('03:40:12Z +12s')
    expect(items[2]).toContain('03:40:12Z +12s')
    expect(items[3]).toContain('03:41:30Z +1m')
    expect(items[4]).toContain('04:05:00Z +25m')

    expect(screen.getByText('03:40:00Z')).toHaveAttribute('datetime', '2026-08-08T03:40:00Z')
  })

  it('names an event that is not a transition, so the log is not read as one', async () => {
    await renderTimeline()

    const call = document.querySelector('li[data-event-id="103"]')
    expect(call).toHaveTextContent('devin_call')
    expect(document.querySelector('li[data-event-id="104"]')).not.toHaveTextContent('transition')
  })

  it('surfaces the scalar fields of detail', async () => {
    await renderTimeline()

    expect(document.querySelector('li[data-event-id="103"]')).toHaveTextContent(
      'status: 201 · session_id: devin-9f2c',
    )
  })

  it('leaves the raw API bodies that detail also carries out of the panel', async () => {
    const withNestedDetail: RemediationEvent = {
      ...remediationTimelineFixture[2],
      detail: { reason: 'requires_upstream_decision', response: { errors: ['nope'] } },
    }
    await renderTimeline({ fetcher: () => Promise.resolve([withNestedDetail]) })

    expect(screen.getByText('reason: requires_upstream_decision')).toBeInTheDocument()
    expect(screen.queryByText(/nope/)).toBeNull()
  })
})

describe('event order', () => {
  it('breaks a tie on created_at with the id, because one transaction stamps one timestamp', () => {
    // 102 and 103 share a created_at. Ordering on the timestamp alone leaves them in array order.
    expect(orderEvents(remediationTimelineFixture).map((each) => each.id)).toEqual([
      101, 102, 103, 104, 105,
    ])
    expect(orderEvents([...remediationTimelineFixture].reverse()).map((each) => each.id)).toEqual([
      101, 102, 103, 104, 105,
    ])
  })

  it('does not reorder itself when the next poll returns the same events shuffled', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(remediationTimelineFixture)
      .mockResolvedValue([...remediationTimelineFixture].reverse())
    await renderTimeline({ fetcher })

    expect(renderedEventIds()).toEqual([101, 102, 103, 104, 105])

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(renderedEventIds()).toEqual([101, 102, 103, 104, 105])
  })
})

describe('choosing which remediation to show', () => {
  it('defaults to the newest remediation still in flight', async () => {
    await renderTimeline({ rows: rowsResolving() })

    // The live table's order: id 13 is the newest of the in-flight rows.
    expect(screen.getByLabelText('Remediation')).toHaveValue('13')
    expect(renderedEventIds()).toEqual([213])
  })

  it('lists every remediation, marking the ones still in flight', async () => {
    await renderTimeline({ rows: rowsResolving() })

    expect(
      [...document.querySelectorAll('option')].map((option) => option.textContent),
    ).toEqual([
      'taxpon/superset#43 — Queued (in flight)',
      'taxpon/superset#42 — Running (in flight)',
      'taxpon/superset#41 — CI failed (in flight)',
      'taxpon/superset#40 — Merged',
      'taxpon/superset#37 — Blocked',
    ])
  })

  it('fetches the timeline of the remediation that was picked', async () => {
    const fetcher = vi.fn(timelineResolving)
    await renderTimeline({ rows: rowsResolving(), fetcher })

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Remediation'), { target: { value: '12' } })
    })

    expect(fetcher.mock.calls.at(-1)?.[0]).toBe(12)
    expect(renderedEventIds()).toEqual([101, 102, 103, 104, 105])
  })

  it('does not show one remediation history under another remediation heading', async () => {
    // The second selection never answers; the first one's events must not stay on screen under it.
    const rows = remediationRowsFixture.filter((each) => each.id === 12 || each.id === 9)
    const fetcher: TimelineFetcher = (id) =>
      id === 12
        ? Promise.resolve(remediationTimelineFixture)
        : new Promise<RemediationEvent[]>(() => {})
    await renderTimeline({ rows: rowsResolving(rows), fetcher })

    expect(renderedEventIds()).toEqual([101, 102, 103, 104, 105])

    await act(async () => {
      fireEvent.change(screen.getByLabelText('Remediation'), { target: { value: '9' } })
    })

    expect(renderedEventIds()).toEqual([])
    expect(screen.getByText('Loading timeline…')).toBeInTheDocument()
  })
})

describe('the empty, loading and error states', () => {
  it('says there is nothing to show yet rather than rendering an empty list', async () => {
    // What the demo shows before the first issue is labelled.
    await renderTimeline({ rows: rowsResolving([]) })

    expect(screen.getByText('No remediations yet, so there is no timeline to show.')).toBeInTheDocument()
    expect(screen.queryByLabelText('Remediation')).toBeNull()
  })

  it('distinguishes a remediation with no events from no remediation at all', async () => {
    await renderTimeline({ fetcher: () => Promise.resolve([]) })

    expect(screen.getByLabelText('Remediation')).toBeInTheDocument()
    expect(
      screen.getByText('No transitions have been recorded for this remediation yet.'),
    ).toBeInTheDocument()
  })

  it('says it is loading while the rows and then the timeline are on their way', async () => {
    vi.useFakeTimers()
    const { unmount } = render(
      <Timeline options={{ fetcher: () => new Promise(() => {}) }} fetcher={timelineResolving} />,
    )
    expect(screen.getByText('Loading remediations…')).toBeInTheDocument()
    unmount()

    render(
      <Timeline
        options={{ fetcher: rowsResolving(ONLY_RUNNING) }}
        fetcher={() => new Promise(() => {})}
      />,
    )
    await act(async () => {})
    expect(screen.getByText('Loading timeline…')).toBeInTheDocument()
  })

  it('reports an unreachable API when it has nothing to show', async () => {
    await renderTimeline({ fetcher: () => Promise.reject(new Error('offline')) })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not reach the remediation timeline.',
    )
    expect(document.querySelector('li[data-event-id]')).toBeNull()
  })

  it('keeps the last good timeline when a poll fails', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(remediationTimelineFixture)
      .mockRejectedValue(new Error('offline'))
    await renderTimeline({ fetcher })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not reach the remediation timeline — showing the last successful update.',
    )
    expect(renderedEventIds()).toEqual([101, 102, 103, 104, 105])
  })

  it('stops polling on unmount', async () => {
    const { unmount } = await renderTimeline()
    unmount()

    expect(vi.getTimerCount()).toBe(0)
  })
})
