// The panel that proves the system is running. One row per remediation, with the state it is in,
// how long it has been going, and the two links — Devin session and pull request — that let any
// claim on this dashboard be checked at source (docs/07-observability.md#dashboard).
//
// It polls /api/remediations itself through useRemediations rather than reading the summary the
// shell hands every panel: the rows are a second endpoint, not a slice of the summary payload.

import { useMemo } from 'react'
import type { CSSProperties } from 'react'
import {
  NO_VALUE,
  formatDurationSeconds,
  formatNumber,
  showData,
  useRemediations,
  type PanelSlot,
  type RemediationRow,
  type RemediationState,
  type UseRemediationsOptions,
} from '../api'

/** Terminal states: docs/04-state-machine.md invariant 1 — no transition leaves these three. */
const TERMINAL_STATES: readonly RemediationState[] = ['MERGED', 'BLOCKED', 'FAILED']

export function isTerminal(state: RemediationState): boolean {
  return TERMINAL_STATES.includes(state)
}

/** The machine names are shouted; a dashboard read by a leader is not. `data-state` keeps the raw one. */
const STATE_LABELS: Record<RemediationState, string> = {
  QUEUED: 'Queued',
  SESSION_CREATED: 'Session created',
  RUNNING: 'Running',
  PR_OPENED: 'PR opened',
  CI_RUNNING: 'CI running',
  CI_FAILED: 'CI failed',
  CI_PASSED: 'CI passed',
  IN_REVIEW: 'In review',
  CHANGES_REQUESTED: 'Changes requested',
  MERGED: 'Merged',
  BLOCKED: 'Blocked',
  FAILED: 'Failed',
}

export function stateLabel(state: RemediationState): string {
  return STATE_LABELS[state]
}

/**
 * Five tones drawn only from the accent, amber and danger tokens. The `--series-*` palette is
 * reserved for stacked series and stays out of here, so a state badge can never be mistaken for an
 * issue class in the throughput chart.
 */
type StateTone = 'working' | 'waiting' | 'attention' | 'danger' | 'merged'

const STATE_TONES: Record<RemediationState, StateTone> = {
  QUEUED: 'waiting',
  SESSION_CREATED: 'waiting',
  RUNNING: 'working',
  PR_OPENED: 'waiting',
  CI_RUNNING: 'working',
  CI_FAILED: 'attention',
  CI_PASSED: 'waiting',
  IN_REVIEW: 'waiting',
  CHANGES_REQUESTED: 'attention',
  MERGED: 'merged',
  BLOCKED: 'danger',
  FAILED: 'danger',
}

const BADGE_BASE: CSSProperties = {
  display: 'inline-block',
  padding: '1px 8px',
  borderRadius: 'var(--radius)',
  border: '1px solid transparent',
  fontSize: '12px',
  fontWeight: 600,
  whiteSpace: 'nowrap',
}

const TONE_STYLES: Record<StateTone, CSSProperties> = {
  waiting: { color: 'var(--muted)', background: 'var(--bg)', borderColor: 'var(--border)' },
  working: { color: 'var(--accent)', background: 'var(--accent-soft)', borderColor: 'var(--accent)' },
  attention: { color: 'var(--amber)', background: 'var(--amber-soft)', borderColor: 'var(--amber)' },
  danger: { color: 'var(--danger)', background: 'var(--danger-soft)', borderColor: 'var(--danger)' },
  merged: { color: 'var(--surface)', background: 'var(--accent)', borderColor: 'var(--accent)' },
}

export function stateBadgeStyle(state: RemediationState): CSSProperties {
  return { ...BADGE_BASE, ...TONE_STYLES[STATE_TONES[state]] }
}

/**
 * In-flight rows first, so what is happening right now is at the top where the demo looks, then
 * most recently labelled first inside each group.
 *
 * `id` breaks the final tie. `labeled_at` is the webhook's timestamp, and a batch of issues labelled
 * together carries the same one; leaving that tie to the sort would let the table reshuffle itself
 * between polls, which reads as the dashboard glitching rather than as work moving.
 */
export function orderRemediations(rows: readonly RemediationRow[]): RemediationRow[] {
  return [...rows].sort(
    (a, b) =>
      Number(isTerminal(a.state)) - Number(isTerminal(b.state)) ||
      Date.parse(b.labeled_at) - Date.parse(a.labeled_at) ||
      b.id - a.id,
  )
}

const TABLE_STYLE: CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  fontVariantNumeric: 'tabular-nums',
}

const HEAD_CELL: CSSProperties = {
  textAlign: 'left',
  padding: '4px 8px',
  borderBottom: '1px solid var(--border)',
  color: 'var(--muted)',
  fontSize: '12px',
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: '0.04em',
}

const CELL: CSSProperties = {
  textAlign: 'left',
  padding: '6px 8px',
  borderBottom: '1px solid var(--border)',
  fontWeight: 400,
  verticalAlign: 'top',
}

const NUMERIC_CELL: CSSProperties = { ...CELL, textAlign: 'right' }

const REASON_STYLE: CSSProperties = { display: 'block', marginTop: '2px', color: 'var(--muted)' }

const LINKS_CELL: CSSProperties = { ...CELL, whiteSpace: 'nowrap' }

const MISSING_LINK_STYLE: CSSProperties = { color: 'var(--muted)' }

const COLUMNS = ['Issue', 'Class', 'State', 'Cycle', 'ACUs', 'Age', 'Verify at source'] as const

function MissingLink({ label }: { label: string }) {
  return (
    <span style={MISSING_LINK_STYLE}>
      <span aria-hidden="true">{NO_VALUE}</span>
      <span className="visually-hidden">{label}</span>
    </span>
  )
}

function Row({ row }: { row: RemediationRow }) {
  const terminal = isTerminal(row.state)
  return (
    <tr
      data-state={row.state}
      data-terminal={String(terminal)}
      // Terminal work is history: it stays visible, because a system that hides its failures cannot
      // be evaluated, but it does not compete with what is in flight.
      style={terminal ? { color: 'var(--muted)' } : undefined}
    >
      <th scope="row" style={CELL}>
        {row.repo}#{row.issue_number}
      </th>
      <td style={CELL}>{row.issue_class}</td>
      <td style={CELL}>
        <span style={stateBadgeStyle(row.state)}>{stateLabel(row.state)}</span>
        {row.blocked_reason != null && <small style={REASON_STYLE}>{row.blocked_reason}</small>}
      </td>
      <td style={NUMERIC_CELL}>{row.cycle}</td>
      <td style={NUMERIC_CELL}>{formatNumber(row.acus_consumed)}</td>
      <td style={NUMERIC_CELL}>{formatDurationSeconds(row.elapsed_seconds)}</td>
      <td style={LINKS_CELL}>
        {/* Loose equality throughout this row: `fetchRemediations` casts the body rather than
            validating it, so a field the endpoint omits arrives as `undefined`. A strict check would
            take the link branch and render an anchor with no href — the broken link `MissingLink`
            exists to prevent. */}
        {row.devin_session_url == null ? (
          <MissingLink label={`No Devin session for issue ${row.issue_number} yet`} />
        ) : (
          <a
            href={row.devin_session_url}
            target="_blank"
            rel="noreferrer"
            aria-label={`Devin session for issue ${row.issue_number}`}
          >
            Session
          </a>
        )}
        {' · '}
        {row.pr_url == null ? (
          <MissingLink label={`No pull request for issue ${row.issue_number} yet`} />
        ) : (
          <a
            href={row.pr_url}
            target="_blank"
            rel="noreferrer"
            aria-label={`Pull request for issue ${row.issue_number}`}
          >
            PR #{row.pr_number}
          </a>
        )}
      </td>
    </tr>
  )
}

export interface LiveTableProps {
  /** Injected by the tests. The shell passes only `state`, so this stays at its default there. */
  options?: UseRemediationsOptions
}

export default function LiveTable({ options }: LiveTableProps) {
  const remediations = useRemediations(options)

  const rows = useMemo(
    () => (showData(remediations) ? orderRemediations(remediations.data) : []),
    [remediations],
  )

  return (
    <>
      {remediations.status === 'error' && (
        <p className="banner banner--error" role="alert">
          Could not reach the remediations API
          {showData(remediations) ? ' — showing the last successful update.' : '.'}
        </p>
      )}

      {/* `showData` rather than a branch on `status`: a failed poll annotates the last good rows
          instead of blanking a table someone is watching. */}
      {!showData(remediations) ? (
        remediations.status === 'loading' && <p className="slot-empty">Loading remediations…</p>
      ) : rows.length === 0 ? (
        <p className="slot-empty">
          No remediations yet. Label an issue <code>devin:autofix</code> to start one.
        </p>
      ) : (
        <table style={TABLE_STYLE}>
          <caption className="visually-hidden">
            Remediations, work in flight first, then completed
          </caption>
          <thead>
            <tr>
              {COLUMNS.map((column) => (
                <th key={column} scope="col" style={HEAD_CELL}>
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Row key={row.id} row={row} />
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

// Annotated, so a typo in the slot name is a compile error here rather than a panel that silently
// fails the shell's runtime check and never appears.
export const slot: PanelSlot = 'table'
export const title = 'Live remediation table'
export const order = 1
