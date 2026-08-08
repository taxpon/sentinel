---
title: Return a reason from signature verification instead of a boolean or an exception
status: accepted
date: 2026-08-08
type: architecture
areas: [github, api]
tasks: [T10, T22]
files: [src/sentinel/security/hmac.py]
specs: [docs/06-event-pipeline.md]
supersedes:
---

# Return a reason from signature verification instead of a boolean or an exception

## Context

The ingress path has to answer three questions from one verification: which status code to send —
an oversized body is `413` while a missing, malformed or mismatched signature is `401` — what to
record for the delivery, and what to log alongside the source IP and delivery id. A boolean answers
none of them.

The secret and the expected digest are the two values that must never reach a log line, an
exception message or a `repr`, and a rejection is the moment at which code is most tempted to
include them "for debugging".

Untrusted input decides which branch runs: the header comes from whoever made the request, so a
header that is absent, empty, not hex, or not even ASCII is an ordinary rejection and must not
surface as a 500. `hmac.compare_digest` raises `TypeError` on a non-ASCII `str`, so the shape of
the header has to be established before any comparison happens.

## Decision

`verify_signature` returns a `SignatureResult` enum — `OK`, `MISSING_HEADER`, `MALFORMED_HEADER`,
`MISMATCH`, `BODY_TOO_LARGE` — and raises nothing for untrusted input. Every member is a fixed
identifier derived from no part of the request, so the whole result is safe to log and to store as
the delivery's outcome. The size limit is checked first, the header shape second (a full match
against `sha256=` plus 64 hex characters), and only then is the digest computed and compared with
`hmac.compare_digest`.

## Alternatives considered

| Option | Why not |
|---|---|
| Return `bool` | The caller cannot separate `413` from `401`, and the log line loses the reason a delivery was refused |
| Raise a `SignatureError` per failure | A forged request is expected traffic, not an exceptional condition; and an exception carries a message that invites the expected digest into it |
| Return the expected digest for the caller to compare | Moves the constant-time comparison out of the one place that can guarantee it, and hands the caller a secret-derived value to log |
| Let the caller check the body size | The spec ties the limit to hashing order; a caller that forgets it hashes 5 MB of attacker-supplied data |

## Consequences

The caller maps results to status codes in one obvious `match`, and the failure reason is legible
in logs without any redaction step, because no branch ever holds the secret or the digest in a
value that leaves the function. The cost is an enum the caller must handle exhaustively rather than
an `if`, and one more name to keep in step with the spec if the policy gains a case.

**What would tell us this was wrong:** if the caller ended up collapsing every non-`OK` result back
into a single branch, meaning the distinctions were never used.
