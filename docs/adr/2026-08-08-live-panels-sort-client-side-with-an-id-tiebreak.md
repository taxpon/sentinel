---
title: The live panels impose their own total order, tie-broken on id
status: accepted
date: 2026-08-08
type: architecture
areas: [dashboard, analytics]
tasks: [T33]
files:
  - dashboard/src/panels/LiveTable.tsx
  - dashboard/src/panels/Timeline.tsx
specs:
  - docs/07-observability.md
  - docs/03-data-model.md
supersedes:
---

# The live panels impose their own total order, tie-broken on id

## Context

The live table and the timeline both redraw a list every five seconds
([07](../07-observability.md)). Anything that is not a total order therefore shows up as movement on
screen: two rows that compare equal can swap between polls, and a reader watching for progress reads
that as the dashboard glitching rather than as work advancing.

Both lists have a real source of ties.

`remediation_event.created_at` defaults to `now()`, which in Postgres is `transaction_timestamp()` —
the clock is read once per transaction, not once per statement. [06](../06-event-pipeline.md) writes
the state transition, the event row and the enqueued job in a single transaction, so a burst of
events routinely carries one byte-identical timestamp. The index on the table is
`(remediation_id, created_at)` ([03](../03-data-model.md)), which does not distinguish them either.

`remediation.labeled_at` comes from the GitHub webhook, and issues labelled in one pass carry the
same value.

Neither endpoint's ordering is pinned by the spec: `GET /api/remediations` and
`GET /api/remediations/{id}` are described by what they return, not by the order they return it in,
and they are implemented by T25 in parallel with these panels.

## Decision

Each panel sorts what it received, rather than rendering the array in the order it arrived.

**The timeline** is ordered by `created_at` ascending, ties broken by `id` ascending. `id` is a
`bigserial`, so within one transaction it is the insertion order that the timestamp is too coarse to
express.

**The live table** is ordered by lifecycle first — the states `MERGED`, `BLOCKED` and `FAILED` are
terminal ([04](../04-state-machine.md)) and sink below everything still in flight — then by
`labeled_at` descending, ties broken by `id` descending. Work in flight is what the panel exists to
show; finished work stays visible below it, because a system that hides its failures cannot be
evaluated.

Both comparators are exported and tested directly, including the case where the same rows arrive in
a different array order on the next poll.

## Alternatives considered

| Option | Why not |
|---|---|
| Render in the order the API returned | An unspecified order from an endpoint written by another task, against tables whose only relevant index cannot break the ties. It would work in the fixture and reshuffle in the demo |
| Fix the order in the endpoint and have the panel trust it | Better, and worth doing anyway, but it puts the guarantee in a file this task does not own and cannot see. The panel would still be one endpoint change away from silently reshuffling |
| Order the timeline by `id` alone | Correct today, since `bigserial` is monotonic within a table, but it makes the display order an artefact of the sequence. Time first, `id` as the tiebreak, still reads correctly if events are ever backfilled |
| Sort the live table by state, in state-machine order | Groups all the `RUNNING` rows together and buries the one that just moved. Recency is what a reader is scanning for; lifecycle only has to separate "still going" from "done" |

## Consequences

Both panels are stable across polls, and a row moves only when something about it actually changed —
which is exactly the signal the live table is there to give. The live table's ordering is asserted by
a test that transitions a row into `MERGED` and watches it move down.

The cost is that the client sorts a list the server could have sorted, and that the two orderings now
live in the panels rather than in the API. At the volume this dashboard shows — tens of remediations,
tens of events each — the sort is free, and the comparators are unit-testable in a way an `ORDER BY`
reached through an unimplemented endpoint is not.

**What would tell us this was wrong:** the table growing past the few hundred rows where sorting in
the browser stops being free, or a second consumer of `/api/remediations` needing the same order — at
which point the ordering belongs in the endpoint, and these comparators become an assertion about it
rather than the only place it is expressed.
