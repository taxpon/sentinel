# Testing

> **Status:** Design · **Answers:** What is tested, at which layer, and what counts as evidence?

Testing happens at two layers, and both are required. Passing tests in the orchestrator say nothing
about whether a Superset defect was actually fixed; a green PR says nothing about whether the
orchestrator handles a replayed webhook.

| Layer | Subject | Evidence |
|---|---|---|
| **Orchestrator** | Sentinel's own code | `pytest` green in this repository's CI |
| **Remediation** | Each fix Devin makes to Superset | CI green **on the pull request itself**, including a regression test that fails without the fix |

## Orchestrator tests

`pytest` + `pytest-asyncio`, `respx` for the Devin and GitHub HTTP layers, `httpx.ASGITransport`
for in-process API calls, and a real Postgres from Compose (the queue relies on
`FOR UPDATE SKIP LOCKED`, which SQLite cannot emulate).

| Area | Cases | Approach |
|---|---|---|
| Signature verification | valid signature; wrong secret; tampered body; missing header; malformed prefix; oversized body | Known HMAC vectors against `verify_signature` directly |
| Delivery deduplication | same `delivery_id` twice → one row, one job, `200` on the second | ASGI client, assert on DB |
| Domain idempotency | label added twice, and label + comment on the same issue → exactly one `remediation` and one session | Concurrent requests, assert unique-constraint behaviour |
| Event mapping | each subscribed event → expected intent; unknown event → `ignored`, never a 5xx | Recorded GitHub payload fixtures |
| State machine | every legal transition; illegal transitions raise; terminal states absorb late webhooks; `cycle` monotonic | Table-driven over the transition matrix in [04](./04-state-machine.md) |
| Devin client | v3 request shape — path, headers, tags, `structured_output_schema`, `max_acu_limit`, `resumable`; response parsing; `429` backoff; `4xx` fails without retry | `respx`, asserting on the captured request body |
| Concurrency policy | at the cap → job deferred, not failed; deferral does not consume the retry budget | Seeded in-flight remediations |
| Budget policy | over `DAILY_ACU_BUDGET` → `BLOCKED` with the right reason and an escalation job | Seeded `acu_ledger` |
| Job queue | two workers claim disjoint jobs; expired lease reclaimed; backoff schedule correct | Two concurrent sessions against real Postgres |
| Poller | status → state mapping for all seven Devin statuses; PR discovered from `pull_requests[]`; ACU and structured output reconciled; reconciliation idempotent | `respx` |
| Escalation | `outcome: blocked` → `BLOCKED`, issue comment, `needs-human` label | Fake GitHub, assert on calls |
| Analytics | funnel, rates, p50/p90, cycle counts, cost, autonomy rate over a fixture event log with hand-computed expected values | Pure functions over seeded rows |
| End-to-end | label → job → fake Devin session → PR opened → check suite **fails** → resume message sent with `cycle:1` → check suite passes → merged; then assert the analytics response | Full ASGI + worker + poller, all externals faked |

The end-to-end case is the one that matters most: it exercises the review-fix loop, which is the
part of the system that would otherwise only be demonstrated by hand.

**Not tested:** Devin's own behaviour. `respx` asserts what Sentinel *sends*; whether Devin
produces a good patch is verified at the remediation layer, on real pull requests.

## Remediation acceptance criteria

A remediation is only counted as successful when all of these hold:

1. The pull request contains a **regression test** that fails without the fix and passes with it.
2. CI is green **on the pull request**, not merely locally.
3. `structured_output.tests.added` is non-empty; an empty array is flagged for manual review
   ([05](./05-devin-integration.md)).
4. `structured_output.root_cause` describes *why* the defect existed, not what was edited.
5. The PR is **merged**, not just opened.

Portfolio-level criteria, checked once across all eight remediations:

- at least six distinct issue classes represented ([01](./01-overview.md));
- at least two showing genuine diagnosis rather than a version bump;
- no two remediations attacking the same underlying problem.

## CI for this repository

`.github/workflows/ci.yml`, on push and pull request:

1. `ruff check` and `ruff format --check`
2. `mypy src/sentinel`
3. `pytest` against a Postgres service container, with coverage reported
4. `npm run build` and `npm test` for `dashboard/`
5. `docker compose config` validation

## CI on the fork

Superset ships 49 workflows, several of which (e2e, Playwright, Helm) take tens of minutes — too
slow to serve as the feedback signal inside the review-fix loop. The fork therefore adds one
lightweight workflow, `devin-autofix-ci.yml`, that runs on pull requests:

- `pre-commit` on changed files;
- `pytest` scoped to the test paths touched by the diff;
- `npm test` scoped to changed frontend packages.

This is a **deliberate narrowing of the CI signal**, made to keep the loop fast enough to
demonstrate, and it is stated as such rather than presented as full-suite validation
([B2](./blockers.md)). Where a remediation touches an area covered by a heavier workflow, that
workflow is run on the PR before merge.
