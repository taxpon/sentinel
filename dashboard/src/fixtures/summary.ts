// Fixture payloads for the component tests, matching the JSON schema in docs/07-observability.md
// exactly. Panels in T31-T33 should build their own cases from these rather than inventing a
// second idea of what the API returns.

import type { AnalyticsSummary } from '../api'

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
