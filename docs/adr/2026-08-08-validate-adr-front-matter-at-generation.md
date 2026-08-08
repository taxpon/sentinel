---
title: Validate ADR front matter at generation time and refuse to write a partial index
status: accepted
date: 2026-08-08
type: process
areas: [ops]
tasks: [T07]
files: [scripts/gen_adr_index.py, docs/adr/template.md]
specs: [docs/implementation-plan.md]
supersedes:
---

# Validate ADR front matter at generation time and refuse to write a partial index

## Context

`docs/adr/index.md` and `.claude/rules/adr-pointers.md` are generated from the YAML front matter of
the records, and `make adr-check` is a CI gate on that output. Every parallel session writes records
into the same directory, from the same template, without reading each other's.

The front matter is not free text. `areas` draws on the `areas` list in
[`docs/tasks.yaml`](../tasks.yaml), `tasks` on the ids in the same file, `date` is what the by-date
table sorts on, and `supersedes` names another record by slug.

The generator checked for the presence of four fields and nothing else. Everything else was
accepted and rendered:

- `area:` written for `areas:` dropped the record from the by-area table, silently;
- `areas: ops` — a string, not a list — iterated into the areas `o`, `p` and `s`;
- an area or task id that exists nowhere created a section or a row for itself;
- `date: yesterday` sorted to the top of the by-date table as a string;
- `supersedes` was never read at all, so a record it named did not have to exist, and the record it
  replaced stayed in the index as an accepted decision next to the one that overturned it.

Malformed YAML, front matter that was never closed, and front matter that parsed to something other
than a mapping each ended in a traceback from inside `yaml` or from an unpacking assignment.

An index that is quietly wrong is worse than one that fails to build: `make adr-check` passes on it,
and the next session reads it as the record of what has been decided.

## Decision

The generator validates every record before it writes anything. Fields are checked against a closed
set of names, `status` and `type` against their vocabularies, `date` against being a real date that
agrees with the filename, `areas` and `tasks` against `docs/tasks.yaml`, and `supersedes` against
the other records — which must exist, must not be the record itself, must be claimed by only one
successor, and must already say `status: superseded`.

Problems are collected across the whole directory and reported together, each prefixed with the file
it came from, and nothing is written when there is even one. A missing `docs/adr/` or
`docs/tasks.yaml` is reported as "run this from the repository root" rather than silently producing
an index with no records in it.

Because a supersession is now guaranteed to be consistent, it is also rendered: the superseded row
in the by-date table carries a link to the record that replaced it, and every other listing marks a
record that is not `accepted` with its status.

## Alternatives considered

| Option | Why not |
|---|---|
| Warn and carry on | The output is a CI gate. A warning inside `make adr-index` is read by nobody, and the index it produced still passes `make adr-check`. |
| Validate in a separate linter | Two commands to run and one more to forget. The generator already parses every record; the check costs nothing there. |
| Fail on the first problem | With records arriving from parallel sessions, one run per problem is one round trip per problem. |
| Check that `files:` entries exist | Records are written before the code they constrain — eight of the current records name a file no task has created yet. The check would fail on correct records. |

## Consequences

A typo in front matter now stops `make adr-index` and CI instead of producing a plausible index, and
the message names the file and the vocabulary the value should have come from. `areas` becomes
required, which is what puts every record in the by-area listing.

The vocabulary lives in `docs/tasks.yaml`, so a record naming a task or an area that has not been
added there yet cannot be generated. That coupling is deliberate — the alternative is an index whose
sections are invented by typos — but it means a new area is added to `docs/tasks.yaml` first.

**What would tell us this was wrong:** sessions working around the validation rather than with it —
records with `areas: [ops]` chosen because it passes, tasks omitted to avoid the check, or the
generator's exclusion list growing. That would mean the schema is describing something other than
what the records need to say.
