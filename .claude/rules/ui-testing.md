---
paths:
  - "dashboard/**"
---

# UI testing rules

Rule 2 in `CLAUDE.md`: UI changes carry UI tests. The line between "write it" and "ask first" is
drawn here so that every session draws it in the same place.

## Always required — component tests

Vitest + React Testing Library, co-located as `dashboard/src/**/*.test.tsx`. Every panel gets:

- **Rendering** from a representative `/api/analytics/summary` payload;
- **Prop branches** — each distinct visual state the component can take;
- **Empty state** — zero remediations, an empty window. Panels must not show `NaN`, `Infinity` or a
  blank box;
- **Error and loading states** — the API is unreachable or slow;
- **Number and duration formatting** — percentages, currency, and seconds rendered as human
  durations. Formatting bugs are the most common defect in this kind of panel and the cheapest to
  catch;
- **Provenance labelling**, where applicable — the cost panel must show whether a figure came from
  Devin or was derived locally.

Use fixture payloads, not live data. Keep them next to the tests.

## Out of scope — browser-level tests

**Do not write any of these. Do not propose them either; the question is settled.**

- Playwright, Cypress or any real-browser driver;
- screenshot or visual-regression comparison;
- a test that boots the API and the dashboard together.

The cost is not worth what they would add here
([ADR](../../docs/adr/2026-08-08-no-browser-level-tests.md)).

This is a decision about the **dashboard only**. It does not touch `tests/test_e2e.py` (T34), which
drives the whole pipeline in-process with every external faked and is the most important test in
the repository.

Where a component test genuinely cannot reach something — real browser layout, cross-panel
interaction — **say so in the pull request** rather than substituting a weaker test and calling the
gap covered. A gap that is written down is fine; one that is papered over is not.

## Layout constraints from the spec

`docs/07-observability.md` fixes these; tests should not contradict them.

- The KPI row is visible at 1440px without scrolling.
- No chart taller than 240px.
- One accent colour; per-class colours reserved for stacked series.
- Freshness is always visible — `generated_at` rendered as "updated Ns ago", amber past 30s.
