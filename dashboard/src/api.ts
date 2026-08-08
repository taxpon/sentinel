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

/**
 * Whether the cost figures came from Devin or were derived by Sentinel from the unit cost. The
 * cost panel must label this; see docs/07-observability.md and docs/05-devin-integration.md.
 */
export type CostSource = 'devin_consumption_api' | 'derived'

export interface Cost {
  acus_total: number
  acus_per_merged_fix: number
  usd_per_fix: number
  unit_cost_usd: number
  source: CostSource
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
  cost: Cost
  cycles: Cycles
  throughput: ThroughputDay[]
  failures: FailureBucket[]
  impact: Impact
  generated_at: string
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

export type SummaryFetcher = (window: string, signal: AbortSignal) => Promise<AnalyticsSummary>

export const fetchSummary: SummaryFetcher = async (window, signal) => {
  const response = await fetch(`/api/analytics/summary?window=${encodeURIComponent(window)}`, {
    signal,
    headers: { accept: 'application/json' },
  })
  if (!response.ok) {
    throw new AnalyticsApiError(
      `GET /api/analytics/summary failed with ${response.status}`,
      response.status,
    )
  }
  return (await response.json()) as AnalyticsSummary
}

// --- Loading, error and empty states -------------------------------------------------------------

/**
 * `empty` is a successful response describing a window in which nothing was labelled — a real
 * state a panel has to render, and the one that produces `NaN` if a panel divides by the funnel.
 * `error` keeps the last good payload so that one failed poll does not blank a working dashboard.
 */
export type SummaryStatus = 'loading' | 'ready' | 'empty' | 'error'

export interface SummaryState {
  status: SummaryStatus
  data: AnalyticsSummary | null
  error: Error | null
}

export function isEmptySummary(summary: AnalyticsSummary): boolean {
  return summary.funnel.labelled === 0
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

  const [state, setState] = useState<SummaryState>({ status: 'loading', data: null, error: null })

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  useEffect(() => {
    const controller = new AbortController()

    const load = async () => {
      try {
        const data = await fetcherRef.current(windowParam, controller.signal)
        if (controller.signal.aborted) return
        setState({ status: isEmptySummary(data) ? 'empty' : 'ready', data, error: null })
      } catch (cause) {
        if (controller.signal.aborted) return
        setState((previous) => ({
          status: 'error',
          data: previous.data,
          error: cause instanceof Error ? cause : new Error(String(cause)),
        }))
      }
    }

    void load()
    const timer = setInterval(() => void load(), intervalMs)
    return () => {
      controller.abort()
      clearInterval(timer)
    }
  }, [windowParam, intervalMs])

  return state
}

// --- Freshness ---------------------------------------------------------------------------------

export interface Freshness {
  ageSeconds: number
  label: string
  /** True once the payload is older than FRESHNESS_AMBER_AFTER_SECONDS. */
  stale: boolean
}

/**
 * "updated Ns ago", amber past 30s. A clock running behind the API would otherwise produce a
 * negative age, so the age is clamped at zero.
 */
export function freshness(generatedAt: string, now: number): Freshness {
  const generated = Date.parse(generatedAt)
  if (Number.isNaN(generated)) {
    return { ageSeconds: Number.NaN, label: 'never updated', stale: true }
  }

  const ageSeconds = Math.max(0, Math.floor((now - generated) / 1000))
  return {
    ageSeconds,
    label: `updated ${formatAge(ageSeconds)} ago`,
    stale: ageSeconds > FRESHNESS_AMBER_AFTER_SECONDS,
  }
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

export function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return NO_VALUE
  return `$${value.toFixed(2)}`
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
  /** The whole state, not just the payload: every panel renders its own loading and empty case. */
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
