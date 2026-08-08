"""Structured JSON logging for `api`, `worker` and `poller`.

One JSON object per line on stdout, shaped as `docs/07-observability.md#logging` specifies. The
shape is a contract, not an implementation detail: the demo runbook in `docs/09-operations.md`
greps for `'"event":"devin.session.created"'` and pipes the line to `jq`, so the rendering is
compact — no space after the colon — and every line is valid JSON on its own.

Two things this module owns, because leaving them to each call site means they happen unevenly:

- **Correlation.** The GitHub delivery id is the correlation id end to end
  (`docs/06-event-pipeline.md#correlation`). `correlate()` binds it, and the remediation, issue,
  class and Devin session it belongs to, onto every line logged for the rest of the block —
  including lines logged deeper in the call stack and in tasks started inside it.
- **Redaction.** Tokens and webhook secrets are never logged, at any level. Every value on its way
  to the renderer is scrubbed: the credentials this process is configured with are replaced
  wherever their text appears, credential-shaped strings are replaced whatever their source, and a
  key whose *name* says credential loses its value regardless of what it holds. See
  `docs/adr/2026-08-08-log-redaction-scrubs-values-not-only-keys.md`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Final, TextIO

import structlog
from pydantic import SecretStr
from structlog.processors import NAME_TO_LEVEL
from structlog.typing import EventDict, FilteringBoundLogger, Processor, WrappedLogger

from sentinel.config import Settings, get_settings

REDACTED: Final = "[redacted]"
"""What a credential is replaced with. A visible marker, so a reader can tell redaction from
absence — "the token was here and we took it out", not "nothing was logged"."""

_SECRET_KEY: Final = re.compile(
    r"token|secret|password|passwd|api[_-]?key|authorization|credential|database_url|dsn",
    re.IGNORECASE,
)
"""Keys whose value is dropped whatever it holds. Deliberately not matched against `signature` or
`session`: `signature_result` and `session_id` are exactly the fields an operator needs to see."""

_CREDENTIAL_SHAPED: Final = re.compile(
    r"""
      cog_[A-Za-z0-9_-]{8,}                       # Devin service-user token
    | github_pat_[A-Za-z0-9_]{16,}                # GitHub fine-grained PAT
    | gh[pousr]_[A-Za-z0-9]{16,}                  # GitHub classic PAT and OAuth tokens
    | [a-z][a-z0-9+.\-]*://[^\s:@/]+:[^\s@/]+@    # any URL carrying user:password@
    """,
    re.VERBOSE,
)
"""Credentials recognised by shape rather than by value. Covers what this process was never
configured with — a token read back from an API response, a DSN built at runtime, the credentials
of another installation quoted in an error message."""


def secret_values(settings: Settings) -> frozenset[str]:
    """Every credential the configuration holds, as the literal text to scrub out of a log line.

    Read off the model rather than from a hand-kept list, so that a `SecretStr` field added to
    `Settings` later is scrubbed without anyone remembering to come back here. Length is not
    considered: a secret short enough to also occur as ordinary text still redacts every
    occurrence, which mangles a line but never leaks one.
    """
    return frozenset(
        value.get_secret_value()
        for name in type(settings).model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
    )


def _scrub_text(text: str, secrets: frozenset[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return _CREDENTIAL_SHAPED.sub(REDACTED, text)


def _is_secret_key(key: object) -> bool:
    return isinstance(key, str) and _SECRET_KEY.search(key) is not None


def _scrub_mapping(mapping: Mapping[Any, Any], secrets: frozenset[str]) -> dict[str, Any]:
    # Keys are scrubbed too, and coerced to text: a credential can be a key as easily as a value,
    # and JSON has no other kind of key.
    return {
        _scrub_text(str(key), secrets): REDACTED if _is_secret_key(key) else _scrub(value, secrets)
        for key, value in mapping.items()
    }


def _scrub(value: object, secrets: frozenset[str]) -> Any:
    """Reduce a value to credential-free data the JSON renderer can serialise directly."""
    match value:
        case SecretStr():
            # `str(SecretStr)` is already masked, but only by pydantic's convention. Do not depend
            # on someone else's placeholder for the one guarantee this module makes.
            return REDACTED
        case str():
            return _scrub_text(value, secrets)
        case bool() | int() | float() | None:
            return value
        case bytes() | bytearray():
            return _scrub_text(bytes(value).decode(errors="replace"), secrets)
        case Mapping():
            return _scrub_mapping(value, secrets)
        case Sequence() | set() | frozenset():
            return [_scrub(item, secrets) for item in value]
        case _:
            # Anything else would reach the renderer intact and be serialised by its fallback,
            # which is a `str()` this module never sees. Do that conversion here instead, where the
            # result is still scrubbed — an ORM row or an exception rendered by `repr` is one of
            # the likelier ways for a credential to arrive.
            return _scrub_text(str(value), secrets)


def _redactor(secrets: frozenset[str]) -> Processor:
    """The redaction processor, closed over the credentials of the configuration it was built for.

    A closure rather than module state: the secrets live and die with the processor chain they were
    installed in, so reconfiguring logging cannot leave a stale set behind.
    """

    def redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        return _scrub_mapping(event_dict, secrets)

    return redact


def configure_logging(settings: Settings | None = None, *, stream: TextIO | None = None) -> None:
    """Install the JSON pipeline. Called once by each of `api`, `worker` and `poller`, at startup.

    `stream` exists for tests, which need somewhere to read the output back from.
    """
    settings = get_settings() if settings is None else settings
    structlog.configure(
        processors=[
            # First, so that bound correlation is filtered and redacted exactly like anything a
            # call site passed directly.
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            # Renders the traceback to text *before* redaction runs. An exception is a common
            # carrier for a credential — a DSN in a connection error, a request body in an HTTP
            # error — and once the renderer has it, redaction is too late.
            structlog.processors.format_exc_info,
            _redactor(secret_values(settings)),
            # Compact separators: `docs/09-operations.md` greps for '"event":"…"' with no space.
            structlog.processors.JSONRenderer(separators=(",", ":")),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(NAME_TO_LEVEL[settings.log_level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout if stream is None else stream),
        # Modules take their logger at import time, which is before this function runs. A cached
        # logger would freeze whatever configuration was in force at its first call — the default
        # console renderer in production, and the previous test's buffer in a test.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """A logger for a module: `log = get_logger(__name__)` at import time is safe."""
    name_args = () if name is None else (name,)
    logger: FilteringBoundLogger = structlog.get_logger(*name_args)
    return logger


def _correlation(
    run: str | None,
    remediation_id: int | None,
    issue: int | None,
    issue_class: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    """The correlation fields under the names `docs/07-observability.md#logging` gives them.

    `issue_class` is bound as `class`, which is the name in the spec and a word Python will not
    accept as a keyword argument. Fields left unset are not bound at all: a line saying
    `"session_id": null` claims the session is absent, when the truth is that this code does not
    know it yet.
    """
    fields = {
        "run": run,
        "remediation_id": remediation_id,
        "issue": issue,
        "class": issue_class,
        "session_id": session_id,
    }
    return {name: value for name, value in fields.items() if value is not None}


@contextmanager
def correlate(
    *,
    run: str | None = None,
    remediation_id: int | None = None,
    issue: int | None = None,
    issue_class: str | None = None,
    session_id: str | None = None,
) -> Iterator[None]:
    """Bind the correlation ids onto every line logged inside this block.

    Bound in a context variable, so it reaches code that knows nothing about correlation — a
    logger deep in the call stack, and any task started inside the block — and unwinds on the way
    out. Nesting adds: entering with a `session_id` alone keeps the `run` bound outside it.
    """
    with structlog.contextvars.bound_contextvars(
        **_correlation(run, remediation_id, issue, issue_class, session_id)
    ):
        yield


def bind_correlation(
    *,
    run: str | None = None,
    remediation_id: int | None = None,
    issue: int | None = None,
    issue_class: str | None = None,
    session_id: str | None = None,
) -> None:
    """Bind correlation for the rest of the current context, with no block to leave.

    For a worker that claims a job, learns the session id partway through, and logs until it takes
    the next job — where the scope is the loop iteration rather than a lexical block. Pair it with
    `clear_correlation()` at the top of each iteration.
    """
    structlog.contextvars.bind_contextvars(
        **_correlation(run, remediation_id, issue, issue_class, session_id)
    )


def clear_correlation() -> None:
    """Drop everything bound in this context, so the next unit of work starts clean."""
    structlog.contextvars.clear_contextvars()
