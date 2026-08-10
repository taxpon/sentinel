# Devin integration

> **Status:** Design · **Answers:** Which v3 endpoints and features does Sentinel use, and how are prompts, playbooks, tags and structured output constructed?

**Sentinel uses the v3 API exclusively.** No v1 or v2 endpoint is called anywhere in the codebase.

Base URL `https://api.devin.ai`, authenticated with a service-user token
(`Authorization: Bearer cog_…`).

## Endpoints used

Every row names the OpenAPI schemas the v3 reference gives for that endpoint, and the permission it
requires. **Nothing here has been observed live** ([B8](./blockers.md#b8)): the whole table is
*verified against the reference* and nothing more, and where the reference is silent the section
below says so in as many words.

| Method | Path | Used for | Request → response schema · permission |
|---|---|---|---|
| `POST` | `/v3/organizations/{org_id}/sessions` | Create a remediation session | `SessionCreateRequest` → `SessionResponse` · `ManageOrgSessions` |
| `GET` | `/v3/organizations/{org_id}/sessions/{devin_id}` | Poller reconciliation — status, ACUs, structured output, PRs | — → `SessionResponse` · `ViewOrgSessions` |
| `GET` | `/v3/organizations/{org_id}/sessions` | The adopt-or-create lookup before every creation; backfill and in-flight listing | `SessionsQueryParams` → `PaginatedResponse[SessionResponse]` · `ViewOrgSessions` |
| `POST` | `/v3/organizations/{org_id}/sessions/{devin_id}/messages` | Review-fix loop: feed CI logs and reviewer feedback | `SessionMessageCreateRequest` → `SessionResponse` · `ManageOrgSessions` |
| `POST` | `/v3/organizations/{org_id}/sessions/{devin_id}/tags` | Append lifecycle tags (`cycle:N`, `outcome:merged`) | `SessionTagsUpdateRequest` → `SessionTagsResponse` · permission not stated |
| `PUT` | `/v3/organizations/{org_id}/tags` | Register the organisation's allowed tag vocabulary at bootstrap | **undocumented path** — see below |
| `GET` | `/v3/enterprise/organizations/{org_id}/tags` | Read the vocabulary that registration would replace — `bootstrap_devin.py --dry-run` only | — → `TagsResponse` · `ManageEnterpriseSettings` |
| `POST` | `/v3/organizations/{org_id}/knowledge/notes` | Seed repository conventions once at bootstrap | `KnowledgeNoteCreateRequest` → `KnowledgeNoteResponse` · `ManageAccountKnowledge` |
| `GET` | `/v3/organizations/{org_id}/playbooks` | Read back the ids of the hand-made playbooks — `make devin-playbooks` | — → `PaginatedResponse[PlaybookResponse]` · `ManageAccountPlaybooks` |
| `GET` | `/v3/organizations/{org_id}/consumption/daily` | Daily ACU spend for the budget guard | — → `ConsumptionResponse` · `ViewOrgConsumption` |
| `GET` | `/v3/enterprise/metrics/sessions` | Merged-PR and ACU aggregates — *enterprise scope, optional* | — → `SessionMetricsResponse` · `ViewAccountMetrics` |

The last row requires enterprise scope and the `ViewAccountMetrics` permission. Sentinel treats it
as an enhancement, not a dependency — see [Degradation](#degradation) and
[B5](./blockers.md).

Three things the reference states that are easy to get wrong from the paths alone:

- **The path parameter is `devin_id`, not the session id.** Every per-session row above is
  documented as `{devin_id}`, described as "Devin session ID (prefix: `devin-`)", with a note that
  "the `devin_id` is the session ID prefixed with `devin-`". Whether `SessionResponse.session_id`
  already carries that prefix — in which case the two are the same string — the reference does not
  say. Sentinel passes `session_id` through unchanged. *Unobserved*, and the first thing a live
  `GET` settles.
- **The three listings return `PaginatedResponse[T]`**, whose array is `items` and which also
  carries `has_next_page` and `end_cursor`. The session listing pages on `after` (the cursor) and
  `first` (default 100, minimum 1, **maximum 200**). `list_sessions` reads one page and follows no
  cursor; the adopt-or-create lookup follows it, at `first=200` — see
  [Adopt or create](#adopt-or-create).
- **Both the enterprise metrics window parameters are required.** `time_before` and `time_after`
  are the only `required: true` query parameters in this table.

**The two tag rows are not a matched pair, and that is unresolved.** The v3 reference documents the
allowed-tag vocabulary at `/v3/enterprise/organizations/{org_id}/tags` — `GET` to read it, `PUT` to
*replace the full set*, `POST` to *append* to it, and two `DELETE`s — each requiring
`ManageEnterpriseSettings`. It documents nothing at `/v3/organizations/{org_id}/tags`, which is
where the row above registers. So two things are open, neither of them decided here:

1. **The path.** The registration may be writing somewhere the API does not serve, in which case
   step 2 of `make bootstrap-devin` fails on the first real run rather than doing anything wrong.
2. **The method.** If it does serve it, `PUT` replaces the whole set: any tag the organisation
   already allows and `devin/playbooks.py` does not list is removed, on the first run, silently.
   `POST` would append instead. Which of the two Sentinel should send depends on whether it owns
   the organisation's vocabulary or only adds to it, and that is the repository owner's call.

Until both are answered, `bootstrap_devin.py --dry-run` reads the documented path and reports what
the registration would add, keep and **remove** — and says so plainly when the read is refused,
because a `PUT` whose removals cannot be named is the thing worth knowing before the run. Neither
the path nor the method of the registration itself is changed by that preview
([B7](./blockers.md#b7), [B8](./blockers.md#b8)).

## Creating a session

`POST /v3/organizations/{org_id}/sessions`, body `SessionCreateRequest`. Field-by-field mapping
from Sentinel state, with what the reference says each field is. `prompt` is the schema's **only**
required field; every other row below is Sentinel choosing to send something.

| Devin field | Sentinel value | Reference |
|---|---|---|
| `prompt` | Rendered from the class prompt template (below) | `string`, required |
| `title` | `[sentinel] #<issue> <issue title>` | `string?` |
| `tags` | The tag set below | `string[]?` |
| `repos` | `["taxpon/superset"]` | `string[]?` |
| `playbook_id` | `PLAYBOOK_IDS[issue_class]`, falling back to `PLAYBOOK_IDS[playbook_name]` — four playbooks serve eight classes, so either key resolves ([ADR](./adr/2026-08-08-playbook-ids-keyed-by-class-or-name.md)) | `string?` |
| `knowledge_ids` | The bootstrap note ids | `string[]?` |
| `structured_output_schema` | The schema below | `object?` — JSON Schema draft 7, max 64 KB, no external `$ref` |
| `structured_output_required` | `true` | `boolean?`, documented as defaulting to true |
| `max_acu_limit` | `ACU_CAPS[issue_class]` | `integer?` |
| `resumable` | `true` — required for the review-fix loop | `boolean`, default `true` |

`repos` names the repository but does not grant access to it — Devin clones, pushes and opens the
pull request under its own GitHub connection, which is not `GITHUB_TOKEN` and is configured in Devin
rather than here ([B15](./blockers.md#b15)).

Sentinel sends no other field. The reference defines twelve more —
`attachment_urls`, `bypass_approval`, `child_playbook_id`, `create_as_user_id`, `devin_mode`,
`platform`, `secret_ids`, `session_links`, `session_secrets` among them — and there is **no
idempotency key of any kind**: no `Idempotency-Key` header, no request-body field, and nothing on
the endpoint's own reference page that behaves as one. The single query parameter, `devin_id`, is
the *parent* session of a session Devin spawns, not a client-supplied token. Everything below
follows from that.

### Adopt or create {#adopt-or-create}

**Creating a session is idempotent per remediation, and Sentinel does it, because the API cannot.**
`create_session` runs a lookup before it posts:

1. `GET /v3/organizations/{org_id}/sessions?tags=sentinel&tags=repo:<repo>&tags=issue:<n>&first=200`,
   following `end_cursor` while `has_next_page`.
2. Every returned session is re-checked against all three tags here, because the reference does not
   state whether the server ANDs the `tags` array, ORs it, or ignores a value. The filter is an
   optimisation; the check is the rule.
3. If one matches, it is **adopted** — returned as though it had just been created, and recorded on
   the remediation exactly as a fresh one would be. No `POST` is made.
4. If none matches, the session is created, and the `POST` is sent **exactly once**.

Those three tags are `session_identity` in `devin/playbooks.py`, and they are the first three of
the five a creation sends, so the lookup cannot search for a combination a creation does not write.
They identify the *remediation*, not the attempt: `remediation` is `UNIQUE (repo, issue_number)`, so
one repository and one issue number name one remediation for ever, and a remediation has at most one
session because the review-fix loop resumes rather than recreates. `run:<delivery_id>` is
deliberately not part of the key — it names one webhook delivery, and keying on it would miss a
session a *previous* job had created.

Where several sessions match — the incident below left three per issue — one is chosen
deterministically so that repeated attempts converge: a live session before an archived one, then
the earliest created, then the lowest id. Every match is a candidate, including an archived one:
archiving is how a runaway is stopped by hand, and answering that with another session would repeat
the fault.

**A lookup that cannot answer creates nothing.** "I could not find out" is not "there is none", and
creating on it is the defect itself. All three ways it can fail raise before the `POST`, but they do
not end the same way, and the difference is operational:

- **Timed out, or `5xx`/`429`, past the read retries** — retryable. The queue backs off and tries
  the job again, up to `MAX_JOB_ATTEMPTS`.
- **Refused — `403`, a token without `ViewOrgSessions`** — not retryable. The job is retired on the
  first occurrence and the remediation escalates, because the next attempt would be refused
  identically.
- **Still claiming another page after the page budget, or naming another page with no cursor to
  reach it** — not retryable, for the same reason: it is a property of the listing, not of the
  moment.

Two consequences worth stating. `ViewOrgSessions` is now a prerequisite for *creating* a session and
not only for polling one, so a service user carrying `ManageOrgSessions` alone fails visibly on its
first remediation instead of working. And a Devin outage stops sessions being created rather than
duplicating them.

The incident this answers, on **2026-08-11**: five issues were labelled within 2.3 s, Devin's API
became overloaded and stopped answering `POST …/sessions` inside the 30 s timeout. The requests had
arrived and the sessions existed; only the responses were lost. The client retried each transport
error three times, so issues #3, #4 and #6 ended with three sessions each — nine in all, inside
about ninety seconds, every one carrying Sentinel's own `[sentinel] #N …` title. They were archived
by hand. Narrowing the client's retry alone would not have been enough: the job queue retries the
job too, so the duplicate would have arrived a minute later instead of a second later. See
[the ADR](./adr/2026-08-11-a-session-is-adopted-before-it-is-created.md).

`SessionResponse` is the body of the create, the get, the listing's `items` and the message post.
Its required fields are `session_id`, `url`, `status`, `tags`, `org_id`, `created_at`,
`updated_at`, `acus_consumed` and `pull_requests`. Sentinel consumes `session_id`, `url`, `status`,
`tags`, `acus_consumed` and `pull_requests[]`; on `GET` additionally `structured_output` and
`status_detail`, both of which the reference marks "only populated on get/list endpoints" — so a
create response carrying neither is correct, not a truncated one. `created_at` and `updated_at` are
epoch **integers**; `created_at` and `is_archived` are read by the adopt-or-create lookup, to order
the candidates, and `updated_at` by nothing.

The observed shape of all of this is in [Response shapes](#response-shapes) below; the entries of
`pull_requests[]` in particular are `pr_url` and `pr_state`, and carry **no pull request number**.

`status_detail` distinguishes `working` from `waiting_for_user`, which is how a session waiting on a
human is detected. The reference enumerates fifteen values for it, so it is read as an open string
and only `waiting_for_user` is branched on — a suspension reason Sentinel does not know about must
not fail a poll.

**`waiting_for_user` is not by itself a stall.** Devin sets it whenever the session has put a
question to the user, and it ends a session by offering to do something further as readily as it
raises a blocker mid-task. What the detail means therefore depends on whether the session has
produced a pull request yet: before one, the session is stuck and the remediation escalates; after
one, the work asked for has been delivered, the question is about an extra, and the remediation
carries on through CI and review with the question recorded rather than escalated. The table is in
[04](./04-state-machine.md#what-waiting_for_user-means) and the reasoning in its
[ADR](./adr/2026-08-10-an-offer-after-the-pull-request-is-not-a-stall.md).

`status` values the poller maps onto [the state machine](./04-state-machine.md): `new`, `claimed`,
`running`, `exit`, `error`, `suspended`, `resuming`. That is the whole enum; there is no `blocked`
status, which is why `blocked` is an outcome of the structured report instead.

`pull_requests[]` is `SessionPullRequest`, whose two required fields are `pr_url` and `pr_state`.
There is **no PR number** anywhere in the response.

## Response shapes

`GET /v3/organizations/{org_id}/sessions` was called against the live API on **2026-08-10** and
answered `200`. It is the only response shape in this document that has been seen rather than
inferred, and it did not match what the code had been written against — see
[B8](./blockers.md#b8) for exactly what that verified and what it did not.

The page is `{ items: [...], end_cursor, has_next_page, total }`. One item:

| Field | Type | Note |
|---|---|---|
| `session_id` | string | |
| `url` | string | The Devin web app link, recorded as `devin_session_url` |
| `status` | string | The seven values above. Which value arrived was not recorded |
| `title` | string | |
| `tags` | array of string | |
| `playbook_id` | null | Arrived null on the observed session |
| `user_id` | string | Not read |
| `org_id` | string | Not read |
| `created_at` | **integer** | An epoch, not an ISO-8601 string. Orders the adoption candidates |
| `updated_at` | **integer** | As above |
| `is_archived` | boolean | A live session is adopted in preference to an archived one |
| `acus_consumed` | number | |
| `pull_requests[].pr_url` | string | **Not `url`.** The field the poller links from |
| `pull_requests[].pr_state` | string | New; not read — see below |
| `parent_session_id` | null | Not read |
| `child_session_ids` | array | Not read |
| `service_user_id` | null | Not read |
| `category` | string | Not read |
| `subcategory` | string | Not read |
| `origin` | string | Not read |
| `automation_id` | null | Not read |
| `structured_output` | null | Present and null before the session reports |
| `devin_mode` | string | Not read |
| `status_detail` | string | A plain string. Which value arrived was not recorded |

That body and the reference's `SessionResponse` agree field for field, with one addition: the live
listing carried `automation_id`, which the reference does not define. `Session` ignores unknown
fields, so it cost nothing — but it is the reason that tolerance is there.

Two consequences the rest of this document depends on:

**There is no pull request number, anywhere in the session body.** Sentinel's `remediation.pr_number`
is what resolves a `check_suite` or `pull_request_review` delivery to a remediation
([06](./06-event-pipeline.md)), so the number is parsed out of `pr_url` — the `/pull/{number}` path
segment — by `devin/schemas.py`. A URL it cannot read leaves `pr_number` null, which makes that
remediation unreachable from GitHub; the poller records the URL it could not read on the
remediation's own event rather than inventing a number.

**The listing paginates.** `list_sessions` still reads one page, so a backfill over an organisation
with more sessions than a page sees a prefix. `find_session` does follow `end_cursor` while
`has_next_page`, because a prefix is not an answer to "does this remediation already have a
session?" — see [Adopt or create](#adopt-or-create).

`pr_state` is likewise carried by the API and read by nothing — a merge observed there would reach
Sentinel a poll ahead of the `pull_request.closed` webhook, but the webhook is authoritative today
and nothing is built on it.

## Playbooks and ACU caps

One playbook per issue class, holding the standing instructions for that kind of work (how to
reproduce, what evidence to gather, what the PR must contain). The per-issue prompt then carries
only what is specific to the issue.

| Playbook | Classes | `max_acu_limit` | Baseline engineer-hours (for impact panel) | Text |
|---|---|---|---|---|
| `security-fix` | `security`, `bug` | 20 | 6.0 | [`playbooks/security-fix.md`](./playbooks/security-fix.md) |
| `dep-upgrade` | `security-dep`, `frontend-dep` | 10 | 2.0 | [`playbooks/dep-upgrade.md`](./playbooks/dep-upgrade.md) |
| `flaky-test` | `flaky-test`, `typing` | 12 | 3.0 | [`playbooks/flaky-test.md`](./playbooks/flaky-test.md) |
| `deprecation` | `deprecation`, `perf` | 12 | 3.0 | [`playbooks/deprecation.md`](./playbooks/deprecation.md) |

Baselines are stated assumptions, labelled as such on the dashboard — not measured facts.

The playbooks themselves are created by hand in the Devin UI ([B6](./blockers.md#b6)), so the texts
in [`playbooks/`](./playbooks/README.md) are the only record of what they contain and the source
they are pasted from.

Their ids are read back with `make devin-playbooks`, which lists the organisation's
playbooks as title and id and prints the `DEVIN_PLAYBOOK_IDS` to paste into `.env`. It creates,
updates and deletes nothing — it exists because the write path does not, and because the id a
session is created with is not something the UI puts in front of whoever made the playbook.

## Structured output

`structured_output_required: true` makes the report a contract rather than prose to be parsed.

```json
{
  "type": "object",
  "required": ["outcome", "root_cause", "changes", "tests", "risk"],
  "properties": {
    "outcome":       { "enum": ["fixed", "partial", "blocked"] },
    "root_cause":    { "type": "string", "description": "Why the defect exists, not what was changed" },
    "changes":       { "type": "array", "items": { "type": "string" } },
    "tests": {
      "type": "object",
      "required": ["added", "command", "passed"],
      "properties": {
        "added":   { "type": "array", "items": { "type": "string" } },
        "command": { "type": "string" },
        "passed":  { "type": "boolean" }
      }
    },
    "risk":           { "enum": ["low", "medium", "high"] },
    "blocked_reason": { "type": "string" },
    "pr_url":         { "type": "string" },
    "confidence":     { "type": "number", "minimum": 0, "maximum": 1 }
  }
}
```

Each field drives something downstream:

| Field | Drives |
|---|---|
| `outcome` | `blocked` forces the `BLOCKED` transition and escalation ([04](./04-state-machine.md)) |
| `root_cause` | Posted as a PR comment — the reviewer's summary, and the evidence that diagnosis happened |
| `tests.added` | Acceptance gate: a remediation with an empty array is flagged for review ([08](./08-testing.md)) |
| `risk`, `confidence` | Dashboard triage ordering |
| `blocked_reason` | The failure-breakdown panel ([07](./07-observability.md)) |

## Tag vocabulary

Tags are how an outside reviewer can open the Devin dashboard and confirm that what Sentinel claims
to have sent is what Devin actually received. They are therefore treated as part of the contract.

| Tag | Example | Purpose |
|---|---|---|
| `sentinel` | `sentinel` | Namespace — every session Sentinel creates carries it |
| `repo:<owner>/<name>` | `repo:taxpon/superset` | Target repository |
| `issue:<n>` | `issue:42` | Back-reference to the originating issue |
| `class:<class>` | `class:security` | Issue class, for per-class metrics |
| `run:<delivery_id>` | `run:8f1c…` | The GitHub delivery that started it — the correlation id used in logs |
| `cycle:<n>` | `cycle:2` | Appended on each review-fix iteration |
| `outcome:<state>` | `outcome:merged` | Appended on reaching a terminal state |

The vocabulary is registered once at bootstrap via `PUT /v3/organizations/{org_id}/tags`; creating a
session with an unregistered tag may be rejected ([B7](./blockers.md)).

Both tag bodies are the same one-field object. `TagsCreateRequest` and `TagsResponse` are each
`{"tags": string[]}` with `tags` required, and `SessionTagsUpdateRequest` is the same with a
**maximum of 50** tags. A session accumulates one `cycle:` tag per fix cycle on top of the six it
is created with, so `MAX_FIX_CYCLES` would have to grow by an order of magnitude before that
ceiling is reachable.

Session tags are appended with `POST`, which the reference describes as "append tags to a session
(deduplicating with existing tags)" — so re-tagging a session it has already tagged is safe. The
`PUT` beside it replaces the session's whole set and is not used.

## Prompt construction

The guiding rule: **delegate the task, not the steps.** The prompt states the objective, the
constraints and the definition of done, then gets out of the way. Investigation, approach and
implementation are Devin's.

Template:

```
GitHub issue #{number} in {repo}: {title}

{issue body}

Objective
  Diagnose the underlying cause and fix it. Do not paper over the symptom.

Definition of done
  - A branch off {base_branch} with the fix.
  - A regression test that fails before your change and passes after it.
  - The relevant existing test suite passes locally.
  - A pull request against {base_branch} whose description explains the root cause.

Constraints
  - Do not modify generated files or unrelated modules.
  - If you conclude this cannot or should not be fixed, report outcome "blocked"
    with a specific blocked_reason instead of forcing a change.
```

Resume messages follow the same principle — state the new fact, restate the goal. CI failure, the
first loop edge:

```
CI failed on {sha}. Failing job: {job_name}.

{log excerpt, last 100 lines}

Diagnose the failure and push a fix to the same branch.
```

A reviewer requesting changes is the second loop edge ([04](./04-state-machine.md)) and takes the
same shape. Inline comments are forwarded along with the review body, because a review can request
changes with an empty body and say everything on the diff:

```
A reviewer requested changes on {pr_url}.

{review body, then inline comments}

Address the review and push a fix to the same branch.
```

Both end with the same notice. The cycle is the one fact the session cannot observe for itself —
Sentinel counts the cycles and enforces `MAX_FIX_CYCLES` outside Devin — and knowing the budget is
what makes reporting `blocked` a real alternative to spending the last cycle on a guess
([ADR](./adr/2026-08-08-resume-messages-state-the-cycle-budget.md)):

```
This is fix cycle {cycle} of {max_cycles}. If the goal cannot be reached within the
remaining cycles, report outcome "blocked" with a specific blocked_reason rather
than continuing.
```

Where a substitution would be empty, a parenthetical stands in rather than leaving a blank gap where
the evidence belongs: `(No log output was captured for the failing job.)` for a job that failed
before producing output, `(The reviewer left no written feedback.)` for a review with nothing
written anywhere, and `(The issue has no description.)` for an issue filed with a title alone.

## Knowledge notes

Seeded once at bootstrap so every session starts with repository context instead of rediscovering
it, and so the prompt stays short:

1. How to run Superset's Python and frontend tests, and which suites are slow.
2. `pre-commit` configuration and the lint rules that gate CI.
3. PR conventions — title format, required description sections.
4. Directories that must not be touched (generated assets, vendored code, translations).

`KnowledgeNoteCreateRequest` requires three fields — `name`, `body` and **`trigger`**, the sentence
saying when Devin should reach for the note — and each of the four above supplies all three.
`trigger` is easy to read as optional prose and is not: a note posted without it is rejected. The
response is `KnowledgeNoteResponse`, whose identifier is **`note_id`**; that is what
`DEVIN_KNOWLEDGE_IDS` records.

**The reference does document a listing.** `GET /v3/organizations/{org_id}/knowledge/notes` —
"List org-level notes", `ManageAccountKnowledge`, the same permission the create needs — returns
`PaginatedResponse[KnowledgeNoteResponse]`. It is outside [the endpoints Sentinel calls](#endpoints-used)
and stays outside for now, but the claim it was left out on — that nothing can list what the
bootstrap created, so `.env` is the only record it exists
([ADR](./adr/2026-08-08-env-is-the-bootstrap-scripts-record.md)) — is not true of the API. It now
matters more than it did: the schedule's id was the one thing that told the bootstrap script this
organisation had been through it before, and with [step 4 removed](#scheduled-sweep) a `.env`
copied from `.env.example` beside four notes that already exist creates four more. Whether
idempotence should be established by reading the organisation rather than by trusting a file is a
decision for whoever owns that ADR; this document only records that the option exists and that the
cost of not taking it has gone up.

## The vulnerability sweep, and why nothing schedules it {#scheduled-sweep}

The sweep is [`src/sentinel/scanner/audit.py`](./adr/2026-08-08-both-ecosystems-are-resolved-against-osv.md):
it reads the target's manifests over the GitHub contents API, resolves both dependency trees
against OSV, and files at most three issues a run carrying the `devin:autofix` label and a `class:`
label. Those issues re-enter at the top of [the primary flow](./02-architecture.md), so Sentinel is
both the producer and the consumer of that work.

**Nothing invokes it automatically.** This document used to specify a fourth bootstrap step that
created a recurring Devin session — `POST /v3/organizations/{org_id}/schedules`, `ScheduleCreateRequest`,
nightly at `0 3 * * *` — and both the step and the endpoint have been removed. Devin's own guide
carries a banner on Scheduled Sessions:

> Automations are now the recommended way to run Devin on a schedule. Automations support schedule
> triggers along with event-driven triggers (Slack, GitHub, Linear, webhooks), conditions,
> invocation limits, and more. **If you're setting up a new scheduled workflow, use an automation
> with a Schedule trigger instead.**
>
> Existing scheduled sessions will continue to work.

The API still works, so this is not a defect. It is a choice not to build new work on a superseded
feature, made by the repository owner and recorded in [B16](./blockers.md#b16). Automations were not
adopted in its place: the v3 reference index lists no endpoint for creating one, so it may be a
web-app feature, and an automatic sweep is not what Sentinel is for — label → remediate → merge is,
and the eight issues for this run were chosen by hand and are already filed.

### How to run it

By hand, deliberately, from the project environment. There is no `make` target and no CLI, because
a sweep that files issues on a public repository is not something to make one keystroke away:

```bash
uv run python - <<'PY'
import asyncio

from sentinel.config import get_settings
from sentinel.scanner.audit import OsvClient, TargetRepository, sweep


async def main() -> None:
    settings = get_settings()
    async with TargetRepository(settings) as repository, OsvClient() as osv:
        report = await sweep(tracker=repository, osv=osv, settings=settings)
    print(f"scanned {report.scanned}, filed {len(report.filed)}, skipped {len(report.skipped)}")


asyncio.run(main())
PY
```

It needs `GITHUB_TOKEN` and `TARGET_REPO`, and it **writes to the target repository**. Running it
twice files nothing twice: every issue it writes carries a fingerprint it reads back off the issues
it has already filed, open and closed
([ADR](./adr/2026-08-08-the-filed-issue-is-the-sweeps-memory.md)). That property was what made a
failed nightly run safe to leave until the next night; with nothing scheduling it, it is what makes
a re-run by hand safe instead.

## Degradation

Some endpoints require enterprise scope, or a permission the service user may not carry. Each has a
defined fallback so that a permission gap degrades a panel rather than breaking the pipeline:

| Capability | Preferred | Fallback | Shape |
|---|---|---|---|
| Aggregate session and merged-PR metrics | `GET /v3/enterprise/metrics/sessions` | Compute from Sentinel's own `remediation` table | `SessionMetricsResponse`: `sessions_with_merged_prs_count`, `avg_acus_per_session`, `sessions_created_count`, all required. `time_before` and `time_after` are **required** epoch parameters — the caller states the window |
| ACU spend | `GET /v3/organizations/{org_id}/consumption/daily` | Sum `acus_consumed` across sessions | `ConsumptionResponse`: `total_acus`, and the days under **`consumption_by_date`**, each `{date, acus, acus_by_product}`. `date` is an epoch integer, at the billing-day boundary of midnight Pacific — 08:00 UTC |
| Playbook creation | `POST /v3/enterprise/playbooks` | Create playbooks in the Devin UI and supply the ids via `PLAYBOOK_IDS` env config | Not called. `POST /v3/organizations/{org_id}/playbooks` also exists, which is why B6's premise is wrong |
| Playbook discovery | `GET /v3/organizations/{org_id}/playbooks` | Open each playbook in the Devin web app and read its id from the page | `PaginatedResponse[PlaybookResponse]`; `playbook_id`, `title` and `access_type` (`enterprise` or `org`) required. Paged by `after` and `first` (default 100, max 200) |
| Tag vocabulary discovery | `GET /v3/enterprise/organizations/{org_id}/tags` | Read the organisation's allowed tags in the Devin web app before registering, since the registration replaces them | `TagsResponse`: `{"tags": string[]}`, `tags` required |

Both ACU capabilities now serve the budget guard alone, and no dashboard figure is computed from
either — spend reporting was removed once it became clear this account is not billed in ACUs
([07](./07-observability.md#analytics-api)), so no panel has a provenance left to label. The live
table's ACU column is the one Devin-reported number still on screen, and it is not served by a
fallback: it is `remediation.acus_consumed`, copied from the session payload by the poller and shown
as the observation it is.

## Client behaviour

- **Retries**: exponential backoff with jitter on `429` and `5xx`; `4xx` other than `429` fails the
  job immediately with the response body recorded in `remediation_event.detail`. `POST
  /v3/organizations/{org_id}/sessions` is the one exception and is sent **exactly once** — see
  Idempotency below. Every read keeps all three attempts; the poller depends on it.
- **Timeouts**: 30 s connect/read; session creation is never on a webhook request path. A read
  timeout on a creation is not evidence that nothing was created, which is why the create is not
  retried.
- **Idempotency**: enforced by Sentinel, because the API offers nothing to enforce it with.
  `SessionCreateRequest` defines no idempotency key and the endpoint accepts no header that acts as
  one. Two layers, and both are needed: `UNIQUE (repo, issue_number)` stops a second *remediation*
  for one issue, and [adopt-or-create](#adopt-or-create) stops a second *session* for one
  remediation — including across a job retry, which the uniqueness constraint never saw.
- **Errors**: every `4xx` and `429` is `ProblemDetail` (RFC 9457) — `title` and `status` required,
  `detail` retained from the legacy body, and `errors` carrying field-level failures on `422` only.
  The client stores the body as text rather than parsing it, so a rejection is diagnosable from
  `remediation_event.detail` without depending on that shape.
- **Logging**: every call logs method, path, status, latency and the `run:<delivery_id>` correlation
  id. Tokens are never logged.
