---
title: The webhook is updated in place on every run, because its secret cannot be read back
status: accepted
date: 2026-08-08
type: process
areas: [github, ops]
tasks: [T41]
files: [scripts/bootstrap_github.py]
specs: [docs/09-operations.md, docs/06-event-pipeline.md]
supersedes:
---

# The webhook is updated in place on every run, because its secret cannot be read back

## Context

`scripts/bootstrap_github.py` is re-run routinely: a free `cloudflared` tunnel hands out a different
URL every time it restarts ([B9](../blockers.md)), and moving the hook is the ordinary reason to run
the script at all.

Everything else the script reconciles can be compared before it is written. A label's colour comes
back from the API, so a matching label is left untouched. The hook's `config.secret` does not:
`GET /repos/{repo}/hooks` returns `"secret": "********"`. Whether the registered hook holds the
value currently in `GITHUB_WEBHOOK_SECRET` is not an observable fact.

It matters because of how the mismatch presents. A hook signing with a stale secret delivers
normally and Sentinel rejects every delivery with `401`
(`docs/09-operations.md#troubleshooting`) — the failure is visible only in the repository's delivery
log, and looks identical to a body-rewriting proxy.

The hook is identified by the path it delivers to, `/webhooks/github`, rather than by its full URL:
the host is the thing that rotates.

## Decision

When a hook delivering to `/webhooks/github` already exists, the script `PATCH`es it with the full
desired configuration — URL, events, `content_type`, `insecure_ssl`, `active` and the secret — every
run, whether or not anything observable differs. The report distinguishes the two cases
("`updated url -> …`" versus "`already current …; secret re-applied`"), but the request is the same.

A second hook on the same path is reported and left alone. Nothing is deleted, and no hook is
created while one exists.

## Alternatives considered

| Option | Why not |
|---|---|
| Write only when an observable field differs | Rotating `GITHUB_WEBHOOK_SECRET` would then silently not take effect, on the run whose whole purpose was to apply it |
| A `--rotate-secret` flag, writing only when asked | The operator has to know that the secret is the one field a normal run does not reconcile. Every run that forgets it produces `401`s that look like a proxy problem |
| Delete the hook and register a new one | Delete-then-recreate makes a re-run destructive. Run against a repository somebody else configured, or interrupted between the two calls, it leaves no webhook at all |
| Store the secret's digest somewhere and compare | State about the fork kept outside the fork, to avoid an idempotent write |

## Consequences

Every run makes one `PATCH` it may not need. It creates nothing, changes nothing when the values
match, and costs one request — idempotence is a property of the resulting state, not a promise to
make no requests.

The secret is written and never read, which is also why it is never reported: there is nothing to
compare, so there is nothing a diff line could print. The dry run reports the *intent* to register
or update a hook and never renders a request body, and the script scrubs every configured credential
out of its own error text — GitHub quotes a rejected payload back in the `errors` of a `422`, and the
payload most likely to be rejected here is the one carrying the secret.

**What would tell us this was wrong:** the hooks API starting to expose something comparable — a
digest, or a `secret_updated_at` — at which point the write becomes conditional like every other
step. Or an operator finding the unconditional `PATCH` in the repository's audit log misleading,
which would be an argument for reporting it more loudly, not for skipping it.
