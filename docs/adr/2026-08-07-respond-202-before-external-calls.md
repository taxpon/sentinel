---
title: Acknowledge webhooks with 202 before making any external call
status: accepted
date: 2026-08-07
type: architecture
areas: [api]
tasks: [T22]
files: [src/sentinel/api/webhooks.py]
specs: [docs/06-event-pipeline.md]
supersedes:
---

# Acknowledge webhooks with 202 before making any external call

## Context

GitHub abandons a webhook delivery that does not respond within roughly ten seconds. Creating a
Devin session is far slower than that and can be rate-limited or retried with backoff. A timed-out
delivery is redelivered, which without care produces duplicate sessions.

## Decision

The webhook request path does exactly three things: verify the signature, write the delivery,
remediation and job rows in one transaction, and return `202 Accepted`. Every call to Devin or
GitHub happens later, in the worker.

## Alternatives considered

| Option | Why not |
|---|---|
| Create the Devin session inline | Exceeds the delivery timeout, and couples GitHub's view of our health to Devin's latency |
| Respond immediately and process in a background task in the API process | Work is lost if the process restarts between the response and the task running |

## Consequences

The endpoint is fast and its failure modes are limited to signature and database errors. The trade
is one more moving part — the worker — and the fact that "accepted" no longer means "done", so
progress must be observed through the state machine rather than the HTTP response.

**What would tell us this was wrong:** if we ever needed to return a synchronous result to the
caller, which webhooks by nature do not.
