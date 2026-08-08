---
title: The resume handler reads its loop edge from the state, and drops a job enqueued on the other one
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T22, T23]
files: [src/sentinel/pipeline/handlers.py]
specs: [docs/04-state-machine.md, docs/06-event-pipeline.md]
supersedes:
---

# The resume handler reads its loop edge from the state, and drops a job enqueued on the other one

## Context

One job kind, `resume_session`, serves both edges of the review-fix loop
([04](../04-state-machine.md#the-review-fix-loop)): `CI_FAILED → RUNNING`, which forwards a failing
job's log, and `CHANGES_REQUESTED → RUNNING`, which forwards a review body and its inline comments.
The two need different GitHub calls and different message templates, so the handler has to know
which it is on.

The job is enqueued by the ingress and claimed by a worker some time later — after a deferral, a
backoff, or a lease being reclaimed. [A rate limit GitHub named a delay
for](./2026-08-08-a-rate-limit-with-an-answer-defers-the-job.md) can hold a job for up to an hour.
Between enqueue and claim the remediation can move.

What the database already holds when the job is claimed:

- `remediation.state`, which is `CI_FAILED` or `CHANGES_REQUESTED` for a resume that is still due;
- `pr_number` and `pr_url`, written once when `PR_OPENED` was entered (invariant 5);
- `devin_session_id`, written when the session was created.

What it does not hold: the head SHA the suite reported on, the review's body, and the review id the
inline comments hang off. Those exist only on the delivery that caused the enqueue.

**The two loop states are reachable from each other, in one direction.** An earlier draft of this
record asserted they were not, and it was wrong. `CI_FAILED → CHANGES_REQUESTED` needs no `RUNNING`
at all, and every step is in [04](../04-state-machine.md)'s own diagram:

```
CI_FAILED --check_suite_succeeded--> CI_PASSED --review_requested--> IN_REVIEW --changes_requested--> CHANGES_REQUESTED
```

None of the three is absorbed: `CHECK_SUITE_SUCCEEDED` lists `CI_PASSED`, `IN_REVIEW` and
`CHANGES_REQUESTED` in `absorbed_from`, so `CI_FAILED` is a live source for it. A green re-run
followed by a review is an ordinary hour on a pull request. The reverse cannot happen —
`CHECK_SUITE_FAILED` absorbs from `CHANGES_REQUESTED` — so the crossing is one-directional.

A CI-edge job claimed after that walk, with the state alone deciding, takes the review branch and
sends this:

```
A reviewer requested changes on https://github.com/taxpon/superset/pull/42889.

(The reviewer left no written feedback.)

Address the review and push a fix to the same branch.
```

Three harms, in order of how long they last: Devin is told a review happened that did not, with
nothing to act on; a fix cycle is spent against `MAX_FIX_CYCLES`; and the state is left `RUNNING`,
so the **real** review's resume job then fails `is_legal` and is completed in silence — the
reviewer's feedback never reaches Devin at all.

## Decision

The state still selects the edge, and the payload is now **checked against it**. A job whose edge
disagrees with the state is stale: the remediation has walked away from it, so it is completed
without a message and without a cycle.

Dropping it loses nothing. Entering either loop state enqueues a `resume_session` of its own
([04](../04-state-machine.md)), so a fresher job for the state the remediation is actually in is
already on the queue — which is why the fix is to drop the stale one rather than to re-derive its
edge and send it anyway.

Two ways to tell it is stale, in order:

1. **`payload["trigger"]`**, naming the transition that enqueued the job — `check_suite_failed` or
   `changes_requested`, from `sentinel.pipeline.state.Trigger`, which is the vocabulary the ingress
   already applies. Exact, and the contract T22 should meet.
2. **The review evidence**, when no trigger is declared. A `pull_request_review.submitted` delivery
   carries the review id and the body; a check-suite failure carries neither. So a job that finds
   `CHANGES_REQUESTED` with no trace of a review in its payload either came from the other edge, or
   could not have said anything if it had not — the message it would send is the empty
   parenthetical.

The payload otherwise still carries facts and never a verdict, and every key has a defined absence:

| Key | Absent means |
|---|---|
| `trigger` | Fall back to the review evidence |
| `head_sha` | Read the pull request's current head instead |
| `review_id` | Forward the review body without its inline comments |
| `review_body` | The reviewer wrote nothing here — the templates have a parenthetical for it |

A **malformed** value is treated as an absent one, for the same reason: `int("none")` on a review id
raises `ValueError`, which is not a permanent failure to the worker, so the remediation would be
retried five times and then failed over a payload field that has a perfectly good degraded meaning.

## Alternatives considered

| Option | Why not |
|---|---|
| The state alone, as this record first said | It is wrong, and the way it is wrong is silent: it sends a fabricated review message, spends a cycle, and discards the real review's job. The premise it rested on — that the two loop states are unreachable from each other — is false in the `CI_FAILED → CHANGES_REQUESTED` direction |
| A `trigger` key in the payload, authoritative on its own | Two sources for one fact that can disagree, and this is the disagreement — but treating the payload as authoritative resolves it the other wrong way, by trusting an ingress that has not been written against a state that is a fact. Comparing them and dropping on a mismatch is what neither source can do alone |
| Infer the edge from which keys are present | `head_sha` is optional by design, so a CI job that omitted it would be read as a review. The evidence check above is deliberately one-sided — it only ever asks whether a *review* is supported, never whether CI is |
| Send the CI message anyway when the state says review | It would be right about the edge and wrong about the state: the transition it then applies is `CHANGES_REQUESTED → RUNNING`, so the real review job is discarded exactly as before. The harm that outlives the wrong message is the swallowed job, not the message |
| A separate job kind per edge | The kinds are enumerated in [06](../06-event-pipeline.md#jobs-and-claiming) and in [03](../03-data-model.md); splitting one would be a spec change and a migration's worth of vocabulary, and it would still need this check, because the state can move under a job of either kind |
| Require every payload key, and fail the job when one is missing | Fails a remediation over a webhook GitHub shaped slightly differently. Each missing key has an answer — GitHub still has the pull request, and [05](../05-devin-integration.md) specifies a parenthetical for absent evidence |
| Have the ingress resolve the facts and store them | It is the one thing the ingress must not do: [ADR](./2026-08-07-respond-202-before-external-calls.md) keeps every external call off the webhook request path |

## Consequences

The wrong-edge message cannot be sent: either the payload agrees with the state, or the job is
dropped and the fresher one drives the lap. The check is cheap, needs no new query, and its failure
mode is losing a lap rather than fabricating one.

The ingress and the worker stay coupled by a payload that is additive: a key T22 does not send
degrades rather than failing, and `trigger` is the one worth sending because it turns a heuristic
into an exact answer.

Two costs. The fallback is biased towards dropping, so an ingress that enqueues a review resume with
an *empty* payload has every lap dropped and the remediation sits in `CHANGES_REQUESTED` with no
pending job. That is recoverable and logged at warning per occurrence, where the alternative — a
fabricated message that also destroys the real job — is neither. And a payload key that is wrong
rather than missing is still used as given: a stale `head_sha` fetches the wrong commit's failing
job, which nothing here detects, because the only thing it could be compared against is GitHub,
which would confirm it.

**What would tell us this was wrong:** `worker.resume.wrong_edge` firing on jobs that were not
stale, which would mean the evidence fallback is too eager and `trigger` has to become required; or
remediations resting in `CHANGES_REQUESTED` with no pending resume, which would mean the fallback is
dropping laps that had something to say.
