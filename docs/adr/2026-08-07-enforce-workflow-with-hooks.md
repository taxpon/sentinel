---
title: Enforce the pre-PR review step with a hook, not with CLAUDE.md alone
status: accepted
date: 2026-08-07
type: process
areas: [ops]
tasks: [T06]
files: [.claude/settings.json]
specs: [docs/implementation-plan.md]
supersedes:
---

# Enforce the pre-PR review step with a hook, not with CLAUDE.md alone

## Context

Several rules must hold on every pull request, including running the PR review toolkit and resolving
its findings first. Claude Code documentation is explicit that CLAUDE.md is *context, not enforced
configuration*, and recommends hooks for anything that must happen at a fixed point in the
lifecycle. With multiple parallel sessions, a rule that is followed most of the time is a rule that
will be missed.

## Decision

Layer the rules by hardness. CLAUDE.md and `.claude/rules/` state them; a `PreToolUse` hook enforces
the pre-PR review by denying `gh pr create` unless `.sentinel-review/<branch>.ok` exists **and**
records the current `HEAD` SHA. Pushing further commits invalidates the marker automatically. A
`SessionStart` hook supplies the live task state that no static file can.

## Alternatives considered

| Option | Why not |
|---|---|
| CLAUDE.md and a PR template only | No way to detect that the step was skipped; the failure is silent |
| CI check after the PR exists | Reliable, but it cannot enforce "before creating the PR", and it turns a skipped step into rework |
| Marker without a SHA | A single review would authorise every later commit on the branch, which is exactly the case worth catching |

## Consequences

The step cannot be skipped by accident, and a human can still bypass it deliberately by writing the
marker — visible rather than silent. The dependency is that `pr-review-toolkit` is a user-level
plugin, absent on a machine that has not installed it; the hook denies with an explanatory message
in that case rather than failing open.

**What would tell us this was wrong:** the hook blocking legitimate work often enough that people
route around it.
