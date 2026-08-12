# Run log

> **Status:** Living · **Answers:** What happened when the pipeline was run against `taxpon/superset`, what the figures mean, and which of them not to trust?

Ten issues on [`taxpon/superset`](https://github.com/taxpon/superset) were labelled `devin:autofix`
between 2026-08-10 and 2026-08-12. Nine reached a merged pull request; one escalated. This is the
record of what each remediation did, what broke while they ran, and where the evidence stops.

It is the audit trail for the submission, not a narrative. Every figure below is either read from
the live API or recomputed from the append-only event log, and where the two disagree the
disagreement is stated rather than resolved.

**All times are UTC.** Metric definitions are in [07](./07-observability.md#metric-definitions) and
are not restated here; this document records the values and the conditions they were measured
under. The system is at `https://sentinel-alpha.fly.dev`; the API paths cited are
`/api/analytics/summary`, `/api/remediations` and `/api/remediations/{id}`.

## Before this run, and why it is a re-run

The ten remediations below are not the first time the pipeline was pointed at these issues, and the
earlier attempts are not in the figures. Recording the boundary rather than letting the scope
sentence imply there was none:

| When (08-10) | What |
|---|---|
| 08:50 → 09:16 | Issue #5's first attempt opened [PR 9](https://github.com/taxpon/superset/pull/9) and it was merged |
| 10:59 | [PR 10](https://github.com/taxpon/superset/pull/10) reverted it |
| 12:15 → 13:42 | A second attempt opened [PR 11](https://github.com/taxpon/superset/pull/11), closed unmerged |
| 13:49 | The re-run recorded here begins |

The first attempt exposed a specification bug rather than an implementation one. Sentinel read "some
check suite succeeded" as "CI is green"; on a fork carrying inherited workflows the suite that moved
it was a label check, three seconds after the pull request opened, and a `Dependency Review` failure
that is environmental on this fork was then read as a CI failure. That resumed the session against
something unfixable, spent a fix cycle, and ended at `BLOCKED`. Every mechanism behaved as
specified. The specification was wrong, and it was corrected before this run
([ADR](./adr/2026-08-10-ci-green-is-the-aggregate-of-the-check-runs.md),
[ADR](./adr/2026-08-10-a-merge-is-recorded-from-any-state.md)).

The database was reset and all of the issues re-labelled under the corrected logic. The pull
requests from the earlier attempts were reverted or closed and are left in the fork's history rather
than deleted; issue #5 therefore carries more Devin sessions than its one remediation row suggests.

## The run at a glance

| Issue | Class | Labelled | State | Cycles | Pull request | Devin session |
|---|---|---|---|---|---|---|
| [5](https://github.com/taxpon/superset/issues/5) | `flaky-test` | 08-10 13:49:04 | MERGED | 0 | [12](https://github.com/taxpon/superset/pull/12) | [`2845d941…`](https://app.devin.ai/sessions/2845d941eed648d18cd055c0de78515f) |
| [1](https://github.com/taxpon/superset/issues/1) | `security` | 08-10 14:09:43 \* | MERGED | 0 | [14](https://github.com/taxpon/superset/pull/14) | [`610abd65…`](https://app.devin.ai/sessions/610abd6545ad4b0383d9d70ef2cd4097) |
| [2](https://github.com/taxpon/superset/issues/2) | `security-dep` | 08-10 14:11:08 \* | MERGED | 0 | [13](https://github.com/taxpon/superset/pull/13) | [`ee9a4751…`](https://app.devin.ai/sessions/ee9a475123c449a185e991dc3381ff5a) |
| [3](https://github.com/taxpon/superset/issues/3) | `bug` | 08-11 22:22:06 | MERGED | 1 | [17](https://github.com/taxpon/superset/pull/17) | [`c5ad74a9…`](https://app.devin.ai/sessions/c5ad74a9d8c046e18e022007021c8c15) |
| [18](https://github.com/taxpon/superset/issues/18) | `flaky-test` | 08-11 23:17:47 | MERGED | 0 | [19](https://github.com/taxpon/superset/pull/19) | [`0955880b…`](https://app.devin.ai/sessions/0955880b79f948c88ca6457bfc04d7f8) |
| [4](https://github.com/taxpon/superset/issues/4) | `frontend-dep` | 08-11 23:56:23 | **BLOCKED** | 0 | — | [`46c2990a…`](https://app.devin.ai/sessions/46c2990ad57b41fe9836d5c2bff1d5ba) |
| [7](https://github.com/taxpon/superset/issues/7) | `deprecation` | 08-11 23:59:12 | MERGED | 3 | [20](https://github.com/taxpon/superset/pull/20) | [`cfc8aece…`](https://app.devin.ai/sessions/cfc8aeced0e545c7b53aa05b4ec83929) |
| [6](https://github.com/taxpon/superset/issues/6) | `perf` | 08-12 02:30:08 | MERGED | 0 | [22](https://github.com/taxpon/superset/pull/22) | [`f20aa3fb…`](https://app.devin.ai/sessions/f20aa3fb2f454def9905705f51b0ec3e) |
| [8](https://github.com/taxpon/superset/issues/8) | `typing` | 08-12 02:30:23 | MERGED | 0 | [23](https://github.com/taxpon/superset/pull/23) | [`3cfab232…`](https://app.devin.ai/sessions/3cfab232455a4213bf10b9347b71dfe7) |
| [21](https://github.com/taxpon/superset/issues/21) | `flaky-test` | 08-12 02:48:07 | MERGED | 0 | [24](https://github.com/taxpon/superset/pull/24) | [`e9f6dd20…`](https://app.devin.ai/sessions/e9f6dd20b509413db42bac3e750420ae) |

\* `labeled_at` is Sentinel's receipt clock, not GitHub's label event. For #1 and #2 the original
deliveries failed and the clock starts at the redelivery — see
[incident 1](#1--the-database-ran-out-of-memory-and-two-webhook-deliveries-were-lost) and the
[caveat below](#which-figures-not-to-trust).

**Issues 18 and 21 were not on the original list of eight.** Both were surfaced by the pipeline
while it was remediating something else, filed as ordinary issues, and then run through the same
pipeline. #18 came out of #3's fix cycle; #21 came out of PR 19, which diagnosed the underlying leak
and deliberately left it alone. That chain is described under
[the two fix-loop runs](#the-two-fix-loop-runs).

## The figures

Read from `/api/analytics/summary?window=7d` on 2026-08-12, the response's own `generated_at` being
03:34:43:

```
funnel      labelled 10 → session_created 10 → pr_opened 9 → ci_green 9 → merged 9
rates       success 0.9   merge 1.0   autonomy 0.778
durations   to_pr          p50 1128 s   p90 2654 s
            to_merge       p50 2212 s   p90 8992 s
            review_latency p50  185 s   p90  542 s
cycles      mean 0.4, distribution {0: 8, 1: 1, 3: 1}
failures    [{reason: session_waiting_for_user, count: 1, issues: [4]}]
impact      hours_saved 32.0
throughput  08-10 flaky-test, security, security-dep
            08-11 bug, flaky-test
            08-12 deprecation, flaky-test, perf, typing
```

Seven distinct classes merged: `security`, `security-dep`, `flaky-test`, `bug`, `deprecation`,
`perf`, `typing`. The eighth, `frontend-dep`, is the escalation.

### What the figures are computed under

- `success` is 9/10, `merge` is 9/9, `autonomy` is 7/9 = 0.778. The two remediations `autonomy`
  excludes are #3 and #7, the only ones that used the fix loop.
- Percentiles are nearest-rank, so each one is a duration some remediation actually took
  ([ADR](./adr/2026-08-08-percentiles-are-nearest-rank-observations.md)). `to_pr` p90 and
  `to_merge` p90 are both #7; `to_pr` p50 and `review_latency` p50 are both #3.
- `ci_green_at` is the first moment the whole head SHA was green, not the first check suite to
  conclude ([ADR](./adr/2026-08-10-ci-green-is-the-aggregate-of-the-check-runs.md)). Every figure
  here was recorded after that change, so all ten are comparable with each other and none is
  comparable with anything recorded before 2026-08-10.
- The window is `2026-08-05T03:34:43Z` to `2026-08-12T03:34:43Z` and is anchored to the moment the
  query runs, selecting on `labeled_at`
  ([ADR](./adr/2026-08-08-the-window-selects-remediations-by-labeled-at.md)). **The same request
  made later returns different figures**, because remediations drop out of the left-hand edge —
  after 2026-08-17 a `7d` window contains none of this run. The block above is a reading, not a
  constant.
- Every timing above was recomputed independently from `/api/remediations/{id}` event rows and
  reproduces the API to within one second (`to_merge` p50 recomputes as 2213 against a reported
  2212; the difference is sub-second timestamp rounding).
- The durations are Sentinel's own observations, not GitHub's. A `MERGED` row lands two to four
  seconds after the pull request's `merged_at`, because it is written when the webhook is processed.
  The per-remediation headings below quote GitHub's `merged_at`; the traces and the durations use
  the event log, so the two differ by seconds and neither is wrong.

### Which figures not to trust

**`review_latency` is not a steady-state number.** p50 185 s and p90 542 s measure a reviewer who
was watching the dashboard and was told the moment each pull request opened. It is honest as a
record of what happened and misleading as an expectation: it is the latency of a person waiting for
one specific event, not of a review queue. Nothing in this run measures how long these pull requests
would have waited in a normal week.

**#1 and #2's clocks start late, and it moves a published percentile.** `labeled_at` is Sentinel's
receipt clock. GitHub applied `devin:autofix` to issue #1 at 14:07:19 and to issue #2 at 14:07:20;
both deliveries returned 500 (incident 1) and the remediations only exist from the redeliveries, so
their recorded `labeled_at` is 14:09:43 and 14:11:08. **2 m 24 s and 3 m 48 s of real elapsed time
are missing from those two remediations' `to_pr` and `to_merge`.** Recomputed from GitHub's label
events, the nearest-rank `to_merge` p50 rises from 2212 s to roughly 2311 s and the p50 remediation
moves from #8 to #2. The eight other remediations are unaffected — their first delivery succeeded.

**`hours_saved` is an assumption, not a measurement.** It is a stated baseline of engineer-hours per
issue class multiplied by the merges in that class. The API says so in its own `assumption` field.
It is not derived from anything observed here.

**The merges are into a fork.** The defects are real defects in the Superset codebase and the fixes
are real fixes, but the merge authority is ours ([B10](./blockers.md#b10)). "Merged" does not mean
"accepted upstream".

**The CI signal is deliberately narrow** — changed files and a scoped test selection
([B2](./blockers.md#b2)). It is a real signal, not full validation. It also has a second edge that
this run discovered: a narrow selection changes *which* failures you see. #18 existed only because
the scoped selection excluded the test that was accidentally holding the rest of the suite together,
and #7's CI failures appeared only because touching `pytest.ini` widened the scope to the whole unit
suite.

**`elapsed_seconds` on `/api/remediations` is not a duration to merge.** It is the age of the
remediation, `now − labeled_at`, for every state including terminal ones — as
`src/sentinel/api/analytics.py` documents. On a remediation that merged two days ago it reads in the
tens of thousands of seconds. Use the event log for durations.

**Seven of the nine merged fixes left their issue open.** Only
[PR 19](https://github.com/taxpon/superset/pull/19) (`Fixes #18`) and
[PR 20](https://github.com/taxpon/superset/pull/20) (`Fixes #7`) carried a GitHub
closing keyword in a form GitHub acts on. The other seven named their issue in prose or in the
template's *Has associated issue* checkbox — [PR 14](https://github.com/taxpon/superset/pull/14)
gets closest with "Fixes GitHub issue #1 in this fork", which is not the keyword form and does
nothing. So issues 1, 2, 3, 5, 6, 8 and 21 are still `OPEN`
although their fix is merged into `master`. Sentinel's own funnel is unaffected — it counts the
merge, not the issue state — but anyone reading the fork's issue list will misread it. This is a
gap in the playbook's pull-request template, not in the orchestrator.

## Per remediation

Each entry: the defect, what the agent produced, and anything it corrected about the brief it was
given. Every merged pull request in this run corrected or sharpened its issue in some way; the ones
that changed the answer rather than refining it are marked.

### #5 · `flaky-test` · [PR 12](https://github.com/taxpon/superset/pull/12) · merged 08-10 14:05:45

A skipped test in `Row.test.tsx` that could never have passed. React Testing Library is configured
with `testIdAttribute: 'data-test'` and `BackgroundStyleDropdown` never set one, so the query was
always `null`.

**Corrected the brief.** The issue named the missing test id as the probable cause and flagged
explicitly that whether it was the *only* cause had not been verified. It was not: the test file
also stubbed `WithPopoverMenu` with a component that rendered `children` and silently dropped
`menuItems`, so the dropdown was never mounted. The test id alone would not have made the test pass.
Both were fixed. A similar `test.skip` in `Column.test.tsx` was found and deliberately left alone,
because it is written against an API that no longer exists.

### #1 · `security` · [PR 14](https://github.com/taxpon/superset/pull/14) · merged 08-10 14:48:43

`SQLStatement.is_mutating()` detects mutation by sqlglot node type, but anything outside a dialect's
grammar parses to an opaque `exp.Command` that carries no type information. The compensating
keyword fallback was gated on Postgres. Which statements degrade to `exp.Command` is a property of
sqlglot's grammar coverage, not of the engine — `CALL some_proc()` degrades on every dialect — so on
any non-Postgres engine with `allow_dml=False` a SQL Lab user could mutate through a stored
procedure. The fallback was generalised to all dialects and `exp.Execute` added for T-SQL.

**Corrected the brief.** The issue warned that generalising the keyword set might over-block. It
does: `SET` and `RESET` could not be generalised, because on Hive and Presto a `SET` preamble is how
a read-only query carries its settings. Generalising them turned three existing `test_has_mutation`
cases red. They were scoped back to Postgres rather than the whole mechanism being abandoned — the
issue's warning was correct and the boundary was found empirically rather than assumed.

### #2 · `security-dep` · [PR 13](https://github.com/taxpon/superset/pull/13) · merged 08-10 14:45:49

paramiko was pinned below 4.0 and the advisory (GHSA-r374-rxx8-8654) records no fixed version. The
pin was deliberate in two independent places: paramiko 4 removed `DSSKey`, which `sshtunnel` 0.4.0
still references on the `open_tunnel` path, and `liccheck`'s GPL allow-list was keyed on the major
version, which a bump alone would trip.

The upgrade to 5.0.0 therefore required production code — a DSA-free key discovery and read path
monkey-patched over `SSHTunnelForwarder` — and new tests, not just a version bump. Because the
advisory names no fixed release, the agent established one empirically: 3.5.1 has `"ssh-rsa": SHA1`
in `RSAKey.HASHES`, 5.0.0 does not. It stated plainly that this proves the acceptance is gone from
the library, not that the vulnerability was demonstrated closed end to end.

Of the ten, this is one of two that read as a dependency bot's job. Neither was one: #4 is the
other, and it could not be done at all.

### #3 · `bug` · [PR 17](https://github.com/taxpon/superset/pull/17) · merged 08-11 23:50:05

A dataset's Hour Offset was treated as a display concern: filter bounds shifted by `−offset`,
fetched values by `+offset`. That is valid while the transform is a shift and stops being valid the
moment the value is truncated, because `DATE_TRUNC` does not distribute over a shift. Rows bucketed
on stored-frame boundaries while only the axis label moved, so the chart looked right and the data
was in the wrong bucket. The fix moved the offset into the engine spec so grain expressions shift,
truncate, and shift back — with dialect spellings for MySQL, MSSQL, SQLite, BigQuery and Kusto.

**Corrected the brief.** The issue placed `offset` on `TableColumn`. It is a column of
`BaseDatasource` — the dataset, not the column — so the implementation reads `self.table.offset`
and the test sets it on the `SqlaTable`. This matters beyond bookkeeping: the offset applies to
every temporal column of a dataset, which is why the fix belongs at the engine spec rather than the
reported call site.

Its fix cycle is described [below](#3--one-cycle-spent-proving-the-failure-was-not-its-own).

### #18 · `flaky-test` · [PR 19](https://github.com/taxpon/superset/pull/19) · merged 08-11 23:41:02

Five `test_fetch_data_*` tests in `test_bigquery.py` patched `g` but never arranged a request
context. `BigQueryEngineSpec.fetch_data` writes its memory-limit flags only inside
`if has_request_context():`, so the assignments never ran and the assertions read back auto-created
child mocks. The tests were replaced with a context manager that pushes a real request context and
asserts against the real `g`. No production file changed.

**Refuted the brief.** The issue led with the hypothesis that the environment had changed — the same
source passing on 08-10 and failing on 08-11 implied a dependency resolving differently or a CI
cache — and named establishing which as the first thing to do. Nothing about the environment
changed. pytest collects files directly under `tests/unit_tests/` before descending into
subdirectories, so a whole-tree run executes `sql_lab_test.py`, which leaks a request context, ahead
of `db_engine_specs/`. A scoped run that selects only `db_engine_specs` has no leak. Same source,
two results, decided by collection scope.

It also disposed of the issue's secondary theory: `AsyncMock` was a symptom, not the cause.
`werkzeug.local.LocalProxy` defines `__await__`, so `mock.patch` of any `LocalProxy` yields an
`AsyncMock`; a `MagicMock` would have failed identically.

### #4 · `frontend-dep` · **BLOCKED** at 08-11 23:59:54

The only non-merge. See [#4 in full](#4--the-one-that-could-not-be-done) below.

### #7 · `deprecation` · [PR 20](https://github.com/taxpon/superset/pull/20) · merged 08-12 02:29:02

`Query.get()` has been deprecated since SQLAlchemy 1.4, but it emits `LegacyAPIWarning`, which only
surfaces under `SQLALCHEMY_WARN_20=1`, and `pytest.ini` promoted only eight named
`RemovedIn20Warning` patterns to errors — none of which matches it. Nothing in the build ever
objected, so new occurrences kept being written after the 1.4 upgrade. Eight live call sites were
converted to `Session.get()` and `pytest.ini` was made to fail on the warning, which is the part
that stops it recurring.

**Corrected the brief.** The issue listed seven call sites. There is an eighth, in the same function
as one of the seven, which the new gate would have failed; it needs per-query execution options to
preserve a soft-delete bypass. The broad `session.query(` idiom — 588 occurrences across 217 files —
and the frozen migration directory were deliberately left out.

Its three fix cycles are described [below](#7--three-cycles-and-the-cap).

### #6 · `perf` · [PR 22](https://github.com/taxpon/superset/pull/22) · merged 08-12 02:41:18

`TagDAO.create_custom_tagged_objects` resolved tags one name at a time, with a second query per tag
against `tagged_object` — reached from every dashboard, chart, dataset and saved-query tag update.
It predates the bulk resolver eleven lines below it and was never converted. Replaced with one
`Tag.name.in_(...)` query and one association query.

**Corrected the brief.** The issue derived the cost as `2N + 1` by reading the code. Measured with a
`before_cursor_execute` listener, the actual shape is `2N` with no constant term: N=2 gives 4
statements before and 2 after, N=10 gives 20 before and 2 after. The regression test asserts the two
measured counts are equal rather than pinning a specific number, so an unrelated query added later
does not break it.

### #8 · `typing` · [PR 23](https://github.com/taxpon/superset/pull/23) · merged 08-12 03:07:13

`pyproject.toml` disabled `warn_unused_ignores` for eight modules on the stated grounds that their
`# type: ignore[attr-defined]` comments are needed in CI where `superset-core` types are not visible.

**Refuted the brief.** The rationale is inverted. `SqlaTable` inherits `CoreDataset` from
`superset_core`, and the mypy overrides set `follow_imports = "skip"` for that package. A class with
an `Any` base has `Any` attributes, so `SqlaTable.id` is `Any` and `SqlaTable.id.in_(...)` can never
raise `attr-defined` — in CI or locally with `superset-core` installed. The ignores were dead in
every configuration. The issue expected the fix to be a proper annotation or a narrowing cast for a
real mypy blind spot; there is no blind spot to fill. The ignores were removed, the two modules
dropped from the override, and two behavioural tests added that compile the `IN` expression instead
of reading it off a mock. The deeper consequence — that every `SqlaTable` class attribute is
untyped to mypy — is real, and was named as belonging to a different change.

One commit, one CI run, no fix cycles, 2213 s from label to merge.

### #21 · `flaky-test` · [PR 24](https://github.com/taxpon/superset/pull/24) · merged 08-12 03:28:56

The request-context leak that PR 19 diagnosed and deliberately left alone.
`test_get_sql_results_oauth2` pushed a test request context and never popped it, so
`has_request_context()` was true for every test collected afterwards. Wrapped in a `with` block —
chosen over a `push()`/`pop()` pair so that it is popped on a failing assertion as well as on
success — plus a regression test asserting the context does not survive.

It resolved both of the issue's open questions. Running the whole unit suite with the context
correctly scoped (12428 passed, 4 skipped, 2 xfailed) established that nothing else depended on the
leak; the issue noted this had never been done. And it **declined the issue's own suggestion** of a
general autouse guard against leaked contexts, with evidence: prototyped as a throwaway plugin, the
guard flags 241 tests whose contexts are popped by fixture finalisers running later in the same
teardown phase. It said so rather than committing something that fails on 241 correctly-scoped
tests.

## #4 — the one that could not be done

Issue #4 asked for a transitive `image-size` denial-of-service advisory to be cleared out of the
deck.gl dependency family by upgrading. The session reached that conclusion and stopped **3 minutes
25 seconds** after it started running:

```
23:56:23  —                → QUEUED           issue_labelled
23:56:26  QUEUED           → SESSION_CREATED
23:56:29  SESSION_CREATED  → RUNNING
23:59:54  RUNNING          → BLOCKED          reason=session_waiting_for_user
```

`needs-human` was applied, a comment with the session link was posted on the issue, and Sentinel did
not retry. The issue remains open and labelled.

**This is not a failure of the pipeline. There is nothing to upgrade to.**

- Both advisories record `introduced: 0, last_affected: 2.0.2`, and 2.0.2 *is* the latest published
  `image-size`, released 2025-04-02. The upstream fix exists as an unreleased pull request. Every
  published version is affected.
- Going forward through the parents does not help. Every `@loaders.gl/textures` release through
  `5.0.0-alpha.1` still depends on `texture-compressor@^1.0.2`, whose final release pins
  `image-size ^0.7.4`; and the newest `deck.gl` — 9.3.10, there is no major 10 — still pulls
  `@loaders.gl/textures ~4.4.0`.
- `npm audit`'s `isSemVerMajor` "fix" is a **two-major downgrade** to `@deck.gl/geo-layers@8.9.36`
  on luma.gl v8, which would break the 30 deck.gl-9 import sites the issue itself had counted.
  `npm audit fix --force` would have removed the advisory by breaking the application.

All three version claims were checked independently against the npm registry before the escalation
was accepted: `image-size` latest 2.0.2 (2025-04-02), `deck.gl` latest 9.3.10,
`texture-compressor` latest 1.0.2 depending on `image-size ^0.7.4`. The agent also established
something nobody had asked for: `texture-compressor` is spawned only as an `npx` CLI from
loaders.gl's Node-side encode path, so `image-size` never enters the browser bundle. Unfixed, and
not reachable the way this application uses it.

**What would unblock it:** an `image-size` release carrying the parser-loop fixes, *and* a
`texture-compressor` or loaders.gl release that accepts it. An `overrides` entry would then be
enough — no source change, none of the 30 import sites touched.

The issue had named this exact unknown under *What could not be verified* — which deck.gl major
resolves `image-size` — and its definition of done said to report `outcome: blocked` with what was
found rather than open a pull request for something else. That is what happened.

## The two fix-loop runs

Two remediations used the review-fix loop. They are different in kind, and the difference is the
point.

### #3 — one cycle, spent proving the failure was not its own

```
22:40:54  RUNNING    → PR_OPENED    pull request 17 opens
22:41:35  PR_OPENED  → PR_OPENED    devin_call  session_question_after_pull_request  cycle=0
22:43:03  PR_OPENED  → CI_FAILED    head_sha 549bd574…
22:43:06  CI_FAILED  → RUNNING      session resumed, cycle=1        ← 3 seconds
22:44:40  RUNNING    → RUNNING      devin_call  cycle=1
23:47:02  RUNNING    → CI_PASSED    head_sha be9da4bc…
23:47:02  CI_PASSED  → IN_REVIEW
23:50:07  IN_REVIEW  → MERGED
```

Handed a red build, the agent did not try to fix it. It reproduced the five failing
`test_bigquery.py` tests on unmodified `origin/master` in a clean worktree, posted the evidence, and
declined to fold an unrelated fix into the pull request — while noting its own tests were green.
They gated this pull request only because it touched `superset/db_engine_specs/*.py`.

That was correct, and the unblocking came from the pipeline rather than from the loop. The failures
were filed as issue #18 at 23:13, labelled at 23:17:47, and merged as PR 19 at 23:41:02. #3 then
merged `master` into its branch — with, as it said, no changes of its own in the merge — and CI went
green six minutes later at 23:47:02.

So the cycle count of 1 overstates what the loop did. The loop's contribution was a diagnosis, not a
repair: the repair was a second remediation. #3 sat blocked for roughly an hour by a defect that had
nothing to do with it, and that hour is visible in its `to_merge` of 5281 s.

The chain did not stop there. PR 19 fixed the five tests and deliberately left the leak that caused
them; the leak became issue #21, which the pipeline then fixed as PR 24. Two defects — #18 and #21 —
each surfaced by an agent declining to fix something outside its slice. #3 itself came from human
triage, and is in [`remediation-candidates.md`](./remediation-candidates.md).

### #7 — three cycles, and the cap

```
00:43:26  RUNNING    → PR_OPENED    pull request 20 opens
00:44:27  PR_OPENED  → PR_OPENED    devin_call  cycle=0
01:00:15  PR_OPENED  → CI_FAILED    head_sha 2a895302…
01:00:18  CI_FAILED  → RUNNING      cycle=1
01:18:58  RUNNING    → CI_FAILED    head_sha 07130563…
01:19:02  CI_FAILED  → RUNNING      cycle=2
01:20:26  RUNNING    → RUNNING      devin_call  cycle=2
02:08:54  RUNNING    → CI_FAILED    head_sha 07130563…   ← the same SHA, failing a second time
02:08:58  CI_FAILED  → RUNNING      cycle=3
02:23:57  RUNNING    → CI_PASSED    head_sha 376c34d6…
02:23:57  CI_PASSED  → IN_REVIEW
02:26:11  IN_REVIEW  → IN_REVIEW    devin_call  cycle=3
02:29:04  IN_REVIEW  → MERGED
02:29:05  MERGED     → MERGED       cancel  reason=issue_closed  absorbed=true
```

The last row is the issue closing after the merge, absorbed rather than moving anything — the same
shape appears on #18.

The failing tests were two parameters of `test_sub_day_last_normalizes` in
`tests/unit_tests/mcp_service/common/test_time_range_validation.py` — a test that has nothing to do
with the deprecation, and which only ran at all because editing `pytest.ini` widens CI's scope to
the whole unit suite.

The agent diagnosed it as a pre-existing, time-of-day-dependent flake on `master`: the test asserts
that `get_since_until` *raises* for sub-day `Last …` values, and that premise only holds once
`now − 1 hour` has cleared today's midnight. Between 00:00 and 01:00 it does not, nothing raises,
and the test fails. It said both CI runs had started inside that window; only the first had, and
[the discrepancy is unresolved](#what-is-still-not-known). It offered to fix the flake and, on scope
grounds, we declined and re-ran CI instead.

The re-run failed the same two tests at 02:08:54, on the identical head SHA — the two `CI_FAILED`
rows against `07130563` are attempt 1 and attempt 2 of one workflow run, the same commit failing
twice. That failure resumed the session at cycle 3, the cap. On that cycle the agent made the change
we had declined: it pinned the clock with `freeze_time("2024-01-02 12:00:00")` and stated the
premise in a comment. CI went green.

Unlike #3, this remediation converged on its own last cycle. It is also the run that cost the most
time — `to_pr` p90, `to_merge` p90 and the whole 8992 s label-to-merge are all #7 — and the reason
is recorded under [decisions](#decisions-that-cost-something) and
[what is still not known](#what-is-still-not-known).

## Incidents

### 1 — the database ran out of memory, and two webhook deliveries were lost

2026-08-10, between the first three remediations and the rest. `sentinel-alpha-db` was running on
`shared-cpu-1x:256MB`. Its VM check went critical on memory and IO:

```
[✗] memory: system spent 1.4s of the last 10 seconds waiting on memory
[✗] io:     system spent 1.5s of the last 10 seconds waiting on io
```

Memory pressure caused swap, swap caused an IO stall, the proxy in front of Postgres timed out its
layer-7 check at 5 s and reported no server available, and in-flight connections were closed with
`asyncpg.ConnectionDoesNotExistError`. The poller exited non-zero and its machine rebooted. It
flapped: down 14:08:41, up, down 14:09:35, up 14:09:56.

The delivery log is the authoritative account, and it does not line up with the poller's window —
the database was already failing requests 79 seconds before the poller's first observed exit:

| Delivery | Result | |
|---|---|---|
| 14:07:22.160 `issues.labeled` | **500** | issue #1 — labelled by GitHub at 14:07:19 |
| 14:07:22.171 `issues.labeled` | **500** | issue #2 — labelled at 14:07:20 |
| 14:09:45.550 redelivery | 202 | |
| 14:09:51.333 redelivery | **500** | landed in the second flap |
| 14:11:08.860 redelivery | 202 | |

GitHub does not retry a 500. Without the redeliveries, labelling those two issues would simply have
had no effect and nothing in Sentinel would have noticed. They were redelivered by hand:

```bash
gh api -X POST repos/taxpon/superset/hooks/663732734/deliveries/<id>/attempts
```

The cost is recorded above in the figures: #1 and #2's clocks start at the redelivery, so their
`to_pr` and `to_merge` are short by the 2 m 24 s and 3 m 48 s the failed deliveries consumed.

**Done about it:** the machine was scaled to 1 GB, which cleared the check (`memory: 0s of the last
60s`), and the next five deliveries — five labels within 2.3 s of each other — all returned 202. The
webhook has reported `last_response: 202` since.

**This is not [B9](./blockers.md#b9), and the register has not caught up.** B9 is about a rotating
tunnel URL, and its own closing criterion — an app exists and `gh api …/hooks` shows a `fly.dev` URL
delivering successfully — is satisfied and has been since the deployment. What this incident exposes
is the consequence B9 gestures at without naming: **a delivery that is lost is lost, and nothing in
Sentinel detects it.** That gap is real, unfixed, and belongs in the register as its own entry.
`docs/blockers.md` is not this task's to edit.

### 2 — a read timeout that was not a failure created nine sessions for three issues

2026-08-10, immediately after. Five issues were labelled within 2.3 s. Devin's API became overloaded
— its own interface showed a server-load message — and `POST /v3/organizations/{org_id}/sessions`
stopped answering inside the client's 30 s timeout.

```
14:51:26  devin.request.retry    POST …/sessions  attempt=2
14:51:57  devin.request.failed   POST …/sessions  attempts=3
14:51:57  worker.job.failed      create_session   DevinTransportError: ReadTimeout
```

The client retries transport errors three times, and **every retry created another session**: the
request had arrived, Devin had created the session, and only the response was lost. Listing sessions
afterwards showed three each for issues #3, #4 and #6 — nine in total, all `new`, all within about
ninety seconds, all carrying Sentinel's own `[sentinel] #N` title. The first three had been labelled
one at a time and never timed out, so #1 and #2 have one session each; #5's extra sessions are the
earlier attempts described [above](#before-this-run-and-why-it-is-a-re-run), not duplicates.

**Done about it:** all nine were archived by hand via
`POST /v3/organizations/{org_id}/sessions/{id}/archive` — archived rather than deleted, so the trail
survives. That endpoint was not in [05](./05-devin-integration.md) at the time and was found by
reading Devin's own page for it; it is documented there now. Archiving does not stop work already in
flight: #6's archived session went on to
open [PR 15](https://github.com/taxpon/superset/pull/15) and #3's opened
[PR 16](https://github.com/taxpon/superset/pull/16); both were closed by hand.

The root cause is client-side and independent of the burst — one timeout produces up to three
sessions whether one issue is labelled or five — so narrowing only the client's retry would have
moved the duplication to the worker's job-level retry a minute later. The fix is adopt-or-create
keyed on the remediation: a matching session is looked for before one is created, and adopted if
found ([ADR](./adr/2026-08-11-a-session-is-adopted-before-it-is-created.md),
[05](./05-devin-integration.md#adopt-or-create)). It shipped as PR #110 on this repository before
the remaining remediations were run.

The remediations for #3, #4, #6, #7 and #8 were deleted at that point and re-run from scratch under
the fix; the three merged ones and their measurements were kept, as were the `webhook_delivery`
rows, which record what GitHub actually sent.

### 3 — adopting an archived session inherits its pull request

Found afterwards, and only because incident 2 left archived sessions lying around with open pull
requests.

When #3 was re-run, adopt-or-create matched the archived session from incident 2 and adopted it. The
session already had [PR 16](https://github.com/taxpon/superset/pull/16) open from the day before, so
the poller linked it immediately and the remediation read as a success **21 seconds** after being
labelled — against a pull request that predated it. The documented safety net is that the poller
observes a stopped session and escalates; that does not hold while the archived session already has
a pull request open, because the pull request moves the remediation on before the session's status
is consulted.

**Done about it:** PR 16 was closed by hand at 22:12:57, at which point the remediation escalated
correctly — the abandoned-pull-request rule
([ADR](./adr/2026-08-09-an-abandoned-pull-request-still-escalates.md)) did what it should. Issue #3
was then re-labelled at 22:22:06 and got a fresh session and
[PR 17](https://github.com/taxpon/superset/pull/17).

The gap itself is **not fixed**. Adopting an archived session in preference to creating a duplicate
is deliberate — archiving is how a runaway is stopped by hand, and answering that with a new session
would repeat the incident it stopped ([05](./05-devin-integration.md#adopt-or-create)) — but nothing
distinguishes "adopted a session that is mid-flight" from "adopted a session that finished
yesterday", and a pull request opened before the remediation started is treated as this
remediation's.

## Decisions that cost something

Two, both ours, both stated once.

**We re-ran CI on #7 instead of accepting the agent's offer to fix the flake.** The agent had
diagnosed `test_sub_day_last_normalizes` as a pre-existing time-of-day flake, said the failure was
not its own, and offered to fix it. We declined on scope grounds — one pull request does one thing —
and re-ran the checks. The re-run failed identically on the same head SHA, which resumed the session
and consumed cycle 3, the cap. The agent then made the change we had declined. The cost is
concrete: a fix cycle, roughly fifty minutes of wall clock, and a remediation that finished at its
limit with no cycles in reserve had anything else gone wrong. Holding the scope line was the right
instinct and the wrong call on a flake that was blocking the gate.

**We serialised the run after incident 2, and that changed the shape of the timings.** The first
three were labelled one issue at a time; the next batch was labelled five at once and produced the
duplicate sessions; everything after the fix was labelled in small groups again. Serialising lowers
the chance of a timeout but does not change what happens when one occurs, so it was never the fix —
the idempotent create was. What it did change is the measurements: the durations in this run are of
remediations that mostly did not compete with each other, so nothing here says what concurrency does
to time-to-pull-request. The concurrency cap has not been exercised at its limit.

## What is still not known

**Why pinning the clock fixed `test_sub_day_last_normalizes`.** The diagnosis is that the test's
premise fails between 00:00 and 01:00, when `now − 1 hour` has not yet cleared today's midnight. Set
against the workflow runs on PR 20, it accounts for one failure out of three:

| Workflow run | Head SHA | Started (UTC) | Ended | Result |
|---|---|---|---|---|
| 31551187784 | `2a895302` | 00:43:21 | 01:00:12 | failure — **inside** the window |
| 31552205853 attempt 1 | `07130563` | 01:01:10 | 01:18:55 | failure — outside |
| 31552205853 attempt 2 | `07130563` | 01:53:48 | 02:08:51 | failure — outside |
| 31556006964 | `376c34d6` | 02:10:07 | 02:23:55 | success, clock pinned |

Only the first run began inside the hour the diagnosis names. The other two began after it and
failed identically on the same two parameters. The agent's own comment stated that both runs to that
point had started at 00:58 and 01:17 UTC; neither figure matches the workflow's start times, and
01:17 is outside the window in any case.

Two explanations were considered and **neither was established**: the runner's clock or timezone may
not be UTC, so the window in runner-local terms may not be the window in the timestamps above; or
the tests may have depended on something else that freezing the clock incidentally removed.

The supporting evidence is thin. The whole unit suite has completed green three times — PR 13 on
2026-08-10 at 14:29, PR 20's final run at 02:10, and PR 23 at 02:48 — but the last two ran against a
tree that already carried the pin. PR 13's run is the only one that was both unpinned and outside
the window, so the time-of-day account rests on a single observation.

The symptom is gone and the mechanism is not confirmed. "It went green" is not "we understood it".

**Why some Devin sessions end by asking a question and others do not.** The decision that such a
question must not escalate ([ADR](./adr/2026-08-10-an-offer-after-the-pull-request-is-not-a-stall.md))
was written on a single observation, and a count made while the run was in progress recorded that
only #5 had asked while #1 and #2 had not. **The event log contradicts that for #1**: remediation 2
carries a `session_question_after_pull_request` row at 2026-08-10 14:45:23, cycle 0, between its
pull request opening and CI going green.

Recounted from the event log, seven of the ten remediations recorded at least one such question —
#5, #1, #3 (twice, cycles 0 and 1), #18, #7 (three times, cycles 0, 2 and 3), #6 and #21. Only #2
and #8 did not, and #4 never opened a pull request. So the behaviour is common rather than
universal, which is a stronger result than the notes claimed but still not a rule. The decision is
unaffected either way — it says an offer must not escalate, not that an offer always arrives — but a
design that had assumed the offer was universal, a timeout for instance, would have broken on #2 and
#8.

What is not known is why some sessions ask and others do not. Nothing in the class, the playbook or
the size of the change separates the seven from the two.

## What this document supplies to the presentation

[`docs/presentation.md`](./presentation.md) names this file as the source for five of its
placeholders; two more take their primary value from the session's `structured_output` and from
`remediation.blocked_reason`, and are listed here because this run fixes what those fields contained.

| Placeholder | Value |
|---|---|
| `{{loop_issue_number}}` | 3 — one cycle, spent on a diagnosis. 7 is the alternative: three cycles, converged on the last |
| `{{loop_cycle_count}}` | 1 for #3; 3 for #7 |
| `{{loop_root_cause}}` | #3: a dataset's hour offset was applied after time-grain truncation, so rows bucketed on the wrong boundary while the axis read correctly |
| `{{classes_merged}}` | 7 — `security`, `security-dep`, `flaky-test`, `bug`, `deprecation`, `perf`, `typing` |
| `{{blocked_issue_number}}` | 4 |
| `{{blocked_reason}}` | `session_waiting_for_user` |
| `{{diagnosis_examples}}` | #3, #18, #8 and #1 — none is a version bump, and #18 and #8 refuted their issue's stated hypothesis outright |

`{{demo_issue_number}}` and `{{demo_issue_class}}` are not fixed here. They name the remediation
triggered live at record time, which this run does not determine.
