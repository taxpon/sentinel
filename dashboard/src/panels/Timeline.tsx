// One remediation's transitions, read off the append-only `remediation_event` log
// (docs/03-data-model.md), which is the audit trail behind every duration on this dashboard.
//
// The shell has no cross-panel selection state and this panel does not own it either, so the
// remediation is chosen here: the live table's own order picks the default — the newest thing still
// in flight — and a select switches to any other row.

import { useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import {
  POLL_INTERVAL_MS,
  fetchRemediationTimeline,
  formatDurationSeconds,
  showData,
  useRemediations,
  type PanelSlot,
  type PolledState,
  type RemediationEvent,
  type UseRemediationsOptions,
} from '../api'
import { isTerminal, orderRemediations, stateBadgeStyle, stateLabel } from './LiveTable'

/**
 * Oldest first, ties broken by `id`.
 *
 * `remediation_event.created_at` defaults to `now()`, which in Postgres is
 * `transaction_timestamp()`, and docs/06-event-pipeline.md writes the transition, the event and the
 * job in one transaction — so several rows routinely carry a byte-identical timestamp. Ordering on
 * the timestamp alone leaves those in whatever order the API happened to return them, and the
 * timeline reorders itself between polls. `id` is a `bigserial`, so it is the insertion order the
 * timestamp is too coarse to express.
 */
export function orderEvents(events: readonly RemediationEvent[]): RemediationEvent[] {
  return [...events].sort(
    (a, b) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.id - b.id,
  )
}

export type TimelineFetcher = (id: number, signal: AbortSignal) => Promise<RemediationEvent[]>

const NO_TIMELINE: PolledState<RemediationEvent[]> = {
  status: 'loading',
  data: null,
  error: null,
  receivedAt: null,
}

/**
 * The same shape as `useRemediations`, for the per-remediation endpoint: one request at a time, and
 * everything in flight cancelled when the selection changes. `api.ts` keeps its polling primitive
 * private and exposes only the two hooks it already needed, and that file belongs to T30.
 */
function useTimeline(
  id: number | null,
  intervalMs: number,
  fetcher: TimelineFetcher,
): PolledState<RemediationEvent[]> {
  const [state, setState] = useState<PolledState<RemediationEvent[]>>(NO_TIMELINE)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    // A new selection starts from nothing: keeping the previous payload would show one
    // remediation's history under another one's heading for as long as the first poll takes.
    setState(NO_TIMELINE)
    if (id === null) return

    const controller = new AbortController()
    let inFlight = false

    const poll = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const data = await fetcherRef.current(id, controller.signal)
        if (controller.signal.aborted) return
        setState({
          status: data.length === 0 ? 'empty' : 'ready',
          data,
          error: null,
          receivedAt: Date.now(),
        })
      } catch (cause) {
        if (controller.signal.aborted) return
        setState((previous) => ({
          status: 'error',
          data: previous.data,
          error: cause instanceof Error ? cause : new Error(String(cause)),
          receivedAt: previous.receivedAt,
        }))
      } finally {
        inFlight = false
      }
    }

    void poll()
    const timer = setInterval(() => void poll(), intervalMs)
    return () => {
      controller.abort()
      clearInterval(timer)
    }
  }, [id, intervalMs])

  return state
}

/** UTC, so the log reads the same as the JSON it came from and a test is not timezone-dependent. */
function clockTime(timestamp: string): string {
  const parsed = new Date(timestamp)
  return `${parsed.toISOString().slice(11, 19)}Z`
}

/**
 * The scalar fields of `detail` — the Devin session id, an HTTP status, a blocked reason. Nested
 * objects are the raw API bodies the log also carries, which are for the database, not the panel.
 */
function detailEntries(detail: Record<string, unknown> | null): [string, string][] {
  if (detail === null) return []
  return Object.entries(detail)
    .filter(([, value]) => value !== null && typeof value !== 'object')
    .map(([key, value]) => [key, String(value)])
}

const LIST_STYLE: CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: '6px',
}

const ITEM_STYLE: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 10em) minmax(0, 1fr)',
  gap: '8px',
  alignItems: 'baseline',
  paddingLeft: '10px',
  borderLeft: '2px solid var(--border)',
}

const WHEN_STYLE: CSSProperties = {
  color: 'var(--muted)',
  fontVariantNumeric: 'tabular-nums',
  whiteSpace: 'nowrap',
}

const FROM_STYLE: CSSProperties = { color: 'var(--muted)' }

const KIND_STYLE: CSSProperties = { marginLeft: '6px', color: 'var(--muted)' }

const DETAIL_STYLE: CSSProperties = { margin: '2px 0 0', color: 'var(--muted)' }

const SELECT_STYLE: CSSProperties = { marginBottom: '8px', display: 'flex', gap: '6px' }

function Event({ event, offsetSeconds }: { event: RemediationEvent; offsetSeconds: number }) {
  const entries = detailEntries(event.detail)
  return (
    <li style={ITEM_STYLE} data-to-state={event.to_state} data-event-id={event.id}>
      <span style={WHEN_STYLE}>
        <time dateTime={event.created_at}>{clockTime(event.created_at)}</time>
        {` +${formatDurationSeconds(offsetSeconds)}`}
      </span>
      <span>
        {event.from_state !== null && (
          <span style={FROM_STYLE}>{stateLabel(event.from_state)} → </span>
        )}
        <span style={stateBadgeStyle(event.to_state)}>{stateLabel(event.to_state)}</span>
        {event.kind !== 'transition' && <small style={KIND_STYLE}>{event.kind}</small>}
        {entries.length > 0 && (
          <small style={DETAIL_STYLE}>
            {entries.map(([key, value]) => `${key}: ${value}`).join(' · ')}
          </small>
        )}
      </span>
    </li>
  )
}

export interface TimelineProps {
  /** Injected by the tests; the shell passes only `state`, so these stay at their defaults there. */
  options?: UseRemediationsOptions
  intervalMs?: number
  fetcher?: TimelineFetcher
}

export default function Timeline({
  options,
  intervalMs = POLL_INTERVAL_MS,
  fetcher = fetchRemediationTimeline,
}: TimelineProps) {
  const remediations = useRemediations(options)

  const rows = useMemo(
    () => (showData(remediations) ? orderRemediations(remediations.data) : []),
    [remediations],
  )

  const [chosenId, setChosenId] = useState<number | null>(null)
  // A choice that has fallen out of the window falls back to the default rather than showing
  // nothing, and the default follows the live table: the newest remediation still in flight.
  const selectedId = rows.some((row) => row.id === chosenId) ? chosenId : (rows[0]?.id ?? null)

  const timeline = useTimeline(selectedId, intervalMs, fetcher)
  const events = useMemo(
    () => (showData(timeline) ? orderEvents(timeline.data) : []),
    [timeline],
  )
  const startedAt = events.length > 0 ? Date.parse(events[0].created_at) : 0

  if (rows.length === 0) {
    return (
      <p className="slot-empty">
        {remediations.status === 'loading'
          ? 'Loading remediations…'
          : 'No remediations yet, so there is no timeline to show.'}
      </p>
    )
  }

  return (
    <>
      <label style={SELECT_STYLE}>
        <span>Remediation</span>
        <select
          value={selectedId ?? ''}
          onChange={(changed) => setChosenId(Number(changed.target.value))}
        >
          {rows.map((row) => (
            <option key={row.id} value={row.id}>
              {row.repo}#{row.issue_number} — {stateLabel(row.state)}
              {isTerminal(row.state) ? '' : ' (in flight)'}
            </option>
          ))}
        </select>
      </label>

      {timeline.status === 'error' && (
        <p className="banner banner--error" role="alert">
          Could not reach the remediation timeline
          {showData(timeline) ? ' — showing the last successful update.' : '.'}
        </p>
      )}

      {!showData(timeline) ? (
        timeline.status === 'loading' && <p className="slot-empty">Loading timeline…</p>
      ) : events.length === 0 ? (
        <p className="slot-empty">No transitions have been recorded for this remediation yet.</p>
      ) : (
        <ol style={LIST_STYLE}>
          {events.map((event) => (
            <Event
              key={event.id}
              event={event}
              offsetSeconds={(Date.parse(event.created_at) - startedAt) / 1000}
            />
          ))}
        </ol>
      )}
    </>
  )
}

export const slot: PanelSlot = 'table'
export const title = 'Remediation timeline'
export const order = 2
