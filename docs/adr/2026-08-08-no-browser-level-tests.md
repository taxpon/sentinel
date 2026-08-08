---
title: Test the dashboard with component tests only, never a real browser
status: accepted
date: 2026-08-08
type: process
areas: [dashboard]
tasks: [T30, T31, T32, T33]
files:
  - .claude/rules/ui-testing.md
specs:
  - docs/08-testing.md
  - docs/07-observability.md
supersedes:
---

# Test the dashboard with component tests only, never a real browser

## Context

The dashboard is four panel groups over one JSON endpoint whose shape is fixed in
`docs/07-observability.md`. It renders numbers, formats durations and currency, and shows an empty
or error state when there is nothing to draw. It has no routing, no forms, no authentication flow
and no cross-page state.

The original rule made browser-level tests a case-by-case decision: a session had to propose one on
the issue and wait for a human answer. That put a synchronous human step in the middle of an
otherwise parallel workflow, and it invited the proposal to be made repeatedly, once per UI task.

The wider constraint is that this repository is built by parallel agent sessions on a metered
budget. A Playwright suite costs browser downloads in CI, a much longer feedback loop, and a
standing maintenance tax paid in flaky retries — and every one of those costs is paid on every task
that follows, not once.

## Decision

Component tests only, using Vitest and React Testing Library. No Playwright, no Cypress, no
screenshot or visual-regression comparison, and no test that boots the API and the dashboard
together. Sessions do not propose them either — proposing was itself part of the cost.

This is scoped to the dashboard. `tests/test_e2e.py` (T34) is unaffected: it drives the pipeline
in-process with every external faked, exercises the review-fix loop, and remains the most important
test in the repository. "End-to-end" names two different things here, and only the browser one is
being dropped.

Where a component test cannot reach something, the pull request says which gap is left uncovered.
An acknowledged gap is acceptable; a weaker test presented as covering it is not.

## Alternatives considered

| Option | Why not |
|---|---|
| Ask per task, as before | A synchronous human decision inside a parallel workflow, re-asked once per UI task, for an answer that was always going to be the same |
| A single smoke test — load the page, assert the KPI row renders | Still pays the whole setup cost (browser in CI, driver, fixtures, a second runner) for one assertion that a component test already makes |
| Screenshot comparison for the layout constraints in `docs/07` | Those constraints — KPI row visible at 1440px, no chart over 240px — are the most flake-prone thing to assert visually, and they are design intent a reviewer checks by looking, not a regression risk |
| Drop UI tests entirely | Number and duration formatting is where this kind of panel actually breaks, and it is the cheapest thing to test |

## Consequences

CI stays fast and the dashboard tasks stay parallelisable, with no human in the loop for a decision
whose answer does not vary by task. The rule is now a flat statement rather than a judgement call,
so five sessions cannot reach five conclusions.

What is knowingly given up: nothing verifies that the panels render correctly in a real browser
engine, that the layout holds at 1440px, or that the panels compose without overlapping. Those are
checked by a human looking at the running dashboard during the demo, which is also when they would
matter.

**What would tell us this was wrong:** a rendering defect reaches the demo that a browser test would
have caught — a panel that works in jsdom and breaks in Chrome, or a layout that collapses at the
target width. One such defect is affordable; a second means jsdom is no longer a sufficient
approximation of the target and a narrow smoke test earns its cost.
