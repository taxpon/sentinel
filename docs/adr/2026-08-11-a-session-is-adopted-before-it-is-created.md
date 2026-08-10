---
title: A session is adopted before it is created, and the create is sent once
status: accepted
date: 2026-08-11
type: architecture
areas: [devin, pipeline]
tasks: [T11, T23]
files: [src/sentinel/devin/client.py, src/sentinel/devin/playbooks.py, src/sentinel/devin/schemas.py]
specs: [docs/05-devin-integration.md, docs/06-event-pipeline.md]
supersedes:
---

# A session is adopted before it is created, and the create is sent once

## Context

On 2026-08-11 five issues were labelled on the target repository within 2.3 seconds. Devin's API
became overloaded — its own web app was showing a server-load error — and
`POST /v3/organizations/{org_id}/sessions` stopped answering within the client's 30 s timeout.

The client treated a transport error as retryable and made three attempts. **Every attempt created
another session.** The requests had arrived and Devin had created the sessions; only the responses
were lost. Listing sessions afterwards showed issues #3, #4 and #6 with three sessions each — nine
in total, created inside about ninety seconds, every one carrying Sentinel's own `[sentinel] #N …`
title and its five tags. They were archived by hand to stop them.

Three facts shaped what could be done about it.

**A read timeout is not a failed request.** Nothing observable at the client distinguishes "the
request never arrived" from "the request was served and the response was lost". The same is true of
a `5xx`, which can be emitted after the session row is written.

**The v3 create endpoint offers no idempotency key.** Its reference page
(`api-reference/v3/sessions/post-organizations-sessions`) was read in full for this: nineteen body
fields, none of them a client-supplied token; no `Idempotency-Key` or equivalent header; one query
parameter, `devin_id`, which is the parent session of a session Devin spawns. So a resend cannot be
marked as a resend, and the server cannot collapse it. This confirms rather than discovers what
`docs/05` already said — but `docs/05` had also never listed the archive endpoint, which the same
day's work found does exist, so the page was read rather than the document trusted.

**Narrowing the retry would have moved the duplicate, not removed it.** There are two retry layers.
The client retries within one job; when the job fails, `pipeline/worker.py` fails it back to the
queue, which schedules another attempt with backoff from ten seconds to ten minutes, and that
attempt sends the same `POST`. A fix confined to the client would have given each remediation its
second session a minute later instead of a second later.

The existing defences do not cover this. `UNIQUE (repo, issue_number)` stops a second *remediation*
for one issue and says nothing about sessions. `policy.admit_session` refuses a remediation that
already carries a `devin_session_id`, and its own record is explicit that this narrows the window
rather than closing it — it can only see an id some earlier attempt committed, and an attempt whose
response was lost commits none.

What was available: `GET /v3/organizations/{org_id}/sessions` takes a `tags` array, a `first` page
size up to 200 and an `after` cursor, and Sentinel already tags every session it creates.

## Decision

**`DevinClient.create_session` is adopt-or-create, and the `POST` is sent exactly once.**

Before posting, it calls `find_session`, which lists sessions filtered by the remediation's three
identity tags — `sentinel`, `repo:<repo>`, `issue:<n>` — following `end_cursor` while
`has_next_page`. A session that matches is returned as though it had just been created; the caller
records it exactly as it would a fresh one, and no `POST` is made. Only when nothing matches is a
session created, under `SEND_ONCE` — a `RetryPolicy(attempts=1)`. Every read, including the lookup
itself, keeps the full retry policy.

Four choices inside that are not obvious.

**The key is the remediation, not the attempt.** `session_identity` in `playbooks.py` returns those
three tags and is consumed by `session_tags`, so it is literally the first three of the five a
creation writes and the two cannot drift. It is a valid key because the database already treats it
as one: `remediation` is `UNIQUE (repo, issue_number)`, so one repository and one issue number name
one remediation for the life of the deployment, and a remediation has at most one session — the
review-fix loop resumes rather than recreates. `run:<delivery_id>` was rejected as the key: it names
one webhook delivery, so a lookup keyed on it would not see a session a *previous* job created,
which is exactly the outer-layer duplicate.

**The tags returned are re-checked here.** The reference documents `SessionsQueryParams.tags` as an
array of strings and states nothing about whether the server ANDs them, ORs them, or ignores a
value it does not recognise. Every session that comes back is therefore tested against all three
tags in `find_session`. The server-side filter is an optimisation; the check is the rule.

**The cursor is followed.** Deciding "there is no session" from the first page of a listing that
might not be filtering would create the duplicate this exists to prevent. The walk ends at
`has_next_page: false` or, after ten pages of 200, at `SessionLookupIncomplete`.

**A lookup that cannot answer creates nothing.** A refused listing, a timeout that outlasts the read
retries, and an unending listing all raise, and the job fails back to the queue. "I could not find
out" is not "there is none".

Where several sessions match, one is chosen by `(is_archived, created_at, session_id)` — a live
session before an archived one, then the earliest, then the lowest id — so repeated attempts
converge on the same session instead of each adopting a different one. Archived sessions are
candidates: archiving is how the nine were stopped by hand, and creating a tenth in response would
repeat the fault.

## Alternatives considered

| Option | Why not |
|---|---|
| Send an idempotency key | There is none to send. The endpoint's own reference page defines no such header and no such body field, and its one query parameter is a parent session id. If v3 gains one, it replaces all of this and the lookup becomes deletable |
| Stop retrying transport errors on the create, and nothing else | Necessary, and the explicit trap in this defect. The job queue retries the job, so the second session would arrive on the next attempt instead of the next second. A narrower retry is half the fix and looks like the whole of it |
| Keep retrying `429` on the create, since a rate limit is rejected before processing | Probably safe, and one more rule to be wrong about. A `5xx` is genuinely ambiguous, and a policy of "retry these statuses but not those" invites the next person to add one. One rule — the create is sent once — is what the mutation test can hold, and the queue's own backoff answers a rate limit perfectly well |
| Look sessions up by `title`, `[sentinel] #N …` | The title carries the issue title, which a maintainer can edit between the create and the retry; matching would then fail and duplicate. Tags are structured, are already validated against the registered vocabulary, and are what the listing endpoint can filter on server-side |
| Key on `run:<delivery_id>` | Identifies the delivery, not the remediation. It would have caught the three-in-a-second duplicates and missed the one-a-minute-later duplicates — the layer this record exists to close |
| Record an "intent to create" row before posting, and reconcile it afterwards | A second source of truth for something Devin already knows, plus a reconciler that has to decide what to do about intents that never became sessions. `remediation.devin_session_id` plus a listing answers the same question from the system that owns the fact |
| Create anyway when the lookup fails | This is the defect, rephrased. A failing lookup and a failing create have the same cause — an overloaded API — so this branch would fire in precisely the incident it is meant to prevent |
| Adopt only sessions that are not archived, and create when every match is archived | The state after a human cleans up a runaway is *all archived*. This would answer the cleanup with a tenth session. A remediation attached to an archived session stalls visibly and escalates, which costs nothing and is recoverable |
| Read one page and treat "not found" as "none" | A listing that ignores the filter would return a page of somebody else's sessions and the create would fire. The prefix is not an answer to this question, whatever it is to a backfill's |
| Put the lookup in `pipeline/handlers.py` | The handler is one caller. Anything else that creates a session — a backfill, a script, a future handler — would have to remember. The client is where "how a session is created" already lives, and where the tags are built |

## Consequences

A remediation ends with exactly one session however many times the attempt repeats: within a call,
across a job retry, across a reclaimed lease, and across a worker that died between the `POST` and
the commit. That last one the module docstring in `handlers.py` previously called unavoidable.

Creating a session now costs a `GET` first, and **`ViewOrgSessions` becomes a prerequisite for
creating a session**, not only for polling one. A service user with `ManageOrgSessions` alone now
fails on the first remediation with a `403` instead of working. That is deliberate: without the
ability to list, a create cannot be made safe, and failing on the first one is how a deployment
learns that on a quiet afternoon rather than a busy one.

A Devin outage now stops sessions being created rather than duplicating them. Remediations queue,
back off, and — if the outage outlasts `MAX_JOB_ATTEMPTS` — fail and escalate to a human with no
session, rather than succeeding with several. The `run:` tag stays on the created session and is
still the correlation id in the logs; it is simply not what the lookup keys on.

Two response fields are read that were not: `created_at` and `is_archived`. Both are in the
reference and in the body observed on 2026-08-10, and both are only ever a tiebreak between sessions
already known to belong to the same remediation.

**What would tell us this was wrong:** `SessionLookupIncomplete` appearing in a real run, which
would mean the `tags` filter is not filtering and the right answer is to find out what it does
rather than to raise the page budget. `devin.session.adopted` firing with `is_archived: true`
outside an incident cleanup, which would mean sessions are being archived under a live remediation
and the poller's handling of that is the thing to fix. And a `403` on the listing from a deployment
that can create — which would mean the two permissions come apart in practice and the trade made
here needs revisiting rather than documenting.
