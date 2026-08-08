---
title: The resume handler reads which loop edge it is on from the state, not from the job payload
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T22, T23]
files: [src/sentinel/pipeline/handlers.py]
specs: [docs/04-state-machine.md, docs/06-event-pipeline.md]
supersedes:
---

# The resume handler reads which loop edge it is on from the state, not from the job payload

## Context

One job kind, `resume_session`, serves both edges of the review-fix loop
([04](../04-state-machine.md#the-review-fix-loop)): `CI_FAILED → RUNNING`, which forwards a failing
job's log, and `CHANGES_REQUESTED → RUNNING`, which forwards a review body and its inline comments.
The two need different GitHub calls and different message templates, so the handler has to know
which it is on.

The job is enqueued by the ingress and claimed by a worker some time later — after a deferral, a
backoff, or a lease being reclaimed. Between those two moments the remediation can move.

What the database already holds when the job is claimed:

- `remediation.state`, which is `CI_FAILED` or `CHANGES_REQUESTED` for a resume that is still due;
- `pr_number` and `pr_url`, written once when `PR_OPENED` was entered (invariant 5);
- `devin_session_id`, written when the session was created.

What it does not hold: the head SHA the suite reported on, the review's body, and the review id the
inline comments hang off. Those exist only on the delivery that caused the enqueue.

The two loop states cannot be reached from each other. `CHANGES_REQUESTED` is reachable only from
`IN_REVIEW`, and `CHECK_SUITE_FAILED` is absorbed rather than applied from `CHANGES_REQUESTED`
([04](../04-state-machine.md#check-suite-events)). Leaving either one means passing through
`RUNNING`, which is where a pending resume stops being legal at all.

## Decision

The state selects the edge. `is_legal(state, SESSION_RESUMED)` decides whether there is anything to
do, and `state is CI_FAILED` decides which message to build.

The payload carries facts and never a verdict, and every key in it is optional:

| Key | Absent means |
|---|---|
| `head_sha` | Read the pull request's current head instead |
| `review_id` | Forward the review body without its inline comments |
| `review_body` | The reviewer wrote nothing here — the templates have a parenthetical for it |

A job whose remediation has moved on is completed without a message, which is the same code path
that makes a reclaimed resume harmless.

## Alternatives considered

| Option | Why not |
|---|---|
| A `trigger` or `edge` key in the payload, authoritative | Two sources for one fact, and they can disagree: a payload saying `changes_requested` for a remediation sitting in `CI_FAILED` would send the wrong message and increment the cycle anyway. The state is the one the transition is computed from regardless, so making it the one the message is built from removes the disagreement rather than resolving it |
| A separate job kind per edge | The kinds are enumerated in [06](../06-event-pipeline.md#jobs-and-claiming) and in `docs/03-data-model.md`; splitting one would be a spec change and a migration's worth of vocabulary for a branch the state already answers |
| Require every payload key, and fail the job when one is missing | Fails a remediation over a webhook GitHub shaped slightly differently, or over an ingress that did not carry a field. Each missing key has an answer — GitHub still has the pull request, and `docs/05-devin-integration.md` specifies a parenthetical for absent evidence — so the resilient reading costs one extra `GET` in the rare case |
| Have the ingress resolve the facts and store them | It is the one thing the ingress must not do: [ADR](./2026-08-07-respond-202-before-external-calls.md) keeps every external call off the webhook request path |

## Consequences

The ingress and the worker are coupled by a payload that is additive: a key the ingress does not yet
send degrades the message rather than failing the job, so the two can be built and changed
independently. The handler has one guard — `is_legal` — that covers cancellation, a duplicate
resume, and a remediation that a webhook moved while the message was in flight; there is no separate
check for any of them.

The cost is that a payload key which is wrong rather than missing is used as given: a stale
`head_sha` would fetch the wrong commit's failing job. Nothing here detects that, because the value
is only ever compared against GitHub, which would confirm it.

**What would tell us this was wrong:** a third resume edge — a maintainer's comment forwarded into
the session, say — which is not a state and would have to be carried as a payload verdict after
all; or resume jobs arriving for a remediation in a loop state the ingress did not put it in, which
would mean the two states are reachable from each other in a way [04](../04-state-machine.md) does
not describe.
