---
title: Absorb the two predictable rebase conflicts instead of engineering them away
status: accepted
date: 2026-08-08
type: process
areas: [ops]
tasks: [T06]
files: [.claude/commands/finish-task.md]
specs: [docs/implementation-plan.md]
supersedes:
---

# Absorb the two predictable rebase conflicts instead of engineering them away

## Context

Sixteen pull requests have merged with several branches open at a time. Two files conflict
repeatedly, and neither is a case of two tasks editing what they should not:

- `docs/adr/index.md` and `.claude/rules/adr-pointers.md` are generated from the ADR front matter.
  Any merged pull request that adds a record changes them, so every open branch that also added one
  conflicts. Resolution is mechanical, and it has happened on nearly every pull request.
- `docs/02-architecture.md` conflicts when two parallel tasks each add a record of
  `type: architecture`, because both append a row at the end of the same **Design decisions** table.
  Unlike the index this is a genuine edit by another task, with 40–80 words of hand-written
  rationale in the row.

The rule that produces the second conflict is enforced by a test in `tests/test_gen_adr_index.py`
and has caught three tasks on rebase.

## Decision

Keep both files tracked and keep both conflicts. Document the resolution as a step of
`/finish-task`, placed before the review so the review marker still covers the final commit: the
generated files are re-derived with `make adr-index`, which overwrites the conflict markers
wholesale, and the architecture table is resolved by keeping both rows.

## Alternatives considered

| Option | Why not |
|---|---|
| Stop tracking `docs/adr/index.md` and generate it on demand | `make adr-check` is CI's staleness guard and the in-repo links point at the file; a session looking for prior decisions reads it. That trades a ten-second regeneration for "the decisions are not readable in the repository" |
| A `.gitattributes` merge driver for the generated files | A custom driver needs `git config merge.<name>.driver` in every clone, and where it is absent git silently falls back to the ordinary conflict. Enforcement that only works on machines that ran a setup step is exactly what [enforcing the workflow with hooks](./2026-08-07-enforce-workflow-with-hooks.md) was written to avoid |
| `merge=union`, which needs no configuration | It produces a plausible-looking index with duplicated rows rather than a conflict. A wrong file that looks right is worse than a conflict that stops you |
| Generate the Design decisions table from ADR front matter too | Front matter holds a title; the rationale column is prose written for that table. Generating it would delete the content that makes the table worth reading, and would convert a content conflict into the generated-file conflict above |
| Drop the rule that an architecture record gets a row | The table went stale within days of being written, which is why the test exists |

## Consequences

Every branch open across a merge pays a rebase step it cannot avoid, and the cost is bounded and
named in advance rather than improvised per session. The generated files can be resolved without
reading them; `docs/02-architecture.md` cannot, and the instruction says so.

**What would tell us this was wrong:** a session resolving `docs/02-architecture.md` by dropping
another task's row — a mis-resolution rather than a nuisance — or enough branches open at once that
rebasing costs more than the index is worth, at which point untracking the generated index becomes
the cheaper trade.
