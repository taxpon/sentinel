// The shell. It owns the polling loop, the freshness indicator and the layout regions; it owns no
// panels. Panels are discovered from src/panels/, so T31, T32 and T33 each add files without
// editing this one or each other's.

import { useEffect, useState } from 'react'
import {
  freshness,
  useAnalyticsSummary,
  type PanelModule,
  type PanelSlot,
  type SummaryState,
  type UseAnalyticsSummaryOptions,
} from './api'

/** Discovered panel modules, keyed by module path so the order can be made deterministic. */
export type PanelRegistry = Record<string, PanelModule>

const SLOTS: { slot: PanelSlot; className: string; label: string }[] = [
  { slot: 'kpi', className: 'kpi-row', label: 'Key metrics' },
  { slot: 'chart', className: 'chart-grid', label: 'Charts' },
  { slot: 'table', className: 'table-region', label: 'Live remediations' },
]

/**
 * Every `src/panels/*.tsx` that is not a test. The glob is evaluated at build time, so a panel file
 * added by another task is picked up with no edit here.
 */
const DISCOVERED_PANELS: PanelRegistry = import.meta.glob<PanelModule>(
  ['./panels/*.tsx', '!./panels/*.test.tsx'],
  { eager: true },
)

function panelsForSlot(registry: PanelRegistry, slot: PanelSlot): [string, PanelModule][] {
  return Object.entries(registry)
    .filter(([, panel]) => panel.slot === slot)
    .sort(([pathA, a], [pathB, b]) => (a.order ?? 0) - (b.order ?? 0) || pathA.localeCompare(pathB))
}

/** Ticks so that "updated Ns ago" advances between polls rather than only when a poll lands. */
function useNow(tickMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), tickMs)
    return () => clearInterval(timer)
  }, [tickMs])
  return now
}

export function FreshnessIndicator({ state }: { state: SummaryState }) {
  const now = useNow()

  if (!state.data) {
    return (
      <span className="freshness" data-stale="true" role="status">
        {state.status === 'loading' ? 'loading…' : 'never updated'}
      </span>
    )
  }

  const { label, stale } = freshness(state.data.generated_at, now)
  return (
    <span
      className={stale ? 'freshness freshness--stale' : 'freshness'}
      data-stale={String(stale)}
      role="status"
    >
      {label}
    </span>
  )
}

function StatusBanner({ state }: { state: SummaryState }) {
  if (state.status === 'loading') {
    return <p className="banner banner--loading">Loading analytics…</p>
  }
  if (state.status === 'error') {
    return (
      <p className="banner banner--error" role="alert">
        Could not reach the analytics API
        {state.data ? ' — showing the last successful update.' : '.'}
      </p>
    )
  }
  if (state.status === 'empty') {
    return <p className="banner banner--empty">No issues were labelled in this window yet.</p>
  }
  return null
}

export interface AppProps {
  /** Injected in tests; in the app it is the set of panel modules on disk. */
  panels?: PanelRegistry
  options?: UseAnalyticsSummaryOptions
}

export default function App({ panels = DISCOVERED_PANELS, options }: AppProps) {
  const state = useAnalyticsSummary(options)

  return (
    <div className="app">
      <header className="app-header">
        <h1>Sentinel</h1>
        <FreshnessIndicator state={state} />
      </header>

      <StatusBanner state={state} />

      <main className="app-main">
        {SLOTS.map(({ slot, className, label }) => {
          const mounted = panelsForSlot(panels, slot)
          return (
            <section key={slot} className={className} aria-label={label} data-slot={slot}>
              {mounted.length === 0 ? (
                <p className="slot-empty">No {slot} panels are mounted yet.</p>
              ) : (
                mounted.map(([path, panel]) => {
                  const Panel = panel.default
                  return (
                    <section key={path} className="panel">
                      <h2 className="panel__title">{panel.title}</h2>
                      <div className="panel__body">
                        <Panel state={state} />
                      </div>
                    </section>
                  )
                })
              )}
            </section>
          )
        })}
      </main>
    </div>
  )
}
