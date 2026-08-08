// The failure breakdown panel — "what can it *not* do?" in docs/07-observability.md, which also
// says why it exists: "a system that hides its failures cannot be evaluated".
//
// The metric, from the table in that document: count grouped by `blocked_reason`, for
// `state ∈ {BLOCKED, FAILED}`. The API delivers it already grouped, with the issue numbers behind
// each bucket, so the reasons are reproduced verbatim — a `blocked_reason` is the token recorded on
// the remediation, and prettifying it would break the link between the panel and the database.
//
// A breakdown is a list of named buckets of wildly different name lengths, each carrying issue
// numbers a reader is meant to go and look at. That is a table, not a chart; the Recharts ADR names
// the five panels that need a chart and this is not one of them.

import type { CSSProperties } from 'react'
import { showData, type FailureBucket, type PanelProps, type PanelSlot } from '../api'

export const title = 'Failure breakdown'
export const slot: PanelSlot = 'chart'
export const order = 60

/** Largest bucket first, ties broken by reason so the order does not depend on the API's. */
export function rankedFailures(failures: FailureBucket[]): FailureBucket[] {
  return [...failures].sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason))
}

export function totalFailures(failures: FailureBucket[]): number {
  return failures.reduce((total, bucket) => total + bucket.count, 0)
}

const note: CSSProperties = { margin: 0, color: 'var(--muted)' }
const summaryLine: CSSProperties = { margin: '0 0 8px' }
const scroller: CSSProperties = { maxHeight: 180, overflowY: 'auto' }
const table: CSSProperties = { width: '100%', borderCollapse: 'collapse' }
const th: CSSProperties = {
  textAlign: 'left',
  color: 'var(--muted)',
  fontWeight: 500,
  padding: '2px 8px 2px 0',
}
const td: CSSProperties = { padding: '3px 8px 3px 0', verticalAlign: 'top' }
const countCell: CSSProperties = { ...td, fontVariantNumeric: 'tabular-nums', fontWeight: 600 }
const bar: CSSProperties = { height: 6, background: 'var(--accent)', borderRadius: 3 }

export default function Failures({ state }: PanelProps) {
  if (!showData(state)) {
    return (
      <p style={note}>
        {state.status === 'error'
          ? 'No failure breakdown — the analytics API is unreachable.'
          : 'Loading…'}
      </p>
    )
  }

  const { failures, funnel } = state.data
  const ranked = rankedFailures(failures)
  const total = totalFailures(failures)

  if (funnel.labelled === 0) {
    return <p style={note}>No issues were labelled in this window, so nothing could fail yet.</p>
  }
  if (ranked.length === 0) {
    return (
      <p style={note} data-metric="failures_total">
        Nothing was blocked or failed — all {funnel.labelled} labelled issues are still on a path
        through the pipeline.
      </p>
    )
  }

  const largest = ranked[0].count

  return (
    <div>
      <p style={summaryLine} data-metric="failures_total">
        <strong>{total}</strong> of {funnel.labelled} labelled issues ended blocked or failed
      </p>
      <div style={scroller}>
        <table style={table}>
          <thead>
            <tr>
              <th style={th} scope="col">
                Reason
              </th>
              <th style={th} scope="col">
                Count
              </th>
              <th style={th} scope="col">
                Issues
              </th>
            </tr>
          </thead>
          <tbody>
            {ranked.map((bucket) => (
              <tr key={bucket.reason} data-reason={bucket.reason}>
                <th style={{ ...td, fontWeight: 400 }} scope="row">
                  <code>{bucket.reason}</code>
                  {/* Proportional to the largest bucket, so the shape of the breakdown is legible
                      without reading every number. One accent colour, as the spec requires. */}
                  <div style={{ ...bar, width: `${(bucket.count / largest) * 100}%` }} />
                </th>
                <td style={countCell}>{bucket.count}</td>
                <td style={td}>{bucket.issues.map((issue) => `#${issue}`).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
