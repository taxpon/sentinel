---
title: The mapping decides whether a delivery has a trigger to apply, not the caller
status: accepted
date: 2026-08-08
type: architecture
areas: [github, pipeline]
tasks: [T20, T22]
files: [src/sentinel/github/events.py]
specs: [docs/06-event-pipeline.md, docs/04-state-machine.md]
supersedes:
---

# The mapping decides whether a delivery has a trigger to apply, not the caller

## Context

The ingress path in [06](../06-event-pipeline.md#ingress-path) fixes the order: map the event to an
intent, *then* upsert the remediation `ON CONFLICT DO NOTHING`, *then* transition. So the mapping
cannot see the remediation's state — it does not exist yet when the mapping runs.

Two rows of the subscribed-events table produce no state change at all. An approving review is
recorded for review latency, and a comment mentioning the bot is forwarded to the session and
counted against `human_message_count`; neither moves the remediation.

And one row is a trap. `Trigger.ISSUE_LABELLED` is legal only from `None`
([ADR](./2026-08-08-trigger-indexed-state-machine.md)), while
[06](../06-event-pipeline.md#deduplication) assigns "label removed and re-added" to the domain
deduplication layer — one remediation, two deliveries. The second delivery therefore arrives with a
remediation that already exists, and applying the trigger blindly raises `IllegalTransitionError`.
That is not a fault: it is a person clicking a label twice, and it must end as a `202`.

## Decision

Two functions, called at the two moments the ingress path can call them.

`map_event(event, payload, *, settings) -> MappedEvent` is pure and stateless: the intent, the
identifiers needed to find the remediation, and the trigger the *row* implies.

`trigger_for(mapped, state) -> Trigger | None` is the second step, taken once the state is known —
`state` being `None` when the lookup found no remediation at all. It returns `None` in three cases,
all of which mean the same thing to the caller: record the delivery and apply nothing.

- The intent carries no trigger: an approval, a forwarded comment.
- No remediation exists and the trigger cannot create one. A pull request closed by hand on the
  fork, or a check suite on a branch Sentinel never touched, would otherwise raise.
- A remediation exists and the trigger creates one. This is the re-labelling case.

The last two are one boundary seen from either side, and the code says so rather than special-casing
`ISSUE_LABELLED` by name: `is_legal(None, trigger)` asks the state machine which triggers create a
remediation, and the answer must agree with whether one was found.

It absorbs nothing else. A `check_suite.completed` for a remediation that exists but is in no CI
state still reaches `transition()` and still raises, because that one is a bug and the spec says
illegal transitions raise rather than silently no-op ([04](../04-state-machine.md#invariants),
invariant 6).

`pr_linked` is deliberately not a parameter. Nothing resolved here puts a condition on the pull
request link, so taking one would suggest it changes the answer.

## Alternatives considered

| Option | Why not |
|---|---|
| Let T22 guard the re-label case | It is the one piece of this the state machine's own docstring warns about, and the warning would then live two modules away from the code that has to heed it. The webhook handler is also not the last caller that will map an event |
| Pass the state into `map_event` | Inverts the ingress order: the state is only known after the upsert, and the upsert needs the identifiers the mapping produces |
| Return `None` for every illegal combination | Then a genuinely impossible sequence — CI completing for a remediation that never opened a pull request — becomes a silent no-op, and invariant 6 stops holding for the whole webhook path |
| Absorb the re-label case only, and let the caller check for a missing remediation | What the first version of this did. The two are the same condition, so splitting them puts half the rule in the module and half in a comment somebody has to read: `trigger_for(cancel, None)` returned `Trigger.FAILED`, which `transition()` raises on, and the contract that made it safe was neither stated nor tested |
| Have `transition()` absorb `ISSUE_LABELLED` from a live state | It is T14's file, and absorbing there would hide the same case from the poller and the worker, which have no deduplication layer standing behind them |

## Consequences

The caller's shape is fixed and small: map, look up or upsert, `trigger_for`, and transition only if
it returned something. The caller never has to guard `transition()` against a remediation that
turned out **not to exist** — the mistake this otherwise invites, since that is the ordinary case
for a repository people also use by hand. Every row of the table is asserted against
`trigger_for(mapped, None)` in `tests/test_events.py`, and the triggers it does return are put
through `transition()` there rather than merely compared.

The guarantee is about existence and nothing else. A returned trigger may still be illegal from the
state the remediation is actually in, and should be — an impossible sequence inside the lifecycle
raises by design (invariant 6). One such sequence is reachable today with no fault at the call site:
a remediation in `PR_OPENED` whose `check_suite.requested` was lost, receiving the `completed` that
follows, which neither CI trigger admits from `PR_OPENED`. See
[the poller record](./2026-08-08-the-poller-links-the-pull-request.md) for why that `requested` is
commonly lost and for the transition-table widening in flight with T14. Until that lands, T22 will
see a raise it did not cause.

The cost is that a reader has to know both functions to see the whole rule: `MappedEvent.trigger` is
what the table says and `trigger_for` is what to apply, and they differ at the existence boundary.
The field is not hidden, so a caller reaching for `mapped.trigger` directly gets the raising
behaviour back.

**What would tell us this was wrong:** a trigger needing to be absorbed for a reason that is *not*
about whether the remediation exists. The rule here is one predicate over one boundary; a second,
unrelated special case would mean the knowledge belongs in the state machine as a per-trigger
"absorb when illegal" flag, next to the `PullRequestCondition` that already lives there, rather than
in a function outside it.
