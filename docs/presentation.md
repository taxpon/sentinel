# Presentation

> **Status:** Design · **Answers:** What is said in the five minutes, in what order, with what on screen, and which figures have to come from a real run?

Five minutes of speech is about 700 words. This document is that script, budgeted section by
section, plus the screen to be on for each beat and the material to hold back for questions.

Every figure that can only exist after a live run is written as a `{{…}}` placeholder. Nothing here
depends on those figures turning out well — each is read out and then interpreted, so the sentence
around it stands whatever the number is. **Do not rehearse with invented values.** Fill them from
the sources below, or say the figure is not in yet.

## Placeholders

The runs have not happened: Devin credentials are unobtained
([B8](./blockers.md#b8)), so no merge count, rate or duration exists yet. The script is written to
be filled, not rewritten.

There is no spend figure to fill in at all. Spend reporting was removed — Devin reports consumption
only in ACUs and this account is not billed in ACUs, so every figure it produced was zero
([ADR](./adr/2026-08-10-spend-reporting-is-removed-because-the-account-is-not-billed-in-acus.md)).
Section 5 speaks hours saved instead, and the honest answer to "what did it cost?" is the one below.

```bash
curl -s 'localhost:8000/api/analytics/summary?window=7d' | jq
grep -o '{{[a-z0-9_]*}}' docs/presentation.md | sort -u   # nothing left unfilled
```

### From `GET /api/analytics/summary?window=7d`

"Said in" is the section that speaks it; the rest are for questions, and are worth having on the
screen behind you rather than in your head.

| Placeholder | Field | Meaning | Said in |
|---|---|---|---|
| `{{labelled_count}}` | `funnel.labelled` | Issues labelled `devin:autofix` in the window | 5 |
| `{{pr_opened_count}}` | `funnel.pr_opened` | Of those, how many produced a pull request | 5 |
| `{{merged_count}}` | `funnel.merged` | Of those, how many merged | 5 |
| `{{success_rate}}` | `rates.success` | `merged / labelled`, as a percentage | 5 |
| `{{merge_rate}}` | `rates.merge` | `merged / pr_opened`, as a percentage | 5 |
| `{{autonomy_rate}}` | `rates.autonomy` | Merged with zero fix cycles and zero human messages into the session | 4 |
| `{{mttr_p50}}` | `durations_seconds.to_merge.p50` | Median label → merge | 5 |
| `{{mttr_p90}}` | `durations_seconds.to_merge.p90` | p90 label → merge | 5 |
| `{{review_latency_p50}}` | `durations_seconds.review_latency.p50` | Median CI-green → merge: the wait on a human | 5 |
| `{{hours_saved}}` | `impact.hours_saved` | `Σ baseline_hours[class] × merged_in_class` — a stated assumption | 5 |
| `{{failure_count}}` | `Σ failures[].count` | Terminal `BLOCKED` or `FAILED` | 6 |
| `{{failure_reasons}}` | `failures[].reason` | Read the two or three largest buckets aloud, by name | 6 |
| `{{time_to_pr_p50}}` | `durations_seconds.to_pr.p50` | Median label → pull request | Q&A |
| `{{mean_cycles}}` | `cycles.mean` | Mean fix cycles per remediation | Q&A |
| `{{zero_cycle_count}}` | `cycles.distribution["0"]` | Remediations that needed no second lap | Q&A |

Percentiles are seconds; convert to minutes or hours when speaking. They are nearest-rank
observations, so each one is a duration some remediation actually took and can be clicked through
to ([ADR](./adr/2026-08-08-percentiles-are-nearest-rank-observations.md)).

### From `docs/run-log.md` (T52) and the fork

`docs/run-log.md` is T52's record of the runs and does not exist until they have happened.

| Placeholder | Source | Meaning | Said in |
|---|---|---|---|
| `{{demo_issue_number}}` | run log | The issue triggered live before the first slide | setup, 3 |
| `{{loop_issue_number}}` | run log | A remediation that went round the review-fix loop and converged | 4 |
| `{{loop_cycle_count}}` | run log | How many laps it took | 4 |
| `{{loop_root_cause}}` | `structured_output.root_cause` on that session | One clause: why the defect existed | 4 |
| `{{classes_merged}}` | run log | Distinct issue classes among the merges | 5 |
| `{{open_blocker_count}}` | [`blockers.md`](./blockers.md) | Rows still `Open` on the day | 6 |
| `{{blocked_issue_number}}` | run log | A remediation that escalated | 6, if substantive |
| `{{blocked_reason}}` | `remediation.blocked_reason` | Why it stopped, in its own words | 6, if substantive |
| `{{demo_issue_class}}` | run log | Its class, from the eight in [01](./01-overview.md#issue-classes) | Q&A |
| `{{diagnosis_examples}}` | run log | Which merges were real diagnosis, not a version bump — [08](./08-testing.md) requires at least two | Q&A |

### Assumptions the speaker supplies

| Placeholder | Meaning | Said in |
|---|---|---|
| `{{engineer_hourly_cost}}` | Loaded engineer cost per hour, for the one line that converts hours to money. Name it as your assumption when you say it, or drop the line. | 5, optional |

### If a figure has no denominator

An empty funnel stage makes a rate undefined rather than zero, and the dashboard renders `—` with
the reason rather than printing a `0` that reads as total failure
([ADR](./adr/2026-08-08-blank-a-figure-whose-denominator-is-empty.md)). If a tile shows a dash on
the day, say what the dash means — "nothing merged in this window, so there is no median time to
merge" — and move on. Do not narrate a number the panel is refusing to claim.

## Budget

697 words: five minutes at 140 a minute. Each section's spoken text is the blockquoted lines under
it, and the counts below are those lines — so the budget is checkable rather than asserted:

```bash
awk '/^## [0-9]\./ {s=$0} s && /^> / {n[s]+=NF-1} END {for (k in n) print n[k], k}' docs/presentation.md
```

Overrunning costs the ending, and the ending is section 6.

| # | Section | Time | Words |
|---|---|---|---|
| 1 | The problem, and what it was pointed at | 0:00–0:38 | 89 |
| 2 | Why Devin | 0:38–1:22 | 102 |
| 3 | What was built | 1:22–2:07 | 105 |
| 4 | The review-fix loop | 2:07–3:02 | 128 |
| 5 | What it produced, and what it was worth | 3:02–3:57 | 129 |
| 6 | What did not work | 3:57–4:46 | 115 |
| 7 | Close | 4:46–4:59 | 29 |

**Before you start.** Run the preflight in [09](./09-operations.md#demo-runbook) — `/healthz`, and
the webhook's `last_response.status` — and trigger `{{demo_issue_number}}` *before* the first slide.
The dashboard then advances behind you during sections 2 and 3, and section 4 has something real to
point at. Waiting for a live label-to-session transition on stage spends forty seconds of a
three-hundred-second budget on a state change nobody doubts.

---

## 1. The problem, and what it was pointed at — 0:00–0:38

**On screen:** the fork's issue list, filtered to `devin:autofix`.

> Every mature codebase carries a backlog nobody schedules. Dependency CVEs, flaky tests,
> deprecations, N-plus-one queries. Each item is small, so it never wins against feature work; there
> are hundreds, so the aggregate risk is real. It gets paid down after an incident, or never.
>
> I pointed a system at that backlog, on a fork of Apache Superset. Eight defects, one per class,
> found by reading the source rather than a changelog. Each names a regression test that fails
> before the fix. That is what makes the numbers later checkable.

The eight and the evidence for each are in
[`remediation-candidates.md`](./remediation-candidates.md); the acceptance criteria are in
[08](./08-testing.md#remediation-acceptance-criteria).

## 2. Why Devin — 0:38–1:22

**On screen:** the loop diagram from [01](./01-overview.md) — labelled issue, session, PR, CI, review, merge.

> Why Devin rather than a code-completion model? Because the unit of delegation is a task, not a
> keystroke. Devin gets an issue and a repository, then investigates, runs the suite, opens a pull
> request, reads its own CI failures and fixes them. Nobody sits in the loop accepting suggestions.
>
> That changes the cost curve: throughput scales with concurrent sessions, not with headcount. And
> it moves the bottleneck. If implementation stops being the constraint, review becomes it. That is
> a claim you should refuse to take on trust, so review latency is measured on its own — and I will
> show you it.

This is the argument the whole talk rests on
([01](./01-overview.md#why-devin-specifically)). The four Devin capabilities it depends on —
autonomous execution in a real VM, resumable sessions, structured output as a contract, and
per-session ACU accounting — belong in questions, not here. Say them only if asked why this was not
built on a plain completion API; the fourth is what the per-class cap and the daily budget guard are
enforced against.

## 3. What was built — 1:22–2:07

**On screen:** the component diagram from [02](./02-architecture.md), then the live dashboard with
`{{demo_issue_number}}` already advancing through the live table.

> Four processes around one Postgres. The API terminates GitHub's signed webhooks and answers 202
> before any external call — GitHub times out a delivery at ten seconds, and creating a session is
> slower than that. Workers claim jobs with `SELECT FOR UPDATE SKIP LOCKED`. A poller reconciles
> session state, because Devin's API has no outbound webhook for it — not a shortcut, the only
> mechanism there is.
>
> Postgres is also the queue, so a transition and the job it enqueues commit in one transaction —
> or neither does.
>
> Every transition is an append-only event. Every number I am about to show comes off that log.

Every claim in there has a decision record behind it:
[202 before external calls](./adr/2026-08-07-respond-202-before-external-calls.md),
[Postgres as the queue](./adr/2026-08-07-postgres-as-job-queue.md),
[the poller drives the state machine](./adr/2026-08-07-poller-drives-state-machine.md),
[transitions are append-only events](./adr/2026-08-07-transitions-are-append-only-events.md).
Name the record, not the reasoning, unless asked.

## 4. The review-fix loop — 2:07–3:02

**On screen:** the timeline panel, with `{{loop_issue_number}}` selected — the `CI_FAILED → RUNNING`
row is the one to point at. Then the pull request itself, showing the fix commit and the
`root_cause` comment.

> A check suite fails. Sentinel resolves the head SHA back to the remediation, pulls the log excerpt
> from the earliest failing job, and sends it to the session that opened the pull request — resumed,
> still holding the context of its own change. It is told which cycle it is on and how many remain —
> not what to do about it.
>
> Issue {{loop_issue_number}} went round {{loop_cycle_count}} times and converged; the root cause it
> reported was {{loop_root_cause}}, in Devin's own structured output. {{autonomy_rate}} of the merges
> needed no lap at all.
>
> The loop is capped: a session that cannot converge escalates rather than burning budget. No merge
> is automatic — a person approves every one. The goal was to move the bottleneck to review, not to
> delete review.

Backing detail if pressed: the loop edges and the cap are in
[04](./04-state-machine.md#the-review-fix-loop); reusing the session rather than starting a fresh one
is [an ADR](./adr/2026-08-07-reuse-resumable-sessions.md), as is
[requiring human approval for every merge](./adr/2026-08-07-humans-approve-every-merge.md) and
[giving Devin the objective rather than the steps](./adr/2026-08-07-delegate-task-not-steps.md).
`MAX_FIX_CYCLES` defaults to 3 and is configuration, not a constant.

## 5. What it produced, and what it was worth — 3:02–3:57

**On screen:** the KPI row and the funnel panel. Hours saved is *not* on the dashboard — the panel
list in [07](./07-observability.md#dashboard) names an impact panel, but `dashboard/src/panels/`
has none and `impact.hours_saved` is rendered nowhere. Read it from step 4 of the runbook —
`curl -s 'localhost:8000/api/analytics/summary?window=7d' | jq '.impact'` — or from a slide.

> {{labelled_count}} issues labelled. {{pr_opened_count}} produced a pull request.
> {{merged_count}} merged, across {{classes_merged}} distinct classes.
>
> Two rates, kept separate. Success rate, {{success_rate}}, is end to end. Merge rate,
> {{merge_rate}}, is the quality of the work once there was work to judge. When they diverge they
> point at different problems.
>
> Median label to merge, {{mttr_p50}}; p90, {{mttr_p90}}. Of the median, {{review_latency_p50}} was
> spent waiting for me — the bottleneck, visible and measured.
>
> Against stated baselines — six engineer-hours for a security fix, two for a dependency upgrade,
> three for a flaky test — that is {{hours_saved}} hours. That is an assumption, labelled as one
> wherever it appears.
>
> No cost per fix. Devin meters in ACUs; this account is not billed in ACUs, so any figure would be
> invented rather than measured.

Two things to get right when saying this. The baselines are real constants —
`SECURITY_FIX` 6.0 hours, `DEP_UPGRADE` 2.0, `FLAKY_TEST` 3.0, `DEPRECATION` 3.0, in
`src/sentinel/devin/playbooks.py` — but they are assumptions about human effort, not observations,
and the API returns that caveat in `impact.assumption`. And say the spend paragraph as a decision,
not an apology: removing a metric that cannot be measured on the system being demonstrated is the
same discipline as section 6, and a listener who has been shown a wrong number before will recognise
it ([ADR](./adr/2026-08-10-spend-reporting-is-removed-because-the-account-is-not-billed-in-acus.md)).
If you want the money line, multiply `{{hours_saved}}` by `{{engineer_hourly_cost}}` out loud and
name the rate as your assumption — or leave it out, which is safer.

Every definition behind these figures is in [07](./07-observability.md#metric-definitions), and
every row of the live table links to both the Devin session and the pull request, so any of it can
be checked at source rather than believed.

## 6. What did not work — 3:57–4:46

**On screen:** the failure-breakdown panel, then [`blockers.md`](./blockers.md).

Do not apologise through this section and do not rush it. It is the evidence that the measurement in
section 5 is worth anything; a pipeline that reports only its successes is not observability.
Step 5 of the runbook exists for the same reason.

> Now the part that makes the rest credible.
>
> These pull requests are merged inside my fork, not into `apache/superset`. Upstream has its own
> review process and timeline that no demonstration controls. So "merged" here means merged into
> `taxpon/superset`. The defects are real defects in Superset's code — the merge authority is mine.
>
> The fork's CI is deliberately narrow: pre-commit on changed files and scoped unit tests. That is a
> real signal. It is not full-suite validation, and I will not present it as one.
>
> {{failure_count}} remediations ended without a merge: {{failure_reasons}}. They are still on the
> dashboard, still labelled `needs-human`, not retried and not tidied away. {{open_blocker_count}}
> items in the blocker register are still open.

`{{blocked_reason}}` on issue `{{blocked_issue_number}}` is worth naming aloud if the reason is a
substantive one — a limitation of the approach rather than an operational hiccup. The fork-merge
constraint is [B10](./blockers.md#b10) and is `Accepted`, not open; the narrowed CI is
[B2](./blockers.md#b2) ([ADR](./adr/2026-08-07-scoped-ci-on-the-fork.md)), and it is what forced
every candidate to name a unit-testable host
([ADR](./adr/2026-08-08-select-only-unit-testable-targets.md)). The provisional ACU caps
([B11](./blockers.md#b11)) are the item most likely to still be open on the day.

## 7. Close — 4:46–4:59

**On screen:** the live table, one row expanded to its session and pull request links.

> The interesting artifact is not the patches. It is that the throughput, the failures and the hours
> each land on a session and a diff you can open.

---

## If you are running long

Cut in this order. Never cut section 6.

1. The p90 in section 5 — read the median only.
2. The money line in section 5 — hours saved stands on its own.
3. The Postgres-as-queue sentence in section 3.
4. The last sentence of section 2 — the promise to show review latency. Section 5 shows it anyway.

## Held back for questions

| Question | Where the answer is |
|---|---|
| Why not a completion model with a harness around it? | The four Devin capabilities in [01](./01-overview.md#why-devin-specifically); resumable sessions and per-session ACU accounting are the two you would have to build |
| Why v3 and not the API everyone's examples use? | Tags, structured-output schemas, `max_acu_limit`, playbook binding and schedules exist only in v3 ([ADR](./adr/2026-08-07-devin-v3-only.md)) |
| How do you know Sentinel sent what it claims? | Session tags — `sentinel`, `repo:`, `issue:`, `class:`, `run:<delivery id>` — are visible in the Devin dashboard, and `run:` is the same id in the logs ([05](./05-devin-integration.md)) |
| What stops a runaway bill? | A per-class `max_acu_limit`, a daily ACU budget defaulting to 100, and a concurrency cap; the budget guard spends against the highest figure any source reports ([ADR](./adr/2026-08-08-the-budget-guard-spends-against-the-highest-figure-any-source-reports.md)) |
| What happens to a duplicate or replayed webhook? | Two unique constraints, not application logic ([ADR](./adr/2026-08-07-two-layer-deduplication.md)) |
| Could it merge something bad? | It cannot merge at all ([ADR](./adr/2026-08-07-humans-approve-every-merge.md)) |
| Is the hours-saved figure defensible? | No, as a measurement — it is `baseline_hours` per class times merges, stated as an assumption wherever it appears ([07](./07-observability.md#metric-definitions)) |
| Would this scale to a real repository? | The data model permits multiple repositories; the deployment does not exercise it, and that is out of scope rather than done ([01](./01-overview.md#scope)) |
