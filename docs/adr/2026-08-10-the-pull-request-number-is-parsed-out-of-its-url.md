---
title: The pull request number is parsed out of the URL Devin reports
status: accepted
date: 2026-08-10
type: architecture
areas: [devin, pipeline]
tasks: [T11, T24]
files: [src/sentinel/devin/schemas.py, src/sentinel/pipeline/poller.py]
specs: [docs/05-devin-integration.md, docs/06-event-pipeline.md]
supersedes:
---

# The pull request number is parsed out of the URL Devin reports

## Context

`GET /v3/organizations/{org_id}/sessions` was called against the live API on 2026-08-10, the first
time any session shape had been seen for real ([B8](../blockers.md#b8)). Two facts about
`pull_requests[]` came out of it, and neither matched the code:

- the URL field is **`pr_url`**, not `url`. `PullRequest` required `url`, so the body failed to
  parse and every session holding a pull request was unreadable;
- there is **no number**, in the entry or anywhere else in the session body. `PullRequest.number`
  was `int | None = None` and had never been supplied by anything.

The second is the one that matters. `remediation.pr_number` is how `webhooks._criterion` resolves a
`check_suite` or a `pull_request_review` delivery to a remediation — `remediation` is keyed
`(repo, issue_number)`, a pull request delivery carries no issue number, and the poller is the only
thing that can link the two ([the poller links the pull
request](./2026-08-08-the-poller-links-the-pull-request.md)). A null `pr_number` therefore resolves
every CI failure and every review to no remediation at all. Each is recorded as ignored, the
remediation sits in `PR_OPENED` looking healthy, and the review-fix loop — the behaviour the system
exists to demonstrate — never engages. Nothing raises.

The number is available: a GitHub pull request URL is `https://…/{owner}/{repo}/pull/{number}`, and
Sentinel already holds `pr_url`.

The parse can fail. A URL with no `/pull/{n}` segment yields nothing, and no number may be invented
for it — a wrong number would attach another pull request's check suites to this remediation.

## Decision

`PullRequest` takes `pr_url` and derives `number` from it, as a **property** on the schema rather
than a validated field, using a `/pull/(\d+)` search that does not constrain the host.

The schema, because every reader of `pull_requests[]` needs the number and the poller is only the
first — a backfill over `list_sessions` is the second — and because the absence of a number is a
fact about the Devin response, which is what this module exists to state.

A property, because a URL it cannot read must fail one remediation rather than the parse of the
whole session: `DevinResponseError` sends the remediation to `SESSION_UNREADABLE` and then `FAILED`,
which would discard a pull request that really exists over the shape of the link to it.

**`pr_url` gets no `url` alias.** `_unwrap` tolerates several list envelopes in the same module, and
that tolerance is why the envelope was not a second bug on the same call — but it stands in for a
fact nobody had, because the specification names no envelope. Here the fact exists.

When the derivation fails, the link is still written and `pr_number` stays null, and both the log
(`poller.pull_request.unnumbered`) and the remediation's own `remediation_event.detail`
(`pr_number_unresolved`, carrying the URL) say so. The event is read off the row — linked, and
unnumbered — so it is reported for as long as the condition holds rather than only on the tick that
linked it.

## Alternatives considered

| Option | Why not |
|---|---|
| Parse in the poller | The poller is one caller of `pull_requests[]`, not the only one, and the next caller would either repeat the rule or quietly do without it. It also leaves `PullRequest.number` sitting on the schema as a field nothing ever populates — which is precisely the defect being fixed |
| A validated field with a URL validator | A `pr_url` shaped unexpectedly would fail the whole session parse, and the poller's answer to an unparseable session is `SESSION_UNREADABLE → FAILED`. That escalates a remediation holding a real pull request because of the format of a link |
| `GET /repos/{owner}/{repo}/pulls?head=…` to ask GitHub | A network call, a rate-limit budget and a failure mode, to recover a number that is already in a string we hold. It also needs a branch name the session does not report |
| Accept `url` alongside `pr_url` | The name is now known. An alias buys nothing today and hides a rename tomorrow: the body would keep parsing under a name the API had stopped sending, and nobody would learn it had moved. A rename should fail the parse, loudly, on the first call — which is what happened here and is why it was found |
| Match `github.com` in the pattern | A GitHub Enterprise install spells `/pull/{n}` the same way. Pinning the host would reject a URL that is perfectly readable, for no gain — the path segment is what identifies a pull request |
| Log the failed derivation and nothing more | A log line is found only by someone who already suspects this. The remediation that will stall is the thing an operator opens, and its timeline is where the reason belongs |
| Escalate the remediation when the number cannot be derived | The pull request exists and is the deliverable; a human can still review and merge it. Failing it would discard finished work over a link format, which is the same bargain [the poller already refuses](./2026-08-08-a-session-with-nothing-to-show-fails.md) to make for a remediation holding a pull request |

## Consequences

The review-fix loop can engage at all — with `pr_number` null it could not, which no test caught
because every fake supplied a number the API does not send. The regression fixture in
`tests/test_devin_client.py` now carries the observed body verbatim, so a fake that drifts back to
the wished-for shape fails the suite instead of hiding it.

Sentinel now depends on GitHub's URL format in a second place. It is stable, and the cost of it
changing is one regex, but it is a coupling that did not exist before.

A pull request whose URL carries no number is linked and unroutable: CI failures and reviews on it
resolve to nothing, and the remediation stays in `PR_OPENED` until someone reads the event. That is
a visible stall rather than a silent one, which is the whole of the improvement — it is not a fix.

**What would tell us this was wrong:** Devin adding a number to `pull_requests[]`, which would make
the derivation dead code to delete rather than a fallback to keep. Equally, `pr_number_unresolved`
appearing at all in a real run: it would mean `pr_url` carries something other than a pull request
URL, and the right answer then is to find out what, not to widen the pattern until it matches.
