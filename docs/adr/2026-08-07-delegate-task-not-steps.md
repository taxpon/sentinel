---
title: Give Devin the objective and constraints, not the steps
status: accepted
date: 2026-08-07
type: architecture
areas: [devin]
tasks: [T15]
files: [src/sentinel/devin/playbooks.py]
specs: [docs/05-devin-integration.md]
supersedes:
---

# Give Devin the objective and constraints, not the steps

## Context

Prompts could be written anywhere on a spectrum from "here is the issue, fix it" to a prescribed
sequence of file edits and commands. Standing context that applies to a whole class of work has two
possible homes: the per-issue prompt, or a playbook and knowledge notes.

## Decision

The prompt carries the issue, the objective, the definition of done and the constraints — nothing
about *how* to investigate or implement. Class-level standing instructions live in playbooks;
repository facts, such as how to run the test suite, live in knowledge notes. Resume messages follow
the same rule: state the new fact and restate the goal.

## Alternatives considered

| Option | Why not |
|---|---|
| Prescribe the steps | Caps the outcome at what we already knew, and the agent cannot adapt when reality differs from our assumption. It also makes the prompt brittle across issue classes |
| Put everything in the prompt, skip playbooks and knowledge | Long prompts repeated per issue, with class-level guidance drifting between them and no single place to correct it |

## Consequences

Prompts stay short and issue-specific, and improving a whole class of work means editing one
playbook. The agent is free to find an approach we would not have specified. In exchange, outcomes
vary more between runs, which is why `structured_output` requires a root cause and a regression test
rather than trusting the result.

**What would tell us this was wrong:** a class of issue where the agent reliably takes a wrong
approach that a constraint in the playbook — not a step in the prompt — could prevent.
