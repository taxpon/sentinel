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

## Ask before starting — browser-level tests

**Do not begin any of the following without asking on the issue and receiving an answer:**

- Playwright, Cypress or any real-browser driver;
- screenshot or visual-regression comparison;
- a test that boots the API and the dashboard together.

These are legitimate techniques, but they carry a large token and runtime cost, and whether that
cost is worth paying is a human decision, not a session-local one.

To ask:

```bash
gh issue comment <N> -R taxpon/sentinel --body "Proposing a browser-level test for <what>, because <why>. \
Component tests cover <what they cover> but cannot cover <the gap>. Estimated scope: <n> files. Proceed?"
```

Then continue with the rest of the task. Do not block on the answer if other work remains, and do
not silently substitute a browser test with a weaker component test — say which gap is left
uncovered.

## Layout constraints from the spec

`docs/07-observability.md` fixes these; tests should not contradict them.

- The KPI row is visible at 1440px without scrolling.
- No chart taller than 240px.
- One accent colour; per-class colours reserved for stacked series.
- Freshness is always visible — `generated_at` rendered as "updated Ns ago", amber past 30s.
