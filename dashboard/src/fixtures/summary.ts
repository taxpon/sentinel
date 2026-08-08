// Fixture payloads for the component tests, matching the JSON schema in docs/07-observability.md
// exactly. Panels in T31-T33 should build their own cases from these rather than inventing a
// second idea of what the API returns.

import type { AnalyticsSummary } from '../api'
import type { RemediationEvent, RemediationRow } from '../api'

/** The example response from the spec, verbatim. */
export const summaryFixture: AnalyticsSummary = {
  window: { from: '2026-08-01T00:00:00Z', to: '2026-08-08T00:00:00Z' },
  funnel: { labelled: 8, session_created: 8, pr_opened: 7, ci_green: 6, merged: 5 },
  rates: { success: 0.625, merge: 0.714, autonomy: 0.6 },
  durations_seconds: {
    to_pr: { p50: 1980, p90: 3600 },
    to_merge: { p50: 6480, p90: 14400 },
    review_latency: { p50: 2700, p90: 7200 },
  },
  cost: {
    acus_total: 61.4,
    acus_per_merged_fix: 12.3,
    usd_per_fix: 27.6,
    unit_cost_usd: 2.25,
    source: 'devin_consumption_api',
  },
  cycles: { mean: 0.8, distribution: { '0': 3, '1': 1, '2': 1 } },
  throughput: [{ day: '2026-08-06', by_class: { security: 1, 'flaky-test': 1 } }],
  failures: [{ reason: 'requires_upstream_decision', count: 1, issues: [37] }],
  impact: { hours_saved: 21.0, assumption: 'baseline hours per issue class; see docs/05' },
  generated_at: '2026-08-08T04:12:03Z',
}

/**
 * A window in which nothing was labelled. Every rate is a division by zero at the source, so the
 * API sends zeros and the panels must not turn them back into NaN.
 */
export const emptySummaryFixture: AnalyticsSummary = {
  window: { from: '2026-08-01T00:00:00Z', to: '2026-08-08T00:00:00Z' },
  funnel: { labelled: 0, session_created: 0, pr_opened: 0, ci_green: 0, merged: 0 },
  rates: { success: 0, merge: 0, autonomy: 0 },
  durations_seconds: {
    to_pr: { p50: 0, p90: 0 },
    to_merge: { p50: 0, p90: 0 },
    review_latency: { p50: 0, p90: 0 },
  },
  cost: {
    acus_total: 0,
    acus_per_merged_fix: 0,
    usd_per_fix: 0,
    unit_cost_usd: 2.25,
    source: 'derived',
  },
  cycles: { mean: 0, distribution: {} },
  throughput: [],
  failures: [],
  impact: { hours_saved: 0, assumption: 'baseline hours per issue class; see docs/05' },
  generated_at: '2026-08-08T04:12:03Z',
}

/**
 * `GET /api/remediations` — the live table's rows, covering the cases the panel has to tell apart:
 * work in flight and work finished, a loop state carrying a cycle, a terminal row with a
 * `blocked_reason`, and rows with neither a session nor a pull request linked yet.
 *
 * Deliberately out of display order, and ids 12 and 13 share a `labeled_at`: a batch of issues
 * labelled together really does, so the ordering tie is part of the fixture rather than a
 * hypothetical.
 */
export const remediationRowsFixture: RemediationRow[] = [
  {
    id: 10,
    repo: 'taxpon/superset',
    issue_number: 40,
    issue_class: 'dependency',
    state: 'MERGED',
    cycle: 0,
    acus_consumed: 12.3,
    elapsed_seconds: 6480,
    devin_session_url: 'https://app.devin.ai/sessions/devin-4a1b',
    pr_number: 117,
    pr_url: 'https://github.com/taxpon/superset/pull/117',
    blocked_reason: null,
    labeled_at: '2026-08-07T22:00:00Z',
  },
  {
    id: 12,
    repo: 'taxpon/superset',
    issue_number: 42,
    issue_class: 'security',
    state: 'RUNNING',
    cycle: 0,
    acus_consumed: 8.5,
    elapsed_seconds: 1980,
    devin_session_url: 'https://app.devin.ai/sessions/devin-9f2c',
    pr_number: null,
    pr_url: null,
    blocked_reason: null,
    labeled_at: '2026-08-08T03:40:00Z',
  },
  {
    id: 9,
    repo: 'taxpon/superset',
    issue_number: 37,
    issue_class: 'security',
    state: 'BLOCKED',
    cycle: 0,
    acus_consumed: 3.25,
    elapsed_seconds: 900,
    devin_session_url: 'https://app.devin.ai/sessions/devin-2d70',
    pr_number: null,
    pr_url: null,
    blocked_reason: 'requires_upstream_decision',
    labeled_at: '2026-08-07T21:00:00Z',
  },
  {
    id: 13,
    repo: 'taxpon/superset',
    issue_number: 43,
    issue_class: 'typing',
    state: 'QUEUED',
    cycle: 0,
    acus_consumed: 0,
    elapsed_seconds: 12,
    devin_session_url: null,
    pr_number: null,
    pr_url: null,
    blocked_reason: null,
    labeled_at: '2026-08-08T03:40:00Z',
  },
  {
    id: 11,
    repo: 'taxpon/superset',
    issue_number: 41,
    issue_class: 'flaky-test',
    state: 'CI_FAILED',
    cycle: 1,
    acus_consumed: 12.25,
    elapsed_seconds: 5400,
    devin_session_url: 'https://app.devin.ai/sessions/devin-7c31',
    pr_number: 118,
    pr_url: 'https://github.com/taxpon/superset/pull/118',
    blocked_reason: null,
    labeled_at: '2026-08-08T02:10:00Z',
  },
]

/**
 * `GET /api/remediations/{id}` for remediation 12 — the append-only log behind that row.
 *
 * Events 102 and 103 carry an identical `created_at` because they are written in one transaction and
 * `created_at` defaults to `now()`, which is `transaction_timestamp()`. The array is in neither
 * timestamp nor id order, so a panel that forgets to sort, or sorts on the timestamp alone, fails.
 */
export const remediationTimelineFixture: RemediationEvent[] = [
  {
    id: 103,
    remediation_id: 12,
    from_state: 'QUEUED',
    to_state: 'SESSION_CREATED',
    kind: 'devin_call',
    detail: { status: 201, session_id: 'devin-9f2c' },
    created_at: '2026-08-08T03:40:12Z',
  },
  {
    id: 101,
    remediation_id: 12,
    from_state: null,
    to_state: 'QUEUED',
    kind: 'transition',
    detail: { issue: 42 },
    created_at: '2026-08-08T03:40:00Z',
  },
  {
    id: 105,
    remediation_id: 12,
    from_state: 'RUNNING',
    to_state: 'PR_OPENED',
    kind: 'transition',
    detail: { pr_number: 119 },
    created_at: '2026-08-08T04:05:00Z',
  },
  {
    id: 102,
    remediation_id: 12,
    from_state: 'QUEUED',
    to_state: 'SESSION_CREATED',
    kind: 'transition',
    detail: null,
    created_at: '2026-08-08T03:40:12Z',
  },
  {
    id: 104,
    remediation_id: 12,
    from_state: 'SESSION_CREATED',
    to_state: 'RUNNING',
    kind: 'transition',
    detail: null,
    created_at: '2026-08-08T03:41:30Z',
  },
]

/**
 * A window in which work started and cost real ACUs, but nothing merged. Every merge-relative
 * figure — MTTR, review latency, autonomy rate, ACU and cost per fix — divides by `merged`, so the
 * API sends a zero that a panel must not render as a measurement: "$0.00 per fix" next to 24 ACUs
 * of spend, or "0% autonomous", are the two most expensive wrong numbers on this dashboard.
 */
export const unmergedSummaryFixture: AnalyticsSummary = {
  window: { from: '2026-08-01T00:00:00Z', to: '2026-08-08T00:00:00Z' },
  funnel: { labelled: 4, session_created: 4, pr_opened: 2, ci_green: 0, merged: 0 },
  rates: { success: 0, merge: 0, autonomy: 0 },
  durations_seconds: {
    to_pr: { p50: 2400, p90: 3000 },
    to_merge: { p50: 0, p90: 0 },
    review_latency: { p50: 0, p90: 0 },
  },
  cost: {
    acus_total: 24.5,
    acus_per_merged_fix: 0,
    usd_per_fix: 0,
    unit_cost_usd: 2.25,
    source: 'derived',
  },
  cycles: { mean: 1.5, distribution: { '1': 2, '2': 2 } },
  throughput: [],
  failures: [
    { reason: 'requires_upstream_decision', count: 1, issues: [37] },
    { reason: 'max_fix_cycles_exceeded', count: 1, issues: [41] },
  ],
  impact: { hours_saved: 0, assumption: 'baseline hours per issue class; see docs/05' },
 * A busier window (T31). Added because one payload cannot show a formatting or an ordering defect:
 * this one has durations that cross into days, a cost figure the API says is `derived`, and a
 * `throughput` series whose days arrive out of order and whose issue classes differ from day to
 * day. Every figure is internally consistent — `rates` match `funnel`, and the throughput counts
 * sum to `funnel.merged` — so a panel can be asserted against the formulas in
 * docs/07-observability.md rather than against itself.
 */
export const busyWindowSummaryFixture: AnalyticsSummary = {
  window: { from: '2026-08-01T00:00:00Z', to: '2026-08-08T00:00:00Z' },
  funnel: { labelled: 20, session_created: 19, pr_opened: 14, ci_green: 12, merged: 9 },
  rates: { success: 0.45, merge: 0.643, autonomy: 0.444 },
  durations_seconds: {
    to_pr: { p50: 900, p90: 5400 },
    to_merge: { p50: 93600, p90: 172800 },
    review_latency: { p50: 45, p90: 3600 },
  },
  cost: {
    acus_total: 240.5,
    acus_per_merged_fix: 26.7,
    usd_per_fix: 60.08,
    unit_cost_usd: 2.25,
    source: 'derived',
  },
  cycles: { mean: 1.4, distribution: { '0': 4, '1': 3, '2': 2 } },
  throughput: [
    { day: '2026-08-05', by_class: { 'flaky-test': 1, dependency: 3 } },
    { day: '2026-08-03', by_class: { security: 2, 'flaky-test': 1 } },
    { day: '2026-08-04', by_class: { security: 2 } },
  ],
  failures: [
    { reason: 'requires_upstream_decision', count: 2, issues: [37, 44] },
    { reason: 'ci_unfixable', count: 1, issues: [51] },
  ],
  impact: { hours_saved: 54.5, assumption: 'baseline hours per issue class; see docs/05' },
  generated_at: '2026-08-08T04:12:03Z',
}
