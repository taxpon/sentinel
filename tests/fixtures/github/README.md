# Recorded GitHub payloads

One file per row of the subscribed-events table in
[`docs/06-event-pipeline.md`](../../../docs/06-event-pipeline.md). The file name is
`<event>.<action>[.<qualifier>].json`, and the first segment is the `X-GitHub-Event` header the
`delivery` fixture sends.

Load them with `factories.github_payload("issues.labeled")`, or as a signed request with the
`delivery` fixture. Add to these rather than hand-writing a partial payload in a test.

## Provenance

There is no webhook on `taxpon/superset` yet, so these are not captured deliveries. Every object
inside them is a real API response, trimmed to a field allowlist and reassembled into the envelope
GitHub documents for that event.

| Part | Where it came from |
|---|---|
| `repository` | `GET /repos/taxpon/superset` — the fork itself, unmodified except for trimming |
| `issue` | Issue 42876 of `apache/superset` |
| `pull_request` | Pull request 42889 of `apache/superset`; the `opened` copy has the closed/merged fields put back to what they were before the merge |
| `review` | The `CHANGES_REQUESTED` review on pull request 42865 of `apache/superset` |
| `check_suite` | A `success` suite on 42889's head, and a `failure` suite from 42534 |
| `comment` | A comment on issue 42876, with a body written for the mention path |

Two things were constructed rather than recorded, because no API returns them:

- **The `devin:autofix` label.** The label does not exist on the fork yet, so the object carries a
  real label's shape with the name, colour and description the bootstrap script will create.
- **`check_suite.pull_requests[]`.** The REST representation leaves it empty for a pull request from
  a fork; the webhook does not. The entry was built from the pull request in these fixtures.

Three edits were made for coherence, so that the whole directory describes one remediation —
issue 42876 labelled, pull request 42889 opened, CI failing, changes requested, CI passing, merged:

- every reference to `apache/superset` was rewritten to `taxpon/superset`, which is `TARGET_REPO`;
- the review's links and `commit_id` were re-pointed at pull request 42889;
- the failing check suite's head was re-pointed at 42889's, so that the failure and the success
  describe the same commit. Its `conclusion`, `app`, timings and check-run count are as recorded.

Everything else — ids, node ids, logins, timestamps, bodies, `author_association`, the null and
empty-list fields — is as GitHub returned it.
