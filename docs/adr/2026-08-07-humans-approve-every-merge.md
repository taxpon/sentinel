---
title: Require human approval for every merge
status: accepted
date: 2026-08-07
type: process
areas: [remediation]
tasks: [T23]
files: []
specs: [docs/01-overview.md, docs/04-state-machine.md]
supersedes:
---

# Require human approval for every merge

## Context

The system can take an issue to a green pull request without human involvement. Whether it should
also merge is a separate question. These are real changes to a real codebase, including security
fixes.

## Decision

Sentinel never merges. It opens the pull request, posts the root-cause summary, and stops. A human
approves and merges.

## Alternatives considered

| Option | Why not |
|---|---|
| Auto-merge when CI is green | Scoped CI on the fork is a deliberately narrowed signal, so green does not mean safe. It would also remove the very measurement the project is built to produce |
| Auto-merge only for low-risk classes | The risk classification is self-reported by the agent, which makes it the wrong thing to gate on |

## Consequences

The stated goal — moving the bottleneck from implementation capacity to review capacity — stays
honest, and review latency becomes a first-class metric rather than a hidden cost. Throughput is
capped by reviewer availability, which is the intended finding rather than a defect.

**What would tell us this was wrong:** review consistently rubber-stamping without findings across a
large sample, which would suggest the gate has become ceremony.
