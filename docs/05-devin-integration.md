# Devin integration

> **Status:** Design · **Answers:** Which v3 endpoints and features does Sentinel use, and how are prompts, playbooks, tags and structured output constructed?

**Sentinel uses the v3 API exclusively.** No v1 or v2 endpoint is called anywhere in the codebase.

Base URL `https://api.devin.ai`, authenticated with a service-user token
(`Authorization: Bearer cog_…`).

## Endpoints used

| Method | Path | Used for |
|---|---|---|
| `POST` | `/v3/organizations/{org_id}/sessions` | Create a remediation session |
| `GET` | `/v3/organizations/{org_id}/sessions/{devin_id}` | Poller reconciliation — status, ACUs, structured output, PRs |
| `GET` | `/v3/organizations/{org_id}/sessions` | Backfill and in-flight listing |
| `POST` | `/v3/organizations/{org_id}/sessions/{devin_id}/messages` | Review-fix loop: feed CI logs and reviewer feedback |
| `POST` | `/v3/organizations/{org_id}/sessions/{devin_id}/tags` | Append lifecycle tags (`cycle:N`, `outcome:merged`) |
| `PUT` | `/v3/organizations/{org_id}/tags` | Register the organisation's allowed tag vocabulary at bootstrap |
| `GET` | `/v3/enterprise/organizations/{org_id}/tags` | Read the vocabulary that registration would replace — `bootstrap_devin.py --dry-run` only |
| `POST` | `/v3/organizations/{org_id}/knowledge/notes` | Seed repository conventions once at bootstrap |
| `POST` | `/v3/organizations/{org_id}/schedules` | Nightly vulnerability sweep |
| `GET` | `/v3/organizations/{org_id}/playbooks` | Read back the ids of the hand-made playbooks — `make devin-playbooks` |
| `GET` | `/v3/organizations/{org_id}/consumption/daily` | Daily ACU spend for the budget guard and cost panel |
| `GET` | `/v3/enterprise/metrics/sessions` | Merged-PR and ACU aggregates — *enterprise scope, optional* |

The last row requires enterprise scope and the `ViewAccountMetrics` permission. Sentinel treats it
as an enhancement, not a dependency — see [Degradation](#degradation) and
[B5](./blockers.md).

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

`POST /v3/organizations/{org_id}/sessions`. Field-by-field mapping from Sentinel state:

| Devin field | Sentinel value |
|---|---|
| `prompt` | Rendered from the class prompt template (below) |
| `title` | `[sentinel] #<issue> <issue title>` |
| `tags` | The tag set below |
| `repos` | `["taxpon/superset"]` |
| `playbook_id` | `PLAYBOOK_IDS[issue_class]`, falling back to `PLAYBOOK_IDS[playbook_name]` — four playbooks serve eight classes, so either key resolves ([ADR](./adr/2026-08-08-playbook-ids-keyed-by-class-or-name.md)) |
| `knowledge_ids` | The bootstrap note ids |
| `structured_output_schema` | The schema below |
| `structured_output_required` | `true` |
| `max_acu_limit` | `ACU_CAPS[issue_class]` |
| `resumable` | `true` — required for the review-fix loop |

Response fields consumed: `session_id`, `url`, `status`, `tags`, `acus_consumed`,
`pull_requests[]`. On `GET`, additionally `structured_output` and `status_detail` — the latter
distinguishes `working` from `waiting_for_user`, which is how a stalled session is detected.

`status` values the poller maps onto [the state machine](./04-state-machine.md): `new`, `claimed`,
`running`, `exit`, `error`, `suspended`, `resuming`.

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

## Scheduled sweep

`POST /v3/organizations/{org_id}/schedules` creates a recurring session that closes the loop back
into the pipeline:

| Field | Value |
|---|---|
| `name` | `sentinel-nightly-vuln-sweep` |
| `schedule_type` | `recurring` |
| `frequency` | `0 3 * * *` (UTC) |
| `prompt` | Run `pip-audit` and `npm audit` on the target repo; for each *new* finding not already tracked, open a GitHub issue with the `devin:autofix` label and the appropriate `class:` label; do not open duplicates |
| `tags` | `sentinel`, `class:scheduled-sweep` |
| `notify_on` | `failure` |

Issues it files re-enter at the top of [the primary flow](./02-architecture.md) — Devin becomes both
the producer and the consumer of work.

## Degradation

Some endpoints require enterprise scope, or a permission the service user may not carry. Each has a
defined fallback so that a permission gap degrades a panel rather than breaking the pipeline:

| Capability | Preferred | Fallback |
|---|---|---|
| Aggregate session and merged-PR metrics | `GET /v3/enterprise/metrics/sessions` | Compute from Sentinel's own `remediation` table |
| ACU spend | `GET /v3/organizations/{org_id}/consumption/daily` | Sum `acus_consumed` across sessions |
| Playbook creation | `POST /v3/enterprise/playbooks` | Create playbooks in the Devin UI and supply the ids via `PLAYBOOK_IDS` env config |
| Playbook discovery | `GET /v3/organizations/{org_id}/playbooks` | Open each playbook in the Devin web app and read its id from the page |
| Tag vocabulary discovery | `GET /v3/enterprise/organizations/{org_id}/tags` | Read the organisation's allowed tags in the Devin web app before registering, since the registration replaces them |

The dashboard labels any figure served by a fallback, so a reader always knows which numbers came
from Devin and which Sentinel derived itself.

## Client behaviour

- **Retries**: exponential backoff with jitter on `429` and `5xx`; `4xx` other than `429` fails the
  job immediately with the response body recorded in `remediation_event.detail`.
- **Timeouts**: 30 s connect/read; session creation is never on a webhook request path.
- **Idempotency**: enforced by Sentinel's `UNIQUE (repo, issue_number)`, not by a Devin-side key.
- **Logging**: every call logs method, path, status, latency and the `run:<delivery_id>` correlation
  id. Tokens are never logged.
