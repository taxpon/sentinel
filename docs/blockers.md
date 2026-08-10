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
| [B6](#b6) | Devin API | Playbooks are also organisation-scoped; reading them works | Programmatic playbook creation | Partly resolved — the premise was wrong |
| [B7](#b7) | Devin API | Org tag vocabulary may need pre-registration | Session creation | Open |
| [B8](#b8) | Credentials | Devin `org_id` and service-user token not yet obtained | **All live execution** | **Open — highest priority** |
| [B9](#b9) | Operations | Webhook needs a public URL; free tunnels rotate | Live demo reliability | Open — a Fly deployment retires it, and is not yet done |
| [B10](#b10) | Evaluation | PRs are merged within the fork, not upstream | Interpretation of "merged" | Accepted |
| [B11](#b11) | Cost | Superset's test suite is heavy; ACU per session unpredictable | Budget calibration | Open |
| [B12](#b12) | Delivery | This repository is private | Reviewer access | Open |
| [B13](#b13) | Delivery | Assignment brief must not leak into a public repository | Publication | Open |
| [B14](#b14) | Devin API | Tag registration writes an undocumented path, and the documented one is enterprise-scoped | Bootstrap step 2; any organisation shared with anyone else | Open |
| [B15](#b15) | Credentials | Devin needs its own GitHub access to the fork; nothing ever documented it as a prerequisite | Every session's push and pull request | Open |

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

### B6 — Playbooks are organisation-scoped too, and the premise here was wrong {#b6}

**Original claim.** That playbook create/update/delete live only under `/v3/enterprise/playbooks/*`,
so a token without enterprise scope could not create the four programmatically.

**That was wrong.** v3 exposes playbooks under **two** scopes. The organisation one carries the full
set — `GET`, `POST`, `PUT`, `DELETE` under `/v3/organizations/{org_id}/playbooks` — and asks for
`ManageAccountPlaybooks` rather than enterprise scope. Enumerated at
[`llms.txt`](https://docs.devin.ai/llms.txt) and specified on each endpoint's own reference page.

**Verified, 2026-08-10.** `make devin-playbooks` reads the organisation's playbooks against the real
API and returns them. The token has `ManageAccountPlaybooks`.

| | |
|---|---|
| `GET /v3/organizations/{org_id}/playbooks` | **Works.** Confirmed against the live API |
| `POST` to the same path | **Not tried.** Same permission on paper, so it probably works |

**Why `POST` is untried.** Probing a create means creating a playbook. `bootstrap_devin.py` declines
to do that deliberately — see `_probe_playbook_creation` — and the four already exist, so there was
nothing to gain by finding out at the cost of leaving a stray playbook in the organisation.

**What this changes.** The reproducibility this entry said was lost is probably available: the four
texts in [`playbooks/`](./playbooks/) could be pushed by a script rather than pasted by hand. Nobody
should act on that until a `POST` has actually been made to work — the claim above is exactly the
kind this entry got wrong the first time.

**Still true.** The four playbooks in use were created by hand in the UI, and their ids reach the
system through `DEVIN_PLAYBOOK_IDS`.

### B7 — Org tag vocabulary may require pre-registration {#b7}

**Evidence.** v3 exposes per-organisation allowed-tag management, implying tags may be validated
against a registered vocabulary. **The path this entry cited does not appear in the v3 reference** —
see [B14](#b14), which is now the harder half of this question.

**Impact.** If enforced, creating a session with an unregistered tag fails with `422` — and tags
carry the whole audit-trail argument ([05](./05-devin-integration.md)).

**Observed, 2026-08-10.** `uv run scripts/bootstrap_devin.py --dry-run` was run against the real
organisation. `GET /v3/enterprise/organizations/{org_id}/tags` — the documented read — answered
**`403`**. The reference requires `ManageEnterpriseSettings` for it and states *"the session tags
feature must be enabled for the enterprise"*; no tag settings are visible in the Devin web app for
this organisation either. So allowed-tag management looks like an enterprise feature this
organisation does not have.

**Inferred, not observed.** That the `PUT` of step 2 is refused for the same reason. The write has
not been attempted, and it goes to a different path ([B14](#b14)); a `403` on the read is evidence
about the write, not a measurement of it.

**Mitigation, and what it is now worth.** The vocabulary is registered at bootstrap before any
session is created — *when the organisation allows it*. Since 2026-08-10 a `403` or `404` on that
`PUT` is a reported degradation rather than a failed run: step 2 says in one line that the
vocabulary was **not** registered and that this is unresolved, and steps 3 and 4 go on to create the
knowledge notes and the nightly sweep. Nothing else depends on the registration having succeeded —
`create_session` sends tags, `handlers.py` adds `cycle:N` on a resume, and the session listing is
filtered by tag, none of which reads the registered vocabulary. What is unknown is whether Devin
*accepts* those tags without it.

**To verify.** The first real `POST /sessions` is the measurement, and it is now the only one this
question has. A session created with Sentinel's tags either succeeds — Devin does not validate
session tags against a registered vocabulary, or this organisation's vocabulary is not enforced —
or fails `422`, which answers B7 in the affirmative and makes registering the vocabulary a
prerequisite that this organisation cannot currently satisfy. Record the outcome here either way.

**One weaker signal arrives first.** Step 4 of the same bootstrap run sends
`POST /schedules` with `sentinel` and `class:scheduled-sweep` on it. So a run that reports step 2 as
*not registered* and still creates the sweep has had unregistered tags accepted **on a schedule** —
which is not the same object as a session, and the reference ties the feature to *session* tags. It
is evidence, not the answer.

### B14 — The tag path we write is undocumented, and the documented one is enterprise-scoped {#b14}

**Evidence.** Sentinel registers the vocabulary with `PUT /v3/organizations/{org_id}/tags`. That
path appears nowhere in the v3 reference. All five methods on an organisation's allowed tags are
documented at **`/v3/enterprise/organizations/{org_id}/tags`** — quoted from the OpenAPI block on
[Get Organization Allowed Tags](https://docs.devin.ai/api-reference/v3/tags/organizations-tags.md),
whose page slug says `organizations-tags` while the spec inside says `enterprise/organizations`.
They require `ManageEnterpriseSettings`, and *"the session tags feature must be enabled for the
enterprise"*.

Every other path Sentinel sends — sessions, knowledge notes, schedules, playbooks, consumption —
appears in the index verbatim. The tag vocabulary is the only one that does not.

**Two questions, and they compound.**

| | If | Then |
|---|---|---|
| Path | the organisation-scoped path is not served | bootstrap step 2 fails on the first real run. **This is the harmless outcome** — nothing is removed |
| Method | it *is* served, as an undocumented alias | `PUT` **replaces** the whole set, so the first run removes every tag the organisation allows that `devin/playbooks.py` does not list |

`POST` appends where `PUT` replaces. Whether Sentinel should own the organisation's vocabulary or
only add to it is a decision, not a bug: **`PUT` is kept deliberately**, because this organisation is
Sentinel's alone. On an organisation shared with anyone else, `POST` is the right method and this
entry is the reason to change it before running.

**Observed, 2026-08-10 — the destructive risk is much reduced.** The preview was run against the
real organisation and the documented read answered **`403`** ([B7](#b7)). If the write is refused
the same way, the second row of that table cannot happen: a `PUT` that never lands removes nothing.
That is the harmless outcome, and it is now the likely one.

Still inferred, not observed: that the write *is* refused. It goes to
`/v3/organizations/{org_id}/tags`, which the reference does not list, so a `403` on the
enterprise-prefixed read does not establish what the organisation-scoped write does. If the
undocumented alias is served without the enterprise permission, the replacement risk is exactly what
it was.

**Mitigation in place.** `uv run scripts/bootstrap_devin.py --dry-run` reads the *documented* path
and reports which tags would be kept, added and **removed**, naming the removals individually. Where
it cannot read — which is what happened — it says the write is likely to be refused too, and that
if it is accepted instead it may remove tags nothing here can name. It also states that the path it
read is not the path step 2 writes, so a removal list is never mistaken for a certainty. A refused
write is reported and does not stop the run.

**To resolve.** Run the preview. If the read is refused, check the organisation's allowed tags in
the Devin web app before running step 2 for real — and read step 2's own line afterwards, which
says whether the write was accepted or refused.

### B8 — Devin credentials not yet obtained {#b8}

**Impact.** Blocks all live execution: session creation, the review-fix loop, ACU accounting, the
scheduled sweep, the demo. Also gates [B5](#b5), [B6](#b6) and [B7](#b7).

**Needed.** `DEVIN_ORG_ID` (`org-…`) and a service-user token (`cog_…`) with at least
`ManageOrgSessions`; enterprise scope if the metrics panel is to use Devin's own figures.

**Meanwhile.** Everything except live execution is buildable — the orchestrator, its full test
suite (Devin is faked with `respx`), the analytics layer and the dashboard all develop without
credentials ([08](./08-testing.md)). This blocker gates the runs, not the build.

**Response shapes: one endpoint has been called, 2026-08-10.** Every endpoint has since been read
against its own page of the v3 OpenAPI reference (below), which is a weaker thing than a call: it
says what the API is documented to send, not what it sent. Exactly one call has been made.

| | |
|---|---|
| `GET /v3/organizations/{org_id}/sessions` | **Verified by a call.** `200`. Full field list recorded in [05](./05-devin-integration.md#response-shapes) |
| `POST /v3/organizations/{org_id}/sessions` | Not called. The create-session *request* body is still unverified against a real `201` |
| `POST …/sessions/{id}/messages` | Not called |
| `POST …/sessions/{id}/tags`, `PUT /v3/organizations/{org_id}/tags` | Not called — and see [B14](#b14) |
| `POST …/knowledge/notes` | Not called |
| `POST …/schedules` | Not called |
| `GET …/consumption/daily` | Not called |
| `GET /v3/enterprise/metrics/sessions` | Not called — see [B5](#b5) |
| `GET /v3/organizations/{org_id}/playbooks` | Verified separately, see [B6](#b6) |

**What the verified call actually established, and what it did not.** Field *names* and *types* were
recorded; field *values* were not. So the seven `status` values and the `waiting_for_user`
`status_detail` remain unconfirmed — `status_detail` is a plain string, and whether it ever carries
`waiting_for_user` has not been seen. `SessionStatus` still rejects an eighth status, which is the
intended behaviour: an unknown status means our reading of the API is wrong and should fail loudly.

**Two mismatches it found, one of them silent.** `pull_requests[].url` is really `pr_url`, which
failed the parse outright and would have stalled every remediation holding a pull request. Worse,
`pull_requests[].number` does not exist at all: it had parsed to `None` without complaint, and
`pr_number` is what resolves a check suite or a review to its remediation — so the review-fix loop
would have engaged for nothing, silently, while the traceback pointed elsewhere. Both are fixed; the
number is now derived from `pr_url`.

**What "unverified" now means, after the shape audit.** Every endpoint in
[the table](./05-devin-integration.md#endpoints-used) has been read back against its own page in
the v3 OpenAPI reference, in both directions. B8 no longer covers "we do not know the field names":
we do, for all twelve. Four had been guessed wrong, and each would have failed at a different
moment — `POST /knowledge/notes` rejected outright for a missing `trigger`; `POST /schedules`
succeeding and then failing to parse the `scheduled_session_id` it needed to record; the daily
consumption body silently unreadable, so the budget guard would have run on its fallback for ever;
and `GET /enterprise/metrics/sessions` raising a `422` for two window parameters nobody had noticed
were required. All four are fixed, and the fixtures now carry the reference's shapes rather than
the ones we hoped for.

What B8 still gates is everything the reference does not state, which is now a short list:

1. **Whether `session_id` is already `devin-` prefixed.** The per-session paths take a `devin_id`
   described as "the session ID prefixed with `devin-`". If `SessionResponse.session_id` is bare,
   every `GET`, message and tag call is aimed one prefix short. A single live create-then-get
   settles it, and it is the first thing to check.
2. **The unit of every epoch integer.** `date`, `created_at` and `updated_at` are typed `integer`
   with no unit. The consumption date is read as either seconds or milliseconds because both
   misreadings land in 1970 and would have shown up as a spend of zero rather than as an error.
3. **The default window of `GET /consumption/daily`.** `time_before` and `time_after` are optional
   and no default is documented, so what "daily" covers when neither is sent is unknown. The budget
   guard reads a single day out of it, so a window that excludes today feeds it a wrong number
   silently. Confirm before the guard is trusted to stop work.
4. **The billing day is Pacific, not UTC.** The reference is explicit — "midnight PST … corresponds
   to 08:00:00 UTC" — so a guard comparing a UTC `today` is reading a day up to eight hours out of
   step with Devin's own.
5. **The tag registration path**, which is [B14](#b14) and unchanged by this audit.

Two premises turned out to be wrong in the other direction, and both are decisions rather than
bugs: the organisation scope carries playbook writes as well as reads (so [B6](#b6)'s premise is
wrong), and `GET /knowledge/notes` and `GET /schedules` both exist — so `.env` is not the only
possible record of what `make bootstrap-devin` created
([ADR](./adr/2026-08-08-env-is-the-bootstrap-scripts-record.md)).

The one call and the reference agree, which is worth stating: `pr_url`, `pr_state`, the absent PR
number, the `items` envelope and the integer timestamps are all in `SessionResponse` and
`SessionPullRequest` as documented. The reference would have caught all three of the shapes that
call found, and did catch four more on endpoints nobody has called.

### B9 — Public URL required for webhook delivery {#b9}

**Impact.** GitHub must reach the API. Free `cloudflared`/`ngrok` URLs change on restart, and a
stale hook URL means silently failed deliveries.

**Mitigation.** Register the hook after starting the tunnel; verify with
`gh api repos/taxpon/superset/hooks --jq '.[].last_response.status'` as a demo preflight step.
Failed deliveries are redelivered by GitHub once the URL is corrected, and delivery-level
deduplication makes redelivery safe ([06](./06-event-pipeline.md)).

**A way out, not yet taken.** Deploying to Fly.io gives a permanent `https://<app>.fly.dev`
hostname, so the hook is registered once and the rotation disappears — the tunnel is not the only
way in. `fly.toml` and the runbook are in place
([09](./09-operations.md#deployment-flyio)). **Nothing has been deployed**, and `flyctl` was not
installed on the machine that runbook was written on, so none of its commands has been run. This
stays `Open` until an app exists and `gh api repos/taxpon/superset/hooks` shows a `fly.dev` URL
delivering successfully.

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

### B15 — Devin needs its own access to the fork, and nobody wrote that down {#b15}

**Known.** `GITHUB_TOKEN` is *Sentinel's* fine-grained PAT. [09](./09-operations.md#prerequisites)
scopes it precisely — issues, pull requests and contents on `taxpon/superset` — and Sentinel spends
it on labels, comments and pull-request reads. **Devin never uses it.** A session is created with
`repos: ["taxpon/superset"]` ([05](./05-devin-integration.md#creating-a-session)) and a prompt whose
definition of done is a branch, a push and a pull request; all three happen under Devin's *own*
GitHub connection, which is configured in Devin and by nothing in this repository. Until this entry,
neither the prerequisites, nor [05](./05-devin-integration.md), nor this register said so.

**Not established.** Whether that connection is already in place. It may well be — nobody has
looked. The defect recorded here is that the requirement was never written down, not that the access
is known to be missing.

**Impact if it is missing.** A session still starts and still investigates. It fails when it tries
to push, and the likely surface is `outcome: "blocked"`, which is a normal, expected result that
escalates to a human ([04](./04-state-machine.md)). So the pipeline appears to work while all eight
remediations escalate for a reason nothing here explains — expensive to diagnose during a demo, and
trivial to check beforehand.

**To resolve.** Look at Devin's connected repositories for `taxpon/superset` in the web app. Two v3
endpoints do report repository reach, and neither replaces looking:

| | |
|---|---|
| `GET /v3beta1/organizations/{org_id}/repositories` | *"List repositories available to an organization"*, service-user `Read` at the organization level. The prefix is **`v3beta1`**; every path Sentinel sends is `v3` ([05](./05-devin-integration.md)) |
| `GET /v3/enterprise/git-providers/connections/{connection_id}/repositories` | *"List repositories for a git connection"* — `ManageGitIntegrations` at the **enterprise** level, which this service user is not assumed to carry ([B5](#b5)) |

Nothing reports what a given *session* was able to reach. **Do not spend a real session finding
out**: a live check costs a session and its ACUs, and the first real remediation is that check
anyway ([B8](#b8)).
