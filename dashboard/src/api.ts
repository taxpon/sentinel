// The typed client for the analytics API, plus the pieces every panel shares: the polling hook,
// the freshness calculation and the formatters. The response shape is transcribed from the JSON
// schema in docs/07-observability.md, which is authoritative and fixed before the endpoint exists
// (T25). Panels in T31-T33 import from here; nothing else is shared between them.

import { useEffect, useRef, useState } from 'react'
import type { ComponentType } from 'react'

/** The dashboard polls every 5 seconds. docs/07-observability.md#dashboard. */
export const POLL_INTERVAL_MS = 5000

/** The freshness indicator turns amber once the payload is older than this. */
export const FRESHNESS_AMBER_AFTER_SECONDS = 30

/** The window the dashboard asks for when nothing else is specified. */
export const DEFAULT_WINDOW = '7d'

// --- Response schema ---------------------------------------------------------------------------

export interface AnalyticsWindow {
  from: string
  to: string
}

export interface Funnel {
  labelled: number
  session_created: number
  pr_opened: number
  ci_green: number
  merged: number
}

export interface Rates {
  success: number
  merge: number
  autonomy: number
}

export interface Percentiles {
  p50: number
  p90: number
}

export interface Durations {
  to_pr: Percentiles
  to_merge: Percentiles
  review_latency: Percentiles
}

export interface Cycles {
  mean: number
  /** Keyed by cycle count as a string, because it arrives as a JSON object key. */
  distribution: Record<string, number>
}

export interface ThroughputDay {
  day: string
  /** Merged count keyed by issue class. Classes are open-ended, hence the index signature. */
  by_class: Record<string, number>
}

export interface FailureBucket {
  reason: string
  count: number
  issues: number[]
}

export interface Impact {
  hours_saved: number
  /** Stated inline by the impact panel: the figure rests on an assumption, not a measurement. */
  assumption: string
}

export interface AnalyticsSummary {
  window: AnalyticsWindow
  funnel: Funnel
  rates: Rates
  durations_seconds: Durations
  cycles: Cycles
  throughput: ThroughputDay[]
  failures: FailureBucket[]
  impact: Impact
  generated_at: string
}

// --- The live remediation table ------------------------------------------------------------------

/** The states in docs/04-state-machine.md, in the order a remediation passes through them. */
export type RemediationState =
  | 'QUEUED'
  | 'SESSION_CREATED'
  | 'RUNNING'
  | 'PR_OPENED'
  | 'CI_RUNNING'
  | 'CI_FAILED'
  | 'CI_PASSED'
  | 'IN_REVIEW'
  | 'CHANGES_REQUESTED'
  | 'MERGED'
  | 'BLOCKED'
  | 'FAILED'

/**
 * A row of `GET /api/remediations`. docs/07-observability.md names the columns the live table shows
 * — state, class, cycle, ACUs, elapsed, Devin session URL, PR URL — without pinning a JSON schema
 * the way it pins the summary, so the field names are taken from the `remediation` columns in
 * docs/03-data-model.md. T25 implements the endpoint against this; a divergence is T25's to
 * reconcile with the issue, not something a panel should work around.
 */
export interface RemediationRow {
  id: number
  repo: string
  issue_number: number
  issue_class: string
  state: RemediationState
  cycle: number
  acus_consumed: number
  /** Seconds since `labeled_at`, so the table does not have to subtract clocks itself. */
  elapsed_seconds: number
  devin_session_url: string | null
  pr_number: number | null
  pr_url: string | null
  /** Populated on BLOCKED and FAILED. */
  blocked_reason: string | null
  labeled_at: string
}

/** A row of `GET /api/remediations/{id}` — the append-only transition log for one remediation. */
export interface RemediationEvent {
  id: number
  remediation_id: number
  from_state: RemediationState | null
  to_state: RemediationState
  kind: 'transition' | 'devin_call' | 'github_call' | 'policy' | 'error'
  detail: Record<string, unknown> | null
  created_at: string
}

// --- Client ------------------------------------------------------------------------------------

export class AnalyticsApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'AnalyticsApiError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

async function getJson(path: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(path, { signal, headers: { accept: 'application/json' } })
  if (!response.ok) {
    throw new AnalyticsApiError(`GET ${path} failed with ${response.status}`, response.status)
  }
  return await response.json()
}

const SUMMARY_KEYS = [
  'window',
  'funnel',
  'rates',
  'durations_seconds',
  'cycles',
  'throughput',
  'failures',
  'impact',
  'generated_at',
] as const

/**
 * A shape check at the boundary, not a full schema validation. The endpoint does not exist yet
 * (T25), and a proxy answering with HTML or an older payload should read as "the API sent the wrong
 * shape" in the error banner rather than as a panel crashing on a missing field.
 */
export function parseSummary(body: unknown, status = 200): AnalyticsSummary {
  if (!isRecord(body)) {
    throw new AnalyticsApiError('/api/analytics/summary did not return a JSON object', status)
  }
  const missing = SUMMARY_KEYS.filter((key) => body[key] === undefined)
  if (missing.length > 0) {
    throw new AnalyticsApiError(
      `/api/analytics/summary is missing ${missing.join(', ')}`,
      status,
    )
  }
  if (!isRecord(body.funnel) || typeof body.funnel.labelled !== 'number') {
    throw new AnalyticsApiError('/api/analytics/summary has no numeric funnel.labelled', status)
  }
  return body as unknown as AnalyticsSummary
}

export type SummaryFetcher = (window: string, signal: AbortSignal) => Promise<AnalyticsSummary>

export const fetchSummary: SummaryFetcher = async (window, signal) =>
  parseSummary(
    await getJson(`/api/analytics/summary?window=${encodeURIComponent(window)}`, signal),
  )

export type RemediationsFetcher = (signal: AbortSignal) => Promise<RemediationRow[]>

export const fetchRemediations: RemediationsFetcher = async (signal) => {
  const body = await getJson('/api/remediations', signal)
  if (!Array.isArray(body)) {
    throw new AnalyticsApiError('/api/remediations did not return an array', 200)
  }
  return body as RemediationRow[]
}

export async function fetchRemediationTimeline(
  id: number,
  signal: AbortSignal,
): Promise<RemediationEvent[]> {
  const body = await getJson(`/api/remediations/${id}`, signal)
  if (!Array.isArray(body)) {
    throw new AnalyticsApiError(`/api/remediations/${id} did not return an array`, 200)
  }
  return body as RemediationEvent[]
}

// --- Loading, error and empty states -------------------------------------------------------------

/**
 * `empty` is a successful response describing a window in which nothing was labelled — a real
 * state a panel has to render, and the one that produces `NaN` if a panel divides by the funnel.
 * `error` keeps the last good payload so that one failed poll does not blank a working dashboard.
 */
export type SummaryStatus = 'loading' | 'ready' | 'empty' | 'error'

export interface PolledState<T> {
  status: SummaryStatus
  data: T | null
  error: Error | null
  /** Local clock when `data` arrived. Skew-proof input to the freshness indicator. */
  receivedAt: number | null
}

export type SummaryState = PolledState<AnalyticsSummary>
export type RemediationsState = PolledState<RemediationRow[]>

/**
 * Whether a panel should draw its figures. True whenever a payload has ever arrived — including in
 * the `error` state, where the shell is deliberately still showing the last good values. A panel
 * that branches on `status === 'error'` instead would blank itself while holding usable data.
 */
export function showData<T>(state: PolledState<T>): state is PolledState<T> & { data: T } {
  return state.data !== null
}

export function isEmptySummary(summary: AnalyticsSummary): boolean {
  return summary.funnel.labelled === 0
}

interface PolledResourceOptions<T> {
  /** Restarts polling when it changes; nothing else does. */
  key: string
  intervalMs: number
  isEmpty?: (data: T) => boolean
}

/**
 * One poll at a time, every `intervalMs`, cancelled on unmount.
 *
 * The re-entry guard is the point of this function. A plain `setInterval(load)` against an API that
 * has slowed down starts a new request before the last one answered — more load exactly when the
 * API can least take it — and applies the responses in completion order, so a slow poll can
 * overwrite a newer one and walk `generated_at` backwards. Skipping a tick while a request is in
 * flight makes overlap impossible, and with a single caller that also makes the responses ordered.
 * Everything that survives a restart is cancelled by the abort instead.
 */
function usePolledResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  { key, intervalMs, isEmpty }: PolledResourceOptions<T>,
): PolledState<T> {
  const [state, setState] = useState<PolledState<T>>({
    status: 'loading',
    data: null,
    error: null,
    receivedAt: null,
  })

  // Read through a ref so that a caller passing an inline fetcher does not restart the interval.
  const loadRef = useRef(load)
  loadRef.current = load

  useEffect(() => {
    const controller = new AbortController()
    let inFlight = false

    const poll = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const data = await loadRef.current(controller.signal)
        if (controller.signal.aborted) return
        setState({
          status: isEmpty?.(data) ? 'empty' : 'ready',
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
  }, [key, intervalMs])

  return state
}

export interface UseAnalyticsSummaryOptions {
  window?: string
  intervalMs?: number
  /** Replaced in tests. Read through a ref, so passing a new function does not restart polling. */
  fetcher?: SummaryFetcher
}

export function useAnalyticsSummary(options: UseAnalyticsSummaryOptions = {}): SummaryState {
  const {
    window: windowParam = DEFAULT_WINDOW,
    intervalMs = POLL_INTERVAL_MS,
    fetcher = fetchSummary,
  } = options

  return usePolledResource((signal) => fetcher(windowParam, signal), {
    key: windowParam,
    intervalMs,
    isEmpty: isEmptySummary,
  })
}

export interface UseRemediationsOptions {
  intervalMs?: number
  fetcher?: RemediationsFetcher
}

/**
 * The live table's rows. A second endpoint, but the same loop: the spec has the SPA polling
 * `/api/remediations` alongside the summary every 5 seconds.
 */
export function useRemediations(options: UseRemediationsOptions = {}): RemediationsState {
  const { intervalMs = POLL_INTERVAL_MS, fetcher = fetchRemediations } = options

  return usePolledResource((signal) => fetcher(signal), {
    key: 'remediations',
    intervalMs,
    isEmpty: (rows) => rows.length === 0,
  })
}

// --- Freshness ---------------------------------------------------------------------------------

export interface Freshness {
  ageSeconds: number
  label: string
  /** True once the payload is older than FRESHNESS_AMBER_AFTER_SECONDS. */
  stale: boolean
}

/**
 * "updated Ns ago", amber past 30s.
 *
 * The age is the larger of two: how old the server says the payload is, and how long ago this
 * browser last received one. The second exists because the first is only as good as the agreement
 * between the two clocks — an API running five minutes ahead makes every payload look like it
 * arrived from the future, and clamping that to zero would leave the indicator reading "updated 0s
 * ago" in grey for five minutes after the poller died. Measuring locally cannot be skewed, and
 * "when did we last hear anything" is the question the indicator is really being asked.
 */
export function freshness(
  generatedAt: string | null,
  now: number,
  receivedAt: number | null,
): Freshness {
  const generated = Date.parse(generatedAt ?? '')
  const ages = [
    Number.isNaN(generated) ? null : elapsedSeconds(generated, now),
    receivedAt === null ? null : elapsedSeconds(receivedAt, now),
  ].filter((age): age is number => age !== null)

  if (ages.length === 0) {
    return { ageSeconds: Number.NaN, label: 'never updated', stale: true }
  }

  const ageSeconds = Math.max(...ages)
  return {
    ageSeconds,
    label: `updated ${formatAge(ageSeconds)} ago`,
    stale: ageSeconds > FRESHNESS_AMBER_AFTER_SECONDS,
  }
}

function elapsedSeconds(since: number, now: number): number {
  return Math.max(0, Math.floor((now - since) / 1000))
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  return `${Math.floor(seconds / 3600)}h`
}

// --- Formatting --------------------------------------------------------------------------------

/**
 * What a panel shows instead of a number it does not have. An empty window divides by zero all
 * over the metric definitions, and `NaN` on a leadership dashboard is worse than an em dash.
 */
export const NO_VALUE = '—'

export function formatPercent(value: number, fractionDigits = 0): string {
  if (!Number.isFinite(value)) return NO_VALUE
  return `${(value * 100).toFixed(fractionDigits)}%`
}

export function formatNumber(value: number, fractionDigits = 1): string {
  if (!Number.isFinite(value)) return NO_VALUE
  return value.toFixed(fractionDigits)
}

/** Durations arrive as seconds and are read by humans, so they render as `1h 48m`, not `6480`. */
export function formatDurationSeconds(seconds: number): string {
  if (!Number.isFinite(seconds)) return NO_VALUE
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total}s`

  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes}m`

  const hours = Math.floor(minutes / 60)
  const remainder = minutes % 60
  if (hours < 24) return remainder === 0 ? `${hours}h` : `${hours}h ${remainder}m`

  const days = Math.floor(hours / 24)
  const remainderHours = hours % 24
  return remainderHours === 0 ? `${days}d` : `${days}d ${remainderHours}h`
}

// --- Theme -------------------------------------------------------------------------------------

/**
 * The spec allows one accent colour, with per-class colours reserved for the stacked series. The
 * series palette is ordinal because issue classes are open-ended; it wraps rather than running out.
 */
export const SERIES_COLOR_COUNT = 6

export function seriesColor(index: number): string {
  return `var(--series-${(Math.abs(index) % SERIES_COLOR_COUNT) + 1})`
}

// --- Panel contract ----------------------------------------------------------------------------

/**
 * Where a panel mounts in the shell. The KPI row is pinned to the top because the spec requires it
 * to be visible at 1440px without scrolling.
 */
export type PanelSlot = 'kpi' | 'chart' | 'table'

export interface PanelProps {
  /**
   * The whole state, not just the payload: every panel renders its own loading and empty case.
   * Draw the figures whenever `showData(state)` is true rather than branching on `status`, so that
   * the `error` state keeps showing the last good values instead of blanking the panel.
   */
  state: SummaryState
}

/**
 * The module shape every file in `src/panels/` exports. T31-T33 each add files there and the shell
 * discovers them, so three parallel tasks add panels without editing a shared registry.
 */
export interface PanelModule {
  default: ComponentType<PanelProps>
  slot: PanelSlot
  /** Rendered as the panel heading by the shell. */
  title: string
  /** Ascending within a slot. Ties fall back to the module path, so the order is deterministic. */
  order?: number
}
