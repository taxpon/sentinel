# What counts as CI green

Fixes the defect that took remediation 1 (issue #5, PR taxpon/superset#9) to `CI_PASSED` on a
`Hold Label Check` suite three seconds after the pull request opened, and then to `CI_FAILED` on
`Dependency Review` — spending a fix cycle on an environmental failure Devin cannot act on.

## The decision

A `check_suite` conclusion stops being the CI signal. The signal is the aggregate of
`GET /repos/{repo}/commits/{sha}/check-runs`, read in a worker, gated on the check run named
`devin-autofix-ci` — the `if: always()` conclusion job of the fork's own workflow, which already
reports whether every scoped signal passed.

Three outcomes:

| Verdict | When | Trigger applied |
|---|---|---|
| `FAILED` | the `devin-autofix-ci` check run failed | `CHECK_SUITE_FAILED` |
| `GREEN` | it succeeded, nothing else is failing, nothing is incomplete | `CHECK_SUITE_SUCCEEDED` |
| `PENDING` | anything else | `CHECK_SUITE_REQUESTED` |

**A failing check run outside our own workflow yields `PENDING`, not `FAILED`.** This diverges from
the rule the team lead approved and is flagged in the report; see the ADR's alternatives table.

## Steps

- [x] `src/sentinel/github/checks.py` — `CIVerdict` and the pure `verdict()` over `CheckRun`s
- [x] `src/sentinel/config.py` — `ci_required_check_name`, `ci_workflow_path`
- [x] `src/sentinel/github/events.py` — `check_suite.completed` maps to `EVALUATE_CI`, no trigger
- [x] `src/sentinel/github/client.py` — narrow `get_failing_job` to our workflow; `CheckRun` docstring
- [x] `src/sentinel/queue.py` — `JobKind.EVALUATE_CI`
- [x] `src/sentinel/api/webhooks.py` — enqueue the evaluation instead of transitioning
- [x] `src/sentinel/pipeline/handlers.py` — the `evaluate_ci` handler
- [x] Tests: verdict table, mapping, webhook enqueue, handler, client narrowing, multi-suite fixture
- [x] `docs/04-state-machine.md`, `docs/06-event-pipeline.md`, `docs/08-testing.md` criterion 2
- [x] `docs/blockers.md` B2, `docs/fork-ci/README.md` step 2 as a prerequisite, with PR #9 recorded
- [x] ADR + its row in `docs/02-architecture.md`
- [x] `tasks/lessons.md` — the single-suite test double
- [x] `make ci` — 1902 passed, ruff and mypy clean

## Second addition: a merge is recorded from any state

- [x] `webhooks.py` — `merged_at` stamped from `Trigger.PR_MERGED`, outside the `moved` branch
- [x] Funnel checked rather than assumed: `metrics.summary` already counts from `merged_at`, so the
      column was the bug and the query was not
- [x] Tests — `BLOCKED` and `FAILED` receive a merge; funnel, MTTR and throughput all count it
- [x] `docs/04` invariant 1, `docs/03` funnel definition, ADR + its `docs/02` row
- [x] Layer 1 verified done on the fork (1 active, 45 `disabled_manually`); B2, `docs/08`,
      fork-ci step 2 and the CI ADR updated from "outstanding" to "done"

## Review

**Shape of the change.** `check_suite.completed` no longer carries a trigger. It maps to
`Intent.EVALUATE_CI`, the ingress enqueues a fifth job kind, and `evaluate_ci` reads the pull
request's head and every check run on it before applying one of three triggers. The state machine
itself is untouched — only what decides which trigger to apply changed.

**Three things found while building that were not in the brief.**

1. `ci_green_at` is stamped by the ingress, so moving the transition to a worker would have left it
   permanently null and silently broken two metrics. Stamped in `_record_evaluation` instead.
2. `check_suite.completed` with `cancelled`/`neutral`/`skipped` used to be dropped as
   `unhandled_conclusion`. Under an aggregate that is wrong: the completion that finally settles a
   SHA can be a skipped suite, and dropping it strands the remediation. All conclusions now enqueue
   an evaluation, and `Reason.UNHANDLED_CONCLUSION` is deleted.
3. Two evaluations of one remediation could interleave and leave the state column disagreeing with
   the events. `load_remediation(..., for_update=True)` serialises them.

**Divergence from the rule first approved, since confirmed.** A failing check run *outside*
`devin-autofix-ci` yields **pending**, not **failed**. The rule as originally approved would report
`CI_FAILED` on every remediation while the fork's `Dependency Review` was live, resuming Devin
against an unfixable repository setting and exhausting `MAX_FIX_CYCLES` — the defect being fixed,
relocated to the worker — and it would break the `get_failing_job` narrowing in the same stroke.
The ADR's alternatives table carries both sides.

**Layer 1 is done**, by the owner, on 2026-08-10: 45 inherited workflows disabled, only
`devin-autofix-ci.yml` active. Layer 2 was written to be correct either way, so no code changed —
but the dependency between them stays documented in B2, `docs/08-testing.md` and fork-ci step 2,
because the definition is only *usable* because the population was shrunk first, and re-enabling one
workflow puts the stall back.
