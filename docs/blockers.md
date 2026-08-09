# Blockers, risks and open questions

> **Status:** Living · **Answers:** What is currently unresolved, what does it block, and what is needed to resolve it?

A working register, updated throughout implementation.

**Rules**

1. New blockers get appended with the next `B` number. **Numbers are never reused.**
2. Resolving an item changes its `Status` and adds a resolution note. **Rows are never deleted** —
   consistent with the append-only audit trail in [03](./03-data-model.md).
3. Anything still `Open` at delivery is stated openly in the write-up, not omitted.

| Status | Meaning |
|---|---|
| `Open` | Unresolved and still blocking or risking something |
| `Mitigated` | A workaround is in place; the underlying constraint remains |
| `Accepted` | A real constraint we have chosen to design around |
| `Resolved` | Gone, with a note on how |

*Last reviewed: 2026-08-07*

## Register

| ID | Area | Summary | Blocks | Status |
|---|---|---|---|---|
| [B1](#b1) | GitHub fork | Issues now enabled; label set, webhook and issues still absent | Everything — issues are the trigger | Partly resolved |
| [B2](#b2) | CI | Fork workflows unregistered; full Superset CI too slow for the loop | Review-fix loop, PR evidence | Open |
| [B3](#b3) | GitHub fork | Default branch is `master`, not `main` | PR base correctness | Mitigated |
| [B4](#b4) | Devin API | No outbound webhook for session status | Push-based state updates | Accepted |
| [B5](#b5) | Devin API | Session metrics endpoint is enterprise-scoped | Cost/merge-rate panel fidelity | Open |
| [B6](#b6) | Devin API | Playbook CRUD is enterprise-scoped | Programmatic playbook creation | Open |
| [B7](#b7) | Devin API | Org tag vocabulary may need pre-registration | Session creation | Open |
| [B8](#b8) | Credentials | Devin `org_id` and service-user token not yet obtained | **All live execution** | **Open — highest priority** |
| [B9](#b9) | Operations | Webhook needs a public URL; free tunnels rotate | Live demo reliability | Open |
| [B10](#b10) | Evaluation | PRs are merged within the fork, not upstream | Interpretation of "merged" | Accepted |
| [B11](#b11) | Cost | Superset's test suite is heavy; ACU per session unpredictable | Budget calibration | Open |
| [B12](#b12) | Delivery | This repository is private | Reviewer access | Open |
| [B13](#b13) | Delivery | Assignment brief must not leak into a public repository | Publication | Open |

---

## Details

### B1 — The fork is not yet set up to be triggered {#b1}

**Original evidence.** `gh api repos/taxpon/superset` returned `has_issues: false`. Forks do not
inherit the parent's issue tracker.

**Now.** Issues are enabled. The rest of the setup has not been done — verified 2026-08-09:

| Check | Command | Result |
|---|---|---|
| Issues enabled | `gh api repos/taxpon/superset --jq .has_issues` | `true` — resolved |
| Label set | `gh api repos/taxpon/superset/labels --paginate` | 9 labels, all GitHub's defaults. No `devin:autofix`, no `class:*`, no `needs-human` |
| Webhook | `gh api repos/taxpon/superset/hooks` | none |
| Remediation issues | `gh issue list -R taxpon/superset` | none open |

**Impact.** `issues.labeled` carrying `devin:autofix` is the primary trigger
([06](./06-event-pipeline.md)), and the class label selects the playbook — an issue without one is
`unclassified` and escalates instead of opening a session. So nothing runs until the labels exist,
and nothing is delivered until the webhook does.

**Resolution.** Three commands, none of which has been run, and each of which writes to a public
repository:

```bash
make bootstrap-github                                  # labels, and the webhook
uv run scripts/file_remediation_issues.py --apply      # the eight issues (dry run is the default)
```

plus pushing `docs/fork-ci/devin-autofix-ci.yml` to the fork — see [B2](#b2).

### B2 — Fork CI: unregistered workflows, and a suite too slow for the loop {#b2}

**Evidence.** Actions are enabled (`{"enabled": true, "allowed_actions": "all"}`) and 49 workflow
files exist under `.github/workflows/`, but the workflows API reports `total_count: 0` — they have
never run on this fork. Several (`superset-e2e`, `superset-playwright`, `superset-helm-*`) take
tens of minutes.

**Impact.** Two problems. Workflows must run once to register. And a CI signal measured in tens of
minutes is too slow to be the feedback channel for the review-fix loop, which needs to be
demonstrable in minutes.

**Mitigation.** Add a lightweight `devin-autofix-ci.yml` to the fork — `pre-commit` on changed
files, scoped `pytest`, scoped `npm test` — and open one throwaway PR to register workflows. Where
a remediation touches an area covered by a heavier workflow, run that workflow before merging.

**Honesty note.** This narrows the CI signal. Say so; do not present scoped CI as full-suite
validation. Specified in [08](./08-testing.md).

**Status, 2026-08-09.** The workflow is written and reviewed — [`fork-ci/devin-autofix-ci.yml`](./fork-ci/devin-autofix-ci.yml) —
but it is **not on the fork**: `gh api repos/taxpon/superset/contents/.github/workflows` does not
list it. Putting it there is a commit to a public repository, so it waits with the other three
writes in [B1](#b1).

### B3 — Default branch is `master` {#b3}

**Evidence.** `defaultBranchRef.name == "master"`.

**Impact.** Any hard-coded `main` silently produces PRs against a non-existent base.

**Mitigation.** `TARGET_BASE_BRANCH` config, defaulting to `master`, used for both the PR base and
the prompt template. No literal branch name appears in code.

### B4 — Devin has no outbound session webhook {#b4}

**Evidence.** The v3 API reference documents no callback or subscription for session status
changes. Devin's own webhook material covers *inbound* triggers (a ticketing system calling Devin),
not outbound notification.

**Impact.** Session progress can only be observed by polling
`GET /v3/organizations/{org}/sessions/{id}`. Dashboard freshness is bounded by
`POLL_INTERVAL_SECONDS`.

**Accepted.** The poller is a first-class component rather than a fallback
([02](./02-architecture.md)). It also makes the pipeline self-healing when a GitHub webhook is
missed. Revisit if outbound webhooks ship.

### B5 — Session metrics endpoint is enterprise-scoped {#b5}

**Evidence.** `GET /v3/enterprise/metrics/sessions` requires a service user with
`ViewAccountMetrics` **at the enterprise level**. Unknown whether our credentials will have it
(depends on [B8](#b8)).

**Impact.** Without it, `sessions_with_merged_prs_count` and `avg_acus_per_session` are unavailable
from Devin.

**Mitigation.** Compute the equivalents from Sentinel's own `remediation` table and mark them
`source: "derived"` in the API response ([07](./07-observability.md)), so a reader can always tell
which figures came from Devin.

**To resolve.** Check the service user's permissions once [B8](#b8) is done; `make bootstrap-devin`
probes and reports reachability.

### B6 — Playbook CRUD is enterprise-scoped {#b6}

**Evidence.** Playbook create/update/delete live under `/v3/enterprise/playbooks/*`.

**Impact.** If our token lacks enterprise scope, the four playbooks cannot be created
programmatically.

**Mitigation.** Create them in the Devin UI and supply ids through `DEVIN_PLAYBOOK_IDS`. Playbooks
are configuration, so this costs reproducibility, not capability.

**To resolve.** Same probe as [B5](#b5).

### B7 — Org tag vocabulary may require pre-registration {#b7}

**Evidence.** v3 exposes `PUT /v3/organizations/{org_id}/tags` and per-organisation allowed-tag
management, implying tags may be validated against a registered vocabulary.

**Impact.** If enforced, creating a session with an unregistered tag fails with `422` — and tags
carry the whole audit-trail argument ([05](./05-devin-integration.md)).

**Mitigation.** Register the vocabulary at bootstrap before any session is created.

**To verify.** After [B8](#b8): create a session with a deliberately unregistered tag and record
whether it is rejected. Update this entry with the answer.

### B8 — Devin credentials not yet obtained {#b8}

**Impact.** Blocks all live execution: session creation, the review-fix loop, ACU accounting, the
scheduled sweep, the demo. Also gates [B5](#b5), [B6](#b6) and [B7](#b7).

**Needed.** `DEVIN_ORG_ID` (`org-…`) and a service-user token (`cog_…`) with at least
`ManageOrgSessions`; enterprise scope if the metrics panel is to use Devin's own figures.

**Meanwhile.** Everything except live execution is buildable — the orchestrator, its full test
suite (Devin is faked with `respx`), the analytics layer and the dashboard all develop without
credentials ([08](./08-testing.md)). This blocker gates the runs, not the build.

### B9 — Public URL required for webhook delivery {#b9}

**Impact.** GitHub must reach the API. Free `cloudflared`/`ngrok` URLs change on restart, and a
stale hook URL means silently failed deliveries.

**Mitigation.** Register the hook after starting the tunnel; verify with
`gh api repos/taxpon/superset/hooks --jq '.[].last_response.status'` as a demo preflight step.
Failed deliveries are redelivered by GitHub once the URL is corrected, and delivery-level
deduplication makes redelivery safe ([06](./06-event-pipeline.md)).

### B10 — PRs are merged in the fork, not upstream {#b10}

**Impact.** "Merged" means merged into `taxpon/superset`. Upstream `apache/superset` has its own
review process and timelines that no demonstration can control.

**Accepted.** Stated explicitly in the README and the presentation so the claim is not overread.
Fixes are nonetheless real remediations of real defects in the Superset codebase, not synthetic
changes.

### B11 — ACU consumption per session is unpredictable {#b11}

**Impact.** Superset's integration tests are database-backed and slow, so a session can burn ACUs
in the test loop. `max_acu_limit` set too low truncates good work; too high removes the cost
ceiling.

**Mitigation.** Treat the initial caps in [05](./05-devin-integration.md) as provisional, calibrate
against the first one or two real runs, and record the measured values here.

### B12 — Repository is private {#b12}

**Impact.** Reviewers must be able to inspect the orchestrator; a private repository fails that
outright.

**Resolution.** Make public — but only after [B13](#b13).

### B13 — Assignment brief must not leak into a public repository {#b13}

**Evidence.** `.gitignore` currently lists `requirements/`, `.claude/` and `CLAUDE.local.md`. The
repository has no commits yet, so nothing has been committed so far.

**Impact.** Publishing the brief or the evaluation criteria would be inappropriate.

**Resolution.** Before flipping visibility, verify the **entire history**, not just the working
tree:

```bash
git log --all --name-only --pretty=format: | sort -u | grep -E 'requirements/|CLAUDE.local|\.env'
```

Expect no output. Also confirm no token or webhook secret appears in any commit.
