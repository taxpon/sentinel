---
title: <The decision, in one line, in the imperative>
status: accepted          # accepted | superseded | deprecated
date: YYYY-MM-DD          # must match the date in the filename
type: architecture        # architecture | process; an architecture one also gets a row in docs/02-architecture.md
areas: []                 # required; vocabulary from docs/tasks.yaml -> areas
tasks: []                 # e.g. [T13, T23]; ids from docs/tasks.yaml
files: []                 # source files this decision constrains
specs: []                 # e.g. [docs/06-event-pipeline.md]
supersedes:               # slug of the record this replaces; set that record's status to superseded
---

<!-- `make adr-index` validates these fields and refuses to write the index if any record fails. -->

# <The decision, in one line>

## Context

The constraint or question that forced a choice. Facts only — what was true at the time, what the
alternatives had to satisfy. No justification here.

## Decision

What was chosen, stated plainly.

## Alternatives considered

| Option | Why not |
|---|---|
| … | … |

## Consequences

What this makes easier, what cost it accepts, and — required — **what would tell us this was
wrong**. The last one is what makes the record actionable later instead of merely historical.
