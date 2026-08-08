---
title: Index the state machine by trigger, and keep the cycle limit inside it
status: accepted
date: 2026-08-08
type: architecture
areas: [pipeline]
tasks: [T14, T20, T23, T24]
files: [src/sentinel/pipeline/state.py]
specs: [docs/04-state-machine.md]
supersedes:
---

# Index the state machine by trigger, and keep the cycle limit inside it

## Context

Three components drive the same transitions: the webhook handler, the worker and the poller. The
transition table in [04](../04-state-machine.md) is written as `From | To | Trigger`, and every
trigger in it leads to exactly one state — what varies between rows is which states the trigger may
be applied from. Two of its invariants constrain the shape of the code: a webhook arriving after a
terminal state is recorded and otherwise ignored (invariant 1), and every transition writes exactly
one `remediation_event` (invariant 4). The cycle limit appears in three places in `docs/` — the
transition table, the loop sequence diagram and the reliability policy — with the diagram phrasing
it as `cycle >= MAX_FIX_CYCLES` before the increment and the invariant as `cycle > MAX_FIX_CYCLES`
after it.

## Decision

`transition(state, trigger, *, cycle, max_fix_cycles)` is a pure function over a table keyed by
`Trigger`, each entry carrying the target state and the frozen set of states it is legal from. It
returns a `Transition` record — `from_state`, `trigger`, `to_state`, `cycle`, `absorbed`, `reason` —
which the caller persists as one row.

Three consequences of that shape are deliberate:

- a terminal `state` absorbs every trigger, returning `absorbed=True` with the state and cycle
  unchanged, rather than raising;
- a resume one past `max_fix_cycles` returns `FAILED` with reason `cycle_limit_exhausted`, so the
  limit is compared in one place instead of in each of the three callers;
- entering `CI_PASSED` does not silently continue to `IN_REVIEW`; `automatic_trigger(state)` names
  the follow-up trigger the caller must then apply, so both transitions are logged.

## Alternatives considered

| Option | Why not |
|---|---|
| A `(from, to)` grid | The caller would have to know the target state before asking whether it may go there, putting the webhook-to-target mapping back into each of the three callers. The refusal message also could not name the trigger |
| Raise on late webhooks too | Invariant 1 makes a late webhook normal, not exceptional. Every handler would wrap the call to tell "expected" from "bug", and the distinction would be re-derived in each |
| Let callers enforce `MAX_FIX_CYCLES` | Three sites comparing a limit whose two spec phrasings differ by an off-by-one. They agree — `cycle >= max` before the increment is `cycle + 1 > max` after it — but only one place should have to know that |
| Chain `CI_PASSED → IN_REVIEW` inside one call | One call would change state twice and invariant 4 permits one event per transition |

## Consequences

The transition matrix is a single literal readable next to the spec table, and the tests can assert
its complement — that no edge exists which the spec does not list. Callers hold no state-machine
logic beyond "apply the trigger, write the row, act if `moved`". The cost is that a trigger is
restricted to one target state, so the spec's `check_suite.completed` splits into two triggers by
conclusion, which the event mapping in T20 has to do anyway.

**What would tell us this was wrong:** a trigger whose target legitimately depends on the state it
is applied from. The table would then need `(state, trigger)` keys, and the target could no longer
appear in the refusal message before the transition is attempted.
