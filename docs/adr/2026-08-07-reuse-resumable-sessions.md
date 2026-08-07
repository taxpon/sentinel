---
title: Resume the existing Devin session on CI failure rather than starting a new one
status: accepted
date: 2026-08-07
type: architecture
areas: [pipeline, devin]
tasks: [T23]
files: [src/sentinel/pipeline/handlers.py]
specs: [docs/04-state-machine.md]
supersedes:
---

# Resume the existing Devin session on CI failure rather than starting a new one

## Context

A first attempt often fails CI or draws a change request. Something has to carry the feedback back
to the agent. Devin sessions can be created with `resumable: true` and later re-engaged with
`POST /v3/organizations/{org}/sessions/{id}/messages`.

## Decision

Feed CI failure logs and reviewer comments into the **existing** session and increment a `cycle`
counter, tagging the session `cycle:N`. A new session is never created for the same issue.

## Alternatives considered

| Option | Why not |
|---|---|
| Create a fresh session per attempt | The new session has no memory of why the first change was made, so it re-derives context already paid for, and cost per fix rises with every retry |
| Hand failures to a human immediately | Discards the capability that makes the system interesting, and the review-fix loop is the behaviour most worth demonstrating |

## Consequences

The agent retains the reasoning behind its own change, which makes the second attempt cheaper and
better informed. Cycle count becomes a meaningful autonomy metric — how much self-correction each
fix required — which would be unmeasurable if each attempt were a separate session. The risk is a
session looping on a failure it cannot resolve, bounded by `MAX_FIX_CYCLES`.

**What would tell us this was wrong:** resumed sessions performing worse than fresh ones, or long
sessions degrading as context accumulates.
