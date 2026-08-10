// The shell. It owns the polling loop, the freshness indicator and the layout regions; it owns no
// panels. Panels are discovered from src/panels/, so T31, T32 and T33 each add files without
// editing this one or each other's.

import { Component, useEffect, useState } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
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

const PANEL_SLOTS: readonly PanelSlot[] = ['kpi', 'chart', 'table']

/**
 * Every `src/panels/*.tsx` that is not a test. The glob is evaluated at build time, so a panel file
 * added by another task is picked up with no edit here.
 */
export const DISCOVERED_PANELS: PanelRegistry = import.meta.glob<PanelModule>(
  ['./panels/*.tsx', '!./panels/*.test.tsx'],
  { eager: true },
)

/**
 * The type argument on `import.meta.glob` is an assertion about the glob's result, not a constraint
 * on the files it matched: nothing connects `PanelModule` to anything in `src/panels/`, and a module
 * that forgets to export `slot` type-checks clean and then silently fails to appear. Three tasks add
 * files to that directory without seeing each other's code, so the shape is checked here instead,
 * and a module that fails is named on screen rather than vanishing.
 */
export function isPanelModule(value: unknown): value is PanelModule {
  if (typeof value !== 'object' || value === null) return false
  const module = value as Partial<PanelModule>
  return (
    module.default != null &&
    typeof module.title === 'string' &&
    PANEL_SLOTS.includes(module.slot as PanelSlot)
  )
}

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
  const { label, stale } = freshness(state.data?.generated_at ?? null, now, state.receivedAt)
  const text = state.data === null && state.status === 'loading' ? 'loading…' : label

  return (
    <>
      <span
        className={stale ? 'freshness freshness--stale' : 'freshness'}
        data-stale={String(stale)}
      >
        {text}
      </span>
      {/* The visible label changes every second; announcing each tick would make the page unusable
          with a screen reader. Only the crossing into staleness is worth an announcement, so this
          region's text changes exactly once per crossing. */}
      <span className="visually-hidden" role="status">
        {stale ? 'Analytics data is out of date.' : ''}
      </span>
    </>
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

/** Names the panel modules that were discovered but cannot be mounted, instead of dropping them. */
function BrokenPanels({ paths }: { paths: string[] }) {
  if (paths.length === 0) return null
  return (
    <p className="banner banner--error" role="alert">
      {paths.length === 1 ? '1 panel could' : `${paths.length} panels could`} not be mounted — a
      module must export a component, a title and a slot: {paths.join(', ')}
    </p>
  )
}

interface PanelBoundaryProps {
  title: string
  children: ReactNode
}

/**
 * One boundary per panel. The shell mounts components written by three tasks it cannot see, so a
 * render that throws must cost that panel and nothing else: one broken panel should not take down
 * the header, the freshness indicator and every other panel that was working.
 */
class PanelBoundary extends Component<PanelBoundaryProps, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`Panel "${this.props.title}" failed to render`, error, info.componentStack)
  }

  render() {
    if (this.state.failed) {
      return (
        <p className="panel__failed" role="alert">
          {this.props.title} panel failed.
        </p>
      )
    }
    return this.props.children
  }
}

export interface AppProps {
  /** Injected in tests; in the app it is the set of panel modules on disk. */
  panels?: PanelRegistry
  options?: UseAnalyticsSummaryOptions
}

export default function App({ panels = DISCOVERED_PANELS, options }: AppProps) {
  const state = useAnalyticsSummary(options)

  const entries = Object.entries(panels)
  const mountable = Object.fromEntries(entries.filter(([, panel]) => isPanelModule(panel)))
  const broken = entries.filter(([, panel]) => !isPanelModule(panel)).map(([path]) => path)

  return (
    <div className="app">
      <header className="app-header">
        <h1>Sentinel</h1>
        <FreshnessIndicator state={state} />
      </header>

      <StatusBanner state={state} />
      <BrokenPanels paths={broken} />

      <main className="app-main">
        {SLOTS.map(({ slot, className, label }) => {
          const mounted = panelsForSlot(mountable, slot)
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
                        <PanelBoundary title={panel.title}>
                          <Panel state={state} />
                        </PanelBoundary>
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
