---
title: Among several webhooks, the one already at the target URL is the one kept
status: accepted
date: 2026-08-09
type: process
areas: [github, ops]
tasks: [T41]
files: [scripts/bootstrap_github.py]
specs: [docs/09-operations.md, docs/06-event-pipeline.md]
supersedes:
---

# Among several webhooks, the one already at the target URL is the one kept

## Context

`scripts/bootstrap_github.py` recognises the hooks it owns by the path they deliver to,
`/webhooks/github`, because the host is a rotating tunnel. It updates one in place and never
deletes, so when it finds several it has to choose which one is canonical.

Several is the expected state, not an exotic one. `docs/09-operations.md#3-webhook` tells the
operator to run `gh api -X POST …/hooks`, which creates unconditionally, and the line below it says
the tunnel URL changes on every restart. Anyone who followed the document across two restarts has
two or three hooks, all on the receiver path, all but one behind a dead tunnel.

The first implementation took `hooks[0]` — GitHub's order, so the oldest — regardless of where it
pointed. Against the ordinary case of *one dead hook and one live hook*, that repoints the dead one
at the tunnel the live one is already using. Both then deliver. The script did not find a
duplicate-delivery condition; it created one, reported the wrong hook as the stale one, and exited
`0`.

Nothing downstream absorbs it. Each hook carries its own `X-GitHub-Delivery`, so delivery-level
deduplication sees two distinct deliveries. Only the domain layer's `remediation (repo,
issue_number)` uniqueness absorbs a doubled *start* (`docs/06-event-pipeline.md`); the resume paths
— `check_suite.completed`, `pull_request_review.submitted`, `issue_comment.created` — have no such
key. Two `resume_session` jobs per CI failure message the same Devin session twice, spend the ACUs
twice, and advance `cycle` twice against `MAX_FIX_CYCLES`.

## Decision

The canonical hook is the one whose `config.url` already equals the target URL, if there is one;
otherwise the oldest. Every other hook on the receiver path is named in a note on stderr, as the
one to remove, and left in place. Where a run is given no URL there is nothing to prefer by,
because nothing is being moved, and the oldest is reconciled where it stands.

The note says which hook is being kept, and says explicitly that events arrive twice when another
hook is already on the same URL.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep `hooks[0]` and note the rest | The bug above. Creating the duplicate-delivery condition is worse than finding it, and it happens on the most likely starting state |
| Delete every hook but one | The rule this script is built on. A delete that runs twice against a repository somebody else configured is unrecoverable, and the trigger for it here would be a heuristic |
| Refuse to act while several exist | Safe and useless: the operator most likely to hit this is the one who followed the documented `POST` and cannot get past it without manual work |
| Deactivate the others instead of deleting | Reversible, but still a write to a hook that may not be ours in intent, and it hides the mess rather than reporting it |

## Consequences

A repository accumulating hooks stays visible rather than silently fixed: the notes repeat on every
run until someone removes them, which is the intended pressure. Delivery is never doubled *by this
script*, and a run that finds it already doubled says so in those words.

The oldest-hook fallback still applies when none is at the target — that is a genuine move, and any
choice is arbitrary, so the deterministic one is used and the other reported.

**What would tell us this was wrong:** an operator wanting the duplicates cleaned up automatically,
often enough that the notes are noise. The answer then is an explicit `--prune` flag, not a change
to what a default run does.
