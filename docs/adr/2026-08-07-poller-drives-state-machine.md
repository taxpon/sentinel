---
title: Drive the remediation state machine from a poller, not from Devin callbacks
status: accepted
date: 2026-08-07
type: architecture
areas: [pipeline]
tasks: [T24]
files: [src/sentinel/pipeline/poller.py]
specs: [docs/02-architecture.md, docs/04-state-machine.md]
supersedes:
---

# Drive the remediation state machine from a poller, not from Devin callbacks

## Context

The pipeline needs to know when a Devin session starts working, opens a pull request, finishes, or
errors. GitHub pushes its events to us over webhooks. Devin does not: the v3 API reference documents
no callback, subscription or outbound webhook for session status changes. Devin's own webhook
material describes the opposite direction — an external system calling Devin to start a session.

## Decision

Run a dedicated `poller` process that periodically calls
`GET /v3/organizations/{org}/sessions/{id}` for every in-flight remediation and reconciles status,
`acus_consumed`, `structured_output` and `pull_requests[]` into the database. Reconciliation is
idempotent, so a repeated observation is harmless.

## Alternatives considered

| Option | Why not |
|---|---|
| Wait for Devin outbound webhooks | They do not exist. Not an option today |
| Infer session progress only from GitHub events | Only observes side effects. A session that errors, stalls in `waiting_for_user`, or hits its ACU cap produces no GitHub event at all — exactly the failures worth surfacing |
| Poll on demand when a dashboard request arrives | Dashboard freshness would depend on someone looking at it, and no state would advance while nobody watched |

## Consequences

Session state is observable at a bounded lag of `POLL_INTERVAL_SECONDS` rather than in real time,
and that lag also bounds dashboard freshness. In exchange the pipeline becomes self-healing: if a
GitHub webhook is missed entirely, the poller still observes the session reaching `exit` and the
pull request appearing on the session, so the remediation is not stranded.

**What would tell us this was wrong:** Devin ships outbound session webhooks, or polling cost or
rate limits become material at higher concurrency. Either would justify revisiting.
