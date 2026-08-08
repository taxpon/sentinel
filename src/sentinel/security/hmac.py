"""Verification of GitHub's `X-Hub-Signature-256` header.

A pure function over the raw request body, so it can be tested directly against known vectors
(docs/06-event-pipeline.md#signature-verification). It never logs, never raises for untrusted
input, and never puts the secret or the expected digest into a value the caller might log.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from enum import StrEnum

SIGNATURE_HEADER = "X-Hub-Signature-256"
"""Name of the header GitHub signs the delivery with."""

MAX_BODY_BYTES = 5 * 1024 * 1024
"""Bodies above this are rejected before hashing; a webhook delivery is never this large."""

_SIGNATURE = re.compile(r"sha256=([0-9a-fA-F]{64})")


class SignatureResult(StrEnum):
    """Outcome of a verification, as the value logged for the delivery.

    Every member is a fixed identifier: none of them carries the secret, the expected digest or
    any other part of the request, so the whole result is safe to put in a log line.
    """

    OK = "ok"
    MISSING_HEADER = "missing_header"
    MALFORMED_HEADER = "malformed_header"
    MISMATCH = "mismatch"
    BODY_TOO_LARGE = "body_too_large"

    @property
    def ok(self) -> bool:
        return self is SignatureResult.OK


def verify_signature(
    body: bytes,
    header: str | None,
    secret: str,
    *,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> SignatureResult:
    """Check `header` against HMAC-SHA256 of `body` under `secret`.

    `body` must be the request body exactly as received: decoding and re-encoding it, or parsing
    and re-serialising the JSON, changes the digest. `header` is the raw `X-Hub-Signature-256`
    value, or `None` when the request carried no such header.

    Rejection is a return value rather than an exception — a caller must be able to answer a
    forged request with a 4xx without an error path of its own. `BODY_TOO_LARGE` maps to 413 and
    every other non-`OK` result to 401.
    """
    if len(body) > max_body_bytes:
        return SignatureResult.BODY_TOO_LARGE

    if not header:
        return SignatureResult.MISSING_HEADER

    match = _SIGNATURE.fullmatch(header)
    if match is None:
        return SignatureResult.MALFORMED_HEADER

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Both sides are lowercase hex of a fixed length, so compare_digest sees no length difference
    # to leak and the comparison itself is constant-time.
    if not hmac.compare_digest(expected, match.group(1).lower()):
        return SignatureResult.MISMATCH

    return SignatureResult.OK
