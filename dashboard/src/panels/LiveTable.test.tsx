import { act, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { POLL_INTERVAL_MS, type RemediationRow, type RemediationState, type RemediationsFetcher } from '../api'
import { remediationRowsFixture } from '../fixtures/summary'
import LiveTable, { isTerminal, orderRemediations, slot, title } from './LiveTable'

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function renderTable(fetcher: RemediationsFetcher) {
  vi.useFakeTimers()
  const result = render(<LiveTable options={{ fetcher }} />)
  await act(async () => {})
  return result
}

const resolving = (rows: RemediationRow[] = remediationRowsFixture): RemediationsFetcher =>
  vi.fn().mockResolvedValue(rows)

const RUNNING_ROW = remediationRowsFixture.find((candidate) => candidate.id === 12) as RemediationRow

function row(overrides: Partial<RemediationRow>): RemediationRow {
  return { ...RUNNING_ROW, ...overrides }
}

/** The issue identifier in each row's header cell, top to bottom — the rendered order. */
function renderedOrder(): string[] {
  return [...document.querySelectorAll('tbody th')].map((cell) => cell.textContent ?? '')
}

const ALL_STATES: RemediationState[] = [
  'QUEUED',
  'SESSION_CREATED',
  'RUNNING',
  'PR_OPENED',
  'CI_RUNNING',
  'CI_FAILED',
  'CI_PASSED',
  'IN_REVIEW',
  'CHANGES_REQUESTED',
  'MERGED',
  'BLOCKED',
  'FAILED',
]

describe('the panel module', () => {
  it('registers itself in the table slot', () => {
    expect(slot).toBe('table')
    expect(title).toBe('Live remediation table')
  })
})

describe('rendering a representative payload', () => {
  it('shows the columns the spec names, one row per remediation', async () => {
    await renderTable(resolving())

    expect([...document.querySelectorAll('thead th')].map((cell) => cell.textContent)).toEqual([
      'Issue',
      'Class',
      'State',
      'Cycle',
      'ACUs',
      'Age',
      'Verify at source',
    ])
    expect(document.querySelectorAll('tbody tr')).toHaveLength(remediationRowsFixture.length)

    const running = screen.getByRole('row', { name: /superset#42/ })
    expect(within(running).getByText('security')).toBeInTheDocument()
    expect(within(running).getByText('Running')).toBeInTheDocument()
    // 1980 seconds since labeled_at, not the raw number.
    expect(within(running).getByText('33m')).toBeInTheDocument()
    expect(within(running).getByText('8.5')).toBeInTheDocument()
  })

  it('links every row to both the Devin session and the pull request', async () => {
    await renderTable(resolving())

    const failing = screen.getByRole('row', { name: /superset#41/ })
    expect(within(failing).getByRole('link', { name: 'Devin session for issue 41' })).toHaveAttribute(
      'href',
      'https://app.devin.ai/sessions/devin-7c31',
    )
    const pr = within(failing).getByRole('link', { name: 'Pull request for issue 41' })
    expect(pr).toHaveAttribute('href', 'https://github.com/taxpon/superset/pull/118')
    expect(pr).toHaveTextContent('PR #118')
    // Verifying a claim at source must not cost the reader the dashboard they were watching.
    expect(pr).toHaveAttribute('target', '_blank')
  })

  it('says what is missing instead of rendering a link to nowhere', async () => {
    await renderTable(resolving())

    const queued = screen.getByRole('row', { name: /superset#43/ })
    expect(within(queued).queryAllByRole('link')).toHaveLength(0)
    expect(within(queued).getByText('No Devin session for issue 43 yet')).toBeInTheDocument()
    expect(within(queued).getByText('No pull request for issue 43 yet')).toBeInTheDocument()

    // A session exists here, the pull request does not yet.
    const running = screen.getByRole('row', { name: /superset#42/ })
    expect(within(running).queryAllByRole('link')).toHaveLength(1)
    expect(within(running).getByText('No pull request for issue 42 yet')).toBeInTheDocument()
  })

  it('shows the cycle count, which is the whole point of the review-fix loop', async () => {
    await renderTable(resolving())

    const failing = screen.getByRole('row', { name: /superset#41/ })
    expect(within(failing).getByText('CI failed')).toBeInTheDocument()
    expect(within(failing).getByText('1')).toBeInTheDocument()
  })
})

describe('every state a row can be in', () => {
  it('renders a label for each, and marks the terminal ones as terminal', async () => {
    await renderTable(
      resolving(
        ALL_STATES.map((state, index) => row({ id: 100 + index, issue_number: 100 + index, state })),
      ),
    )

    const labels = [
      'Queued',
      'Session created',
      'Running',
      'PR opened',
      'CI running',
      'CI failed',
      'CI passed',
      'In review',
      'Changes requested',
      'Merged',
      'Blocked',
      'Failed',
    ]
    for (const label of labels) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }

    for (const state of ALL_STATES) {
      const rendered = document.querySelector(`tr[data-state="${state}"]`)
      expect(rendered, `no row rendered for ${state}`).not.toBeNull()
      expect(rendered).toHaveAttribute('data-terminal', String(isTerminal(state)))
    }
  })

  it('agrees with the state machine about which states are terminal', () => {
    expect(ALL_STATES.filter(isTerminal)).toEqual(['MERGED', 'BLOCKED', 'FAILED'])
  })

  it('shows why a terminal row stopped, rather than only that it did', async () => {
    await renderTable(resolving())

    const blocked = screen.getByRole('row', { name: /superset#37/ })
    expect(within(blocked).getByText('Blocked')).toBeInTheDocument()
    expect(within(blocked).getByText('requires_upstream_decision')).toBeInTheDocument()
  })
})

describe('the empty, loading and error states', () => {
  it('explains an empty window instead of showing a blank box', async () => {
    // What the demo shows before the first issue is labelled.
    await renderTable(resolving([]))

    expect(screen.getByText(/No remediations yet/)).toBeInTheDocument()
    expect(document.querySelector('table')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says it is loading until the first response lands', () => {
    vi.useFakeTimers()
    render(<LiveTable options={{ fetcher: () => new Promise(() => {}) }} />)

    expect(screen.getByText('Loading remediations…')).toBeInTheDocument()
  })

  it('reports an unreachable API when it has nothing to show', async () => {
    await renderTable(vi.fn().mockRejectedValue(new Error('offline')))

    expect(screen.getByRole('alert')).toHaveTextContent('Could not reach the remediations API.')
    expect(document.querySelector('table')).toBeNull()
  })

  it('keeps the last good rows when a poll fails, rather than blanking the table', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(remediationRowsFixture)
      .mockRejectedValue(new Error('offline'))
    await renderTable(fetcher)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not reach the remediations API — showing the last successful update.',
    )
    expect(screen.getByRole('row', { name: /superset#42/ })).toBeInTheDocument()
  })
})

describe('the age column', () => {
  it('renders seconds as a duration a human reads, at every scale', async () => {
    const elapsed = [0, 45, 90, 3600, 6480, 90_000]
    await renderTable(
      resolving(
        elapsed.map((seconds, index) =>
          row({ id: 200 + index, issue_number: 200 + index, elapsed_seconds: seconds }),
        ),
      ),
    )

    for (const [index, formatted] of ['0s', '45s', '1m', '1h', '1h 48m', '1d 1h'].entries()) {
      const rendered = screen.getByRole('row', { name: new RegExp(`superset#${200 + index}\\b`) })
      expect(within(rendered).getByText(formatted)).toBeInTheDocument()
    }
  })
})

describe('row order', () => {
  it('puts work in flight above work that has finished, newest first', () => {
    expect(orderRemediations(remediationRowsFixture).map((each) => each.id)).toEqual([
      13, 12, 11, 10, 9,
    ])
  })

  it('breaks a tie on labeled_at with the id, so equal timestamps do not sort arbitrarily', () => {
    // Ids 12 and 13 share a labeled_at: a batch of issues labelled together really does.
    const tied = remediationRowsFixture.filter((each) => each.id === 12 || each.id === 13)

    expect(orderRemediations(tied).map((each) => each.id)).toEqual([13, 12])
    expect(orderRemediations([...tied].reverse()).map((each) => each.id)).toEqual([13, 12])
  })

  it('does not reshuffle when the next poll returns the same rows in another order', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(remediationRowsFixture)
      .mockResolvedValue([...remediationRowsFixture].reverse())
    await renderTable(fetcher)

    const before = renderedOrder()
    expect(before).toEqual([
      'taxpon/superset#43',
      'taxpon/superset#42',
      'taxpon/superset#41',
      'taxpon/superset#40',
      'taxpon/superset#37',
    ])

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(renderedOrder()).toEqual(before)
  })

  it('moves a remediation down as it reaches a terminal state', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce([row({ id: 13, issue_number: 43, state: 'QUEUED' }), RUNNING_ROW])
      .mockResolvedValue([row({ id: 13, issue_number: 43, state: 'MERGED' }), RUNNING_ROW])
    await renderTable(fetcher)

    expect(renderedOrder()).toEqual(['taxpon/superset#43', 'taxpon/superset#42'])

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })

    expect(renderedOrder()).toEqual(['taxpon/superset#42', 'taxpon/superset#43'])
  })
})
