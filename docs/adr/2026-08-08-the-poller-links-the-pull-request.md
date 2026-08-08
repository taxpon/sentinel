---
title: The poller links the pull request; pull_request.opened is recorded and dropped
status: accepted
date: 2026-08-08
type: architecture
areas: [github, pipeline, api]
tasks: [T20, T22, T24]
files: [src/sentinel/github/events.py]
specs: [docs/06-event-pipeline.md, docs/04-state-machine.md, docs/07-observability.md]
supersedes:
---

# The poller links the pull request; `pull_request.opened` is recorded and dropped

## Context

[04](../04-state-machine.md#transitions) gives `RUNNING → PR_OPENED` two sources: "`pull_request.opened`
from Devin's bot, **or** `pull_requests[]` on the session". Implementing the first half meant
finding, from the webhook payload alone, which remediation the pull request belongs to. It cannot be
done:

- `remediation` is unique on `(repo, issue_number)`, and `remediation.pr_number` / `pr_url` are
  **null until `PR_OPENED` has been applied** (`docs/03-data-model.md`). `PR_OPENED` is the
  transition that populates them, so at the moment this delivery arrives there is no key to look it
  up by. Every *later* pull request event — `closed`, a review, a check suite — resolves fine by
  `pr_number`, precisely because the link already exists by then.
- A `pull_request` payload carries no issue number. [05](../05-devin-integration.md) asks the session
  for "a pull request against `{base_branch}` whose description explains the root cause" and does not
  require it to reference the issue, so parsing a closing keyword out of the body would rest on a
  convention nothing establishes. The recorded payload in `tests/fixtures/github/` — this project's
  own record of what the pull request for a remediation looks like — opens with "Fixes Sentry
  SUPERSET-PYTHON-WE6", a Sentry link and no `#`.
- The head branch name is Devin's to choose and is not derived from the issue.
- Filtering on the author does not help either: it narrows which deliveries are considered, not which
  remediation they belong to.

The poller has none of this trouble. It reads `pull_requests[]` from the session it already knows the
id of ([ADR](./2026-08-07-poller-drives-state-machine.md)), so attribution is by construction rather
than by inference.

## Decision

`pull_request.opened` maps to `Intent.IGNORED` with `Reason.LINKED_BY_THE_POLLER`. The delivery is
stored in `webhook_delivery` like any other, with `handler_result = ignored` and that reason; nothing
is looked up and no trigger is applied. `RUNNING → PR_OPENED` is driven by the poller alone.

The reason is a member of the vocabulary rather than a silent fall-through, so an operator reading
`webhook_delivery` sees *why* the pull request was ignored and does not go looking for a bug.

## Alternatives considered

| Option | Why not |
|---|---|
| Parse a closing keyword (`Fixes #42876`) out of the body | Would work only when Devin happens to write one, which nothing requires and the recorded example does not do — and the naive form of the regex matches "Fixes Sentry …" in that very payload. A fast path that fires on a convention the system does not enforce is a fast path that fires unpredictably |
| Add the closing keyword to the session prompt, then parse it | Defensible, and it changes `docs/05` and T11's prompt-building code, neither of which this task owns. Worth proposing on its own merits: it would also make GitHub close the issue on merge. It is not a prerequisite for anything today |
| Match on the head branch | Devin names its own branch; no rule ties it to the issue number |
| Return the intent anyway, with no key, and let T22 no-op | The API would then promise something it cannot deliver, and the no-op would be invisible — the delivery would be recorded as `enqueued` while nothing was enqueued |
| Filter on the author being Devin's bot and keep the intent | Narrows *which* deliveries are considered but still supplies no key. It also names a service-account login that is configured nowhere, so a deployment authenticating Devin as anything else would silently lose every pull request |

## Consequences

The link is established by exactly one mechanism instead of two racing ones, which suits a column
that [04](../04-state-machine.md#the-review-fix-loop) makes write-once: `pr_opened_at` is stamped
once, by the component with unambiguous attribution, and `PullRequestCondition.UNLINKED` never has to
arbitrate between a webhook and a poll.

The cost is latency. `pr_opened_at` is now stamped up to `POLL_INTERVAL_SECONDS` (default 20 s) after
the pull request actually opened, so time-to-PR in [07](../07-observability.md) carries that much
error. On a measure whose interesting values are minutes, that is acceptable; it is not free, and it
is the number to check first if time-to-PR ever looks suspiciously uniform.

The second cost is not latency, and it is not yet paid off. A `check_suite.requested` arriving before
the poller has linked the pull request is lost: the lookup finds no remediation, and `trigger_for`
absorbs it. That the *link* comes first is by design — `Trigger.CHECK_SUITE_REQUESTED` requires a
linked pull request, so the state machine already ordered them. What follows from the loss is not.

The remediation is left in `PR_OPENED`, and as the transition table stands today **neither
`check_suite.completed` trigger is legal from `PR_OPENED`**: both list `CI_RUNNING, RUNNING`, so the
completion that arrives a minute later raises `IllegalTransitionError` rather than moving it on. Lap
one is stranded. The re-entry from `RUNNING` that
[ADR](./2026-08-08-ci-states-re-entered-from-running.md) provides does not cover this — it is about
lap two onward, once a resume has already returned the remediation to `RUNNING`, whereas lap one
sits in `PR_OPENED` precisely because linking is what put it there. And with a poll interval of 20
seconds against the gap between a pull request opening and its first check suite, losing the
`requested` is the ordinary ordering, not an unlucky one.

The fix is one line in `src/sentinel/pipeline/state.py` — `PR_OPENED` added to the sources of both
`completed` triggers — which is T14's file and is **in flight with T14 as of 2026-08-08**. Until it
lands, this decision leaves lap one dependent on the `requested` webhook winning a race it usually
loses. Recorded here rather than worked around in the mapping, because widening the transition table
is the correct fix and belongs to the module that owns it.

**What would tell us this was wrong:** remediations sitting in `PR_OPENED` with a green pull request
— the symptom of the above, if the state machine change does not land. Beyond that: time-to-PR
mattering at second resolution, or the session prompt gaining a required "Closes #N" line, either of
which would make the webhook path both feasible and worth having, and this record should then be
superseded rather than quietly worked around.
