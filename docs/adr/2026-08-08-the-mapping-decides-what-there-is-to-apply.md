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

`trigger_for(mapped, state, *, pr_linked=False) -> Trigger | None` is the second step, taken once
the state is known. It returns `None` for an intent that carries no trigger, and `None` for a
re-labelling — asking `is_legal(state, Trigger.ISSUE_LABELLED)`, which is `False` for every state
except `None`. Either way the caller's branch is the same one: record the delivery against the
remediation and stop.

It absorbs nothing else. A `check_suite.completed` for a remediation in no CI state still reaches
`transition()` and still raises, because that one is a bug and the spec says illegal transitions
raise rather than silently no-op ([04](../04-state-machine.md#invariants), invariant 6).

## Alternatives considered

| Option | Why not |
|---|---|
| Let T22 guard the re-label case | It is the one piece of this the state machine's own docstring warns about, and the warning would then live two modules away from the code that has to heed it. The webhook handler is also not the last caller that will map an event |
| Pass the state into `map_event` | Inverts the ingress order: the state is only known after the upsert, and the upsert needs the identifiers the mapping produces |
| Return `None` for every illegal combination | Then a genuinely impossible sequence — CI completing for a remediation that never opened a pull request — becomes a silent no-op, and invariant 6 stops holding for the whole webhook path |
| Have `transition()` absorb `ISSUE_LABELLED` from a live state | It is T14's file, and absorbing there would hide the same case from the poller and the worker, which have no deduplication layer standing behind them |

## Consequences

The caller's shape is fixed and small: map, upsert, `trigger_for`, and transition only if it
returned something. The two "record but do not move" rows and the re-labelling case collapse into
one branch, so none of them can be forgotten independently, and each is a test in
`tests/test_events.py` asserting `None` against a recorded payload.

The cost is that a reader now has to know both functions to see the whole rule: `MappedEvent.trigger`
is what the table says and `trigger_for` is what to apply, and they differ for exactly one intent.
The field is not hidden, so a caller reaching for `mapped.trigger` directly gets the raising
behaviour back.

**What would tell us this was wrong:** a second trigger needing to be absorbed for reasons of its
own. Two special cases in `trigger_for` means the knowledge belongs in the state machine as a
per-trigger "absorb when illegal" flag, next to the `PullRequestCondition` that already lives there,
rather than in a function outside it.
