"""Known-vector tests for `X-Hub-Signature-256` verification.

Expected digests are derived here independently of the code under test: either as literals
published by GitHub, or from the standard library's `hmac` directly. Nothing in this file asks
`verify_signature` what the right answer is.
"""

from __future__ import annotations

import hashlib
import hmac as stdlib_hmac

import pytest

from sentinel.security.hmac import (
    MAX_BODY_BYTES,
    SIGNATURE_HEADER,
    SignatureResult,
    verify_signature,
)

SECRET = "It's a Secret to Everybody"
BODY = b"Hello, World!"

# From GitHub's own documentation of X-Hub-Signature-256, for the secret and body above.
GITHUB_DOCUMENTED_DIGEST = "757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def sign(body: bytes, secret: str) -> str:
    """The header GitHub would send — computed from the standard library, not from our code."""
    return f"sha256={stdlib_hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


def test_the_header_name_is_the_one_github_sends() -> None:
    assert SIGNATURE_HEADER == "X-Hub-Signature-256"


def test_accepts_the_vector_published_by_github() -> None:
    result = verify_signature(BODY, f"sha256={GITHUB_DOCUMENTED_DIGEST}", SECRET)

    assert result is SignatureResult.OK
    assert result.ok


def test_the_helper_agrees_with_the_published_vector() -> None:
    # Guards every other case below: if `sign` were wrong, they would pass vacuously.
    assert sign(BODY, SECRET) == f"sha256={GITHUB_DOCUMENTED_DIGEST}"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b'{"action":"labeled"}', id="json"),
        pytest.param('{"title":"日本語 — em dash"}'.encode(), id="utf8"),
        pytest.param(bytes(range(256)), id="every-byte-value"),
        pytest.param(b"x" * 100_000, id="large"),
    ],
)
def test_accepts_a_valid_signature_over_the_raw_bytes(body: bytes) -> None:
    assert verify_signature(body, sign(body, SECRET), SECRET) is SignatureResult.OK


def test_rejects_a_signature_made_with_a_different_secret() -> None:
    header = sign(BODY, "not-the-configured-secret")

    assert verify_signature(BODY, header, SECRET) is SignatureResult.MISMATCH


def test_rejects_a_body_tampered_with_by_one_byte() -> None:
    header = sign(BODY, SECRET)
    tampered = b"Hello, World?"

    assert len(tampered) == len(BODY)
    assert verify_signature(tampered, header, SECRET) is SignatureResult.MISMATCH


def test_rejects_a_body_tampered_with_by_one_bit() -> None:
    body = b'{"action":"labeled"}'
    header = sign(body, SECRET)
    flipped = bytes([body[0] ^ 0b1]) + body[1:]

    assert verify_signature(flipped, header, SECRET) is SignatureResult.MISMATCH


def test_rejects_a_re_serialised_body_that_is_semantically_identical() -> None:
    # The signature covers bytes, not meaning: whitespace added by a JSON round-trip breaks it.
    body = b'{"action":"labeled"}'
    header = sign(body, SECRET)

    assert verify_signature(b'{"action": "labeled"}', header, SECRET) is SignatureResult.MISMATCH


@pytest.mark.parametrize("header", [pytest.param(None, id="absent"), pytest.param("", id="empty")])
def test_rejects_a_missing_header(header: str | None) -> None:
    assert verify_signature(BODY, header, SECRET) is SignatureResult.MISSING_HEADER


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(GITHUB_DOCUMENTED_DIGEST, id="no-prefix"),
        pytest.param(f"sha1={GITHUB_DOCUMENTED_DIGEST}", id="sha1-prefix"),
        pytest.param(f"SHA256={GITHUB_DOCUMENTED_DIGEST}", id="uppercase-prefix"),
        pytest.param(f"sha256 {GITHUB_DOCUMENTED_DIGEST}", id="space-instead-of-equals"),
        pytest.param(f" sha256={GITHUB_DOCUMENTED_DIGEST}", id="leading-space"),
        pytest.param(f"sha256={GITHUB_DOCUMENTED_DIGEST}\n", id="trailing-newline"),
        pytest.param("sha256=", id="digest-missing"),
        pytest.param(f"sha256={GITHUB_DOCUMENTED_DIGEST[:-1]}", id="digest-too-short"),
        pytest.param(f"sha256={GITHUB_DOCUMENTED_DIGEST}00", id="digest-too-long"),
        pytest.param(f"sha256={'z' * 64}", id="digest-not-hex"),
        pytest.param(f"sha256={'é' * 64}", id="digest-not-ascii"),
        pytest.param(
            f"sha256={GITHUB_DOCUMENTED_DIGEST},sha256={GITHUB_DOCUMENTED_DIGEST}",
            id="two-signatures",
        ),
    ],
)
def test_rejects_a_malformed_header(header: str) -> None:
    # A malformed header is a rejection, never an exception escaping as a 500.
    assert verify_signature(BODY, header, SECRET) is SignatureResult.MALFORMED_HEADER


def test_accepts_uppercase_hex() -> None:
    header = f"sha256={GITHUB_DOCUMENTED_DIGEST.upper()}"

    assert verify_signature(BODY, header, SECRET) is SignatureResult.OK


def test_rejects_a_body_over_the_limit() -> None:
    oversized = b"x" * (MAX_BODY_BYTES + 1)

    assert verify_signature(oversized, sign(oversized, SECRET), SECRET) is (
        SignatureResult.BODY_TOO_LARGE
    )


def test_accepts_a_body_exactly_at_the_limit() -> None:
    at_limit = b"x" * MAX_BODY_BYTES

    assert verify_signature(at_limit, sign(at_limit, SECRET), SECRET) is SignatureResult.OK


def test_an_oversized_body_is_never_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The point of the limit is that no attacker-supplied body of unbounded size reaches SHA-256.
    # A returned BODY_TOO_LARGE would only prove the check precedes the comparison, so refuse to
    # hash at all and let the digest itself report the violation.
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("hashed a body that exceeds the limit")

    monkeypatch.setattr("sentinel.security.hmac.hmac.new", refuse)
    oversized = b"x" * 11

    # With no header at all the size still wins, so the endpoint answers 413 rather than 401.
    assert verify_signature(oversized, None, SECRET, max_body_bytes=10) is (
        SignatureResult.BODY_TOO_LARGE
    )
    # A well-formed header is not enough to get the body hashed either. It is a literal because
    # `sign` would itself trip the refusal above.
    assert (
        verify_signature(oversized, f"sha256={GITHUB_DOCUMENTED_DIGEST}", SECRET, max_body_bytes=10)
        is SignatureResult.BODY_TOO_LARGE
    )


def test_the_digest_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Timing behaviour is invisible in the return value, so assert the call instead: `==` here
    # would pass every other test in this file while leaking the digest one byte at a time.
    calls: list[tuple[str, str]] = []
    real = stdlib_hmac.compare_digest

    def record(left: str, right: str) -> bool:
        calls.append((left, right))
        return bool(real(left, right))

    monkeypatch.setattr("sentinel.security.hmac.hmac.compare_digest", record)

    assert verify_signature(BODY, sign(BODY, SECRET), SECRET) is SignatureResult.OK
    assert calls == [(GITHUB_DOCUMENTED_DIGEST, GITHUB_DOCUMENTED_DIGEST)]


def test_the_documented_limit_is_five_megabytes() -> None:
    assert MAX_BODY_BYTES == 5 * 1024 * 1024


@pytest.mark.parametrize(
    "body, header",
    [
        pytest.param(BODY, sign(BODY, "another-secret"), id="mismatch"),
        pytest.param(BODY, None, id="missing"),
        pytest.param(BODY, "sha256=nonsense", id="malformed"),
        pytest.param(b"x" * (MAX_BODY_BYTES + 1), None, id="oversized"),
    ],
)
def test_a_rejection_leaks_neither_the_secret_nor_the_expected_digest(
    body: bytes, header: str | None
) -> None:
    result = verify_signature(body, header, SECRET)
    expected_digest = stdlib_hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert not result.ok
    for message in (str(result), repr(result), result.value):
        assert SECRET not in message
        assert expected_digest not in message
