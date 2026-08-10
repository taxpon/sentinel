"""Process configuration, loaded from the environment.

Every variable in the table in `docs/09-operations.md#configuration` appears here exactly once, with
the documented default and required-ness. `.env.example` is the canonical list of names;
`tests/test_env_example.py` keeps that file and the table in step.

The variables are declared in groups — what the pipeline acts on, the GitHub token, the webhook
secret, Devin, the database, the policy, the reporting — and `Settings` is all of them at once.
`api`, `worker` and `poller` load `Settings` through `get_settings()` and so still fail at startup
if any one variable is missing. A script loads one group, or a composition of a few, through
`load_config()` and is told about nothing else: filing issues on the fork needs a GitHub token, not
Devin credentials and a database
(`docs/adr/2026-08-10-a-script-loads-the-configuration-group-it-reads.md`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode

from pydantic import (
    AfterValidator,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DATABASE_URL_SCHEME = "postgresql+asyncpg://"

# What a managed provider hands out. Postgres itself has no notion of a driver in a connection
# URL — naming one is SQLAlchemy's convention — so these say which database to reach, not how.
PLAIN_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})

# The keyword arguments of `asyncpg.connect`. SQLAlchemy's asyncpg dialect does not interpret the
# query string: `create_connect_args` does `opts.update(url.query)` and the result becomes keyword
# arguments to `asyncpg.connect`. So a query parameter is usable exactly when it is named here or
# just below, and anything else is a `TypeError` raised on the first connection rather than at
# startup — which on Fly is the release command, so the deploy never starts.
#
# Transcribed rather than introspected, so that this module keeps importing no driver and the
# rejection is decided by a value that can be read here. `tests/test_config.py` holds the set to the
# installed asyncpg's actual signature, so a version that adds a keyword fails CI rather than
# rejecting a URL an operator was entitled to write.
ASYNCPG_CONNECT_PARAMETERS = frozenset(
    {
        "command_timeout",
        "connection_class",
        "database",
        "direct_tls",
        "dsn",
        "gsslib",
        "host",
        "krbsrvname",
        "loop",
        "max_cacheable_statement_size",
        "max_cached_statement_lifetime",
        "passfile",
        "password",
        "port",
        "record_class",
        "server_settings",
        "service",
        "servicefile",
        "ssl",
        "statement_cache_size",
        "target_session_attrs",
        "timeout",
        "user",
    }
)

# The four the dialect's DBAPI shim implements itself and pops before calling `asyncpg.connect`, so
# they are accepted despite not appearing above.
DIALECT_QUERY_PARAMETERS = frozenset(
    {
        "async_creator_fn",
        "async_fallback",
        "prepared_statement_cache_size",
        "prepared_statement_name_func",
    }
)

# `sslmode` is libpq's spelling of a setting asyncpg calls `ssl`, and the two take the same six
# values — `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`. Renaming it is not
# an interpretation of ours: asyncpg's own DSN parser performs this rewrite, `ssl` inheriting the
# `sslmode` string unchanged, and then parses it against an enum of the libpq names
# (`asyncpg/connect_utils.py`). SQLAlchemy never hands asyncpg a DSN to parse — only keyword
# arguments — which is the whole reason the rewrite has to happen here instead.
TRANSLATED_QUERY_PARAMETERS: Mapping[str, str] = MappingProxyType({"sslmode": "ssl"})

LogLevel = Literal["debug", "info", "warning", "error", "critical"]

# NoDecode turns off the built-in JSON handling so that a malformed value fails as a validation
# error naming the variable, rather than as a SettingsError raised before validation begins.
#
# Held immutably. The map decides which playbook Devin runs for an issue class, on an object every
# module in the process shares, so a stray write to it would silently redirect the remediation;
# freezing the model stops a field being rebound but not edited in place.
#
# Named rather than written inline because `scripts/bootstrap_devin.py --list-playbooks` restates
# the field with a default — it is the option that *finds* the ids, so it cannot require them — and
# a second copy of this annotation is a second thing to keep in step.
PlaybookIds = Annotated[Mapping[str, str], NoDecode, AfterValidator(MappingProxyType)]


class ConfigurationError(RuntimeError):
    """The environment does not describe a usable configuration."""


def normalise_database_url(url: str) -> str:
    """Return `url` with the asyncpg driver named and its query string in asyncpg's spelling.

    Every managed provider issues `postgres://` or `postgresql://`, and `fly postgres attach` writes
    one straight into the app's secrets. Demanding the driver made deployment a step where the
    operator has to know to rewrite a URL a platform command had just written for them, and getting
    it wrong crash-loops all three processes. The driver is Sentinel's choice rather than theirs, so
    it is applied instead of demanded.

    What the check still catches is a URL naming a *different* driver — the case it existed for,
    where "psycopg2 is not installed" surfaces from inside the engine on the first query rather than
    at startup.

    Swapping the driver is not enough on its own, because the query string is written for the driver
    the provider assumed. A provider-issued URL carries libpq settings, and the dialect forwards
    every one of them to `asyncpg.connect` as a keyword argument it may not define; `sslmode` is the
    one every hosted database sets, and it took down the first deploy. So the query string is
    translated where an exact equivalent exists and rejected where none does, rather than carried
    across and left to fail on the first connection.

    The DSN embeds the Postgres password, so no message here may echo `url` — a parameter *name* is
    reported, never a value.
    """
    base, separator, query = url.partition("?")
    if base.startswith(DATABASE_URL_SCHEME):
        normalised = base
    else:
        scheme, delimiter, rest = base.partition("://")
        if not delimiter or scheme not in PLAIN_POSTGRES_SCHEMES:
            raise ValueError(
                "DATABASE_URL must be a postgres:// or postgresql:// URL, or already name the "
                f"{DATABASE_URL_SCHEME} driver"
            )
        normalised = DATABASE_URL_SCHEME + rest
    if not separator:
        return normalised
    return f"{normalised}?{_normalise_query(query)}"


def _normalise_query(query: str) -> str:
    """Rename the query parameters that asyncpg spells differently, rejecting those it cannot take.

    Rejecting is the deliberate half. The parameters with no asyncpg equivalent are the ones saying
    how strictly the server's certificate is checked (`sslrootcert`, `sslcert`, `sslkey`) or what
    the session starts as (`options`, `application_name`), and asyncpg exposes those only as an
    `ssl.SSLContext` or a `server_settings` dict — objects, which a URL cannot carry. Dropping them
    would connect anyway, less verified or less configured than the operator asked for and with
    nothing said about it. A deployment that stops with the parameter named costs the time it takes
    to read the message; one that quietly stops verifying a certificate costs rather more.
    """
    parameters = parse_qsl(query, keep_blank_values=True)
    present = {name for name, _ in parameters}
    translated: list[tuple[str, str]] = []
    for name, value in parameters:
        target = TRANSLATED_QUERY_PARAMETERS.get(name)
        if target is None:
            if name not in ASYNCPG_CONNECT_PARAMETERS and name not in DIALECT_QUERY_PARAMETERS:
                raise ValueError(
                    f"DATABASE_URL sets '{name}', which the asyncpg driver does not accept. It is "
                    "a libpq setting with no equivalent asyncpg can be given through a URL; remove "
                    "it. ('sslmode' is the exception, and is translated automatically.)"
                )
            translated.append((name, value))
        elif target in present:
            raise ValueError(
                f"DATABASE_URL sets both '{name}' and '{target}', which are the same setting under "
                f"the libpq and asyncpg names. Keep '{target}'"
            )
        else:
            translated.append((target, value))
    return urlencode(translated)


def _decode_json(value: Any, variable: str) -> Any:
    """Decode a JSON-valued variable, leaving anything already decoded alone."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        # `exc.msg` describes the syntax, never the payload, so this stays safe to log.
        raise ValueError(f"{variable} must be valid JSON: {exc.msg}") from None


class ConfigurationGroup(BaseSettings):
    """The loading rules, shared by every group below and by `Settings`.

    A group is a set of variables that are read together: nothing else about it differs from the
    whole. Composing several is ordinary inheritance — `class C(GitHubSettings, WebhookSettings)` —
    because every group reads the same environment under the same rules, with no prefix and no
    nesting, so a variable means the same thing whichever model happens to declare it.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `.env.example` ships every variable, with the required ones blank, and `make db` copies it
        # verbatim. Without this, a blank line would configure an empty token rather than leaving
        # the variable unset, and the required-variable check would never fire.
        env_ignore_empty=True,
        # `env_ignore_empty` only catches the exactly-empty value, and a secret pasted into `.env`
        # tends to arrive with a stray space attached. Stripping first means a whitespace-only value
        # fails the `min_length` below rather than configuring a blank credential.
        str_strip_whitespace=True,
        # The same `.env` feeds Compose, which reads variables of its own (POSTGRES_PORT, API_PORT).
        extra="ignore",
        # Nothing writes to the settings, and `get_settings()` hands the same object to the api, the
        # worker and the poller, so one assignment anywhere would reconfigure the whole process —
        # past every validator here, and past SecretStr, since the assigned value is a plain str.
        # Freezing stops a field being rebound; it does not reach inside one, so the two container
        # fields below are held in immutable types of their own.
        frozen=True,
    )


class TargetSettings(ConfigurationGroup):
    """Which repository the pipeline acts on, on which branch, under which label.

    No credential and no required variable: this group says *what* is being remediated, and every
    part of the system that touches the fork needs it — which is why the two groups below extend it
    rather than sit beside it.
    """

    target_repo: str = Field(default="taxpon/superset", min_length=1)
    target_base_branch: str = Field(default="master", min_length=1)
    autofix_label: str = Field(default="devin:autofix", min_length=1)
    # The two names of the fork's own CI, which `sentinel.github.checks` reads the signal from and
    # `get_failing_job` fetches a log out of. Both are facts about the workflow in
    # `docs/fork-ci/devin-autofix-ci.yml`
    # rather than deployment choices, and both are settings because the fork is not ours to pin: a
    # renamed job or a moved file must be a variable to change, not a release.
    ci_required_check_name: str = Field(default="devin-autofix-ci", min_length=1)
    ci_workflow_path: str = Field(default=".github/workflows/devin-autofix-ci.yml", min_length=1)


class GitHubSettings(TargetSettings):
    """What talking to GitHub about that repository needs.

    Not the webhook secret. That one verifies deliveries *arriving*; it has nothing to do with
    making a call, and requiring it here would mean a script that only files issues could not start
    without it.
    """

    github_token: SecretStr = Field(min_length=1)


class WebhookSettings(ConfigurationGroup):
    """The shared secret deliveries are signed with — `api`, and the bootstrap that registers it."""

    github_webhook_secret: SecretStr = Field(min_length=1)


class DevinSettings(TargetSettings):
    """The Devin organisation, and what a session about the target repository is created with."""

    devin_api_base: str = Field(default="https://api.devin.ai", min_length=1)
    devin_api_token: SecretStr = Field(min_length=1)
    devin_org_id: str = Field(min_length=1)
    devin_enterprise_id: str | None = None
    devin_playbook_ids: PlaybookIds
    devin_knowledge_ids: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("devin_playbook_ids", mode="before")
    @classmethod
    def _parse_playbook_ids(cls, value: Any) -> Any:
        return _decode_json(value, "DEVIN_PLAYBOOK_IDS")

    @field_validator("devin_knowledge_ids", mode="before")
    @classmethod
    def _parse_knowledge_ids(cls, value: Any) -> Any:
        return _decode_json(value, "DEVIN_KNOWLEDGE_IDS")

    @field_serializer("devin_playbook_ids")
    def _serialize_playbook_ids(self, value: Mapping[str, str]) -> dict[str, str]:
        # pydantic cannot serialize a mappingproxy on its own, and `model_dump_json` is how the
        # startup log line is built.
        return dict(value)

    @field_validator("devin_enterprise_id", mode="before")
    @classmethod
    def _absent_enterprise_id(cls, value: Any) -> Any:
        # Blank means the same as omitted — fall back to the locally derived metrics. Consumers ask
        # `is None`, so a whitespace-only value must not survive as an empty string.
        return None if isinstance(value, str) and not value.strip() else value


class DatabaseSettings(ConfigurationGroup):
    """Where the queue, the remediations and the event log live."""

    # A SecretStr because the DSN embeds the password: it must not surface in a repr or a log line
    # any more than a token does. Read it with `.get_secret_value()`.
    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def _apply_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        # Re-wrapped, because the normalised URL carries the same password the given one did.
        return SecretStr(normalise_database_url(value.get_secret_value()))


class PolicySettings(ConfigurationGroup):
    """The ceilings the pipeline runs under. Every one has a default; none is a credential."""

    max_concurrent_sessions: int = Field(default=3, gt=0)
    daily_acu_budget: float = Field(default=100.0, gt=0)
    # Zero is meaningful: escalate to a human on the first review rather than attempting a fix.
    max_fix_cycles: int = Field(default=3, ge=0)
    max_job_attempts: int = Field(default=5, gt=0)
    job_lease_timeout_seconds: int = Field(default=900, gt=0)
    poll_interval_seconds: int = Field(default=20, gt=0)


class ReportingSettings(ConfigurationGroup):
    """What the cost panel scales by, and how much anything says about itself."""

    acu_unit_cost_usd: float = Field(default=2.25, ge=0)
    log_level: LogLevel = "info"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        # `str_strip_whitespace` does not reach a Literal field, so strip here too.
        return value.strip().lower() if isinstance(value, str) else value


class Settings(
    DevinSettings,
    GitHubSettings,
    WebhookSettings,
    DatabaseSettings,
    PolicySettings,
    ReportingSettings,
):
    """Every group at once — the configuration `api`, `worker` and `poller` load.

    Composed rather than declared, so that the table in `docs/09-operations.md#configuration` stays
    covered exactly once: a variable belongs to one group, and this model is the union of them.
    A service is only ever configured through this, so narrowing is something a script opts into and
    never something a process falls into.
    """


def _describe(error: ValidationError) -> str:
    """Render the failures as variable names and messages, never the values they rejected."""
    lines: list[str] = []
    for item in error.errors(include_url=False):
        location = item["loc"]
        # An empty `loc` belongs to a model-wide error, of which there are none today. It must still
        # not raise: an IndexError here would be raised while the ValidationError is being handled,
        # which re-attaches it to the ConfigurationError as `__context__` and undoes the whole point
        # of this function.
        variable = str(location[0]).upper() if location else "configuration"
        # The tail of `loc` is kept: for DEVIN_PLAYBOOK_IDS it is the offending issue class, which
        # the operator needs in order to find the bad entry. It is a key or an index, never a value.
        for part in location[1:]:
            variable += f"[{part}]" if isinstance(part, int) else f".{part}"
        lines.append(f"  {variable}: {item['msg']}{_blank_hint(variable, item['type'])}")
    return "\n".join(
        ["invalid configuration — see .env.example and docs/09-operations.md:", *lines]
    )


def _blank_hint(variable: str, error_type: str) -> str:
    """Whether this "missing" variable is in fact present and blank, and where that comes from.

    `env_ignore_empty` makes a blank value mean "unset", which is what `.env.example` shipping the
    required variables blank depends on. The cost is that "Field required" cannot distinguish a
    variable nobody set from one an empty shell variable is quietly overriding — and Compose gives
    the shell precedence over `env_file`, so a perfectly correct `.env` reads as absent. That is not
    a hypothetical: a shell with the `gh` CLI configured exports a blank `GITHUB_TOKEN`, and the
    error then sends the operator to inspect the one file that was already right.

    Safe to report: the value is blank, so naming it discloses nothing. Nothing else about the
    environment is read or echoed.
    """
    if error_type != "missing":
        return ""
    value = os.environ.get(variable)
    if value is None or value.strip():
        return ""
    return (
        " — the variable is set but blank, which counts as unset. A blank shell variable takes"
        " precedence over the same name in .env; unset it, or give it a value"
    )


def load_config[Group: ConfigurationGroup](model: type[Group]) -> Group:
    """Read `model` from the environment, or raise `ConfigurationError` naming what is wrong.

    What a script calls, with the group — or the composition of groups — it actually reads:

        class Configuration(GitHubSettings, WebhookSettings):
            '''Exactly what this script uses.'''

        settings = load_config(Configuration)

    Only the variables of `model` are required, so a dry run of a script that talks to GitHub starts
    with a GitHub token and nothing else. Everything the whole model does is kept, because it is the
    same model: `SecretStr`, `env_ignore_empty`, the frozen fields, and an error that names
    variables without echoing values.

    Not cached, unlike `get_settings()`. A script reads its configuration once, in `main`, and
    hands the object down; there is no second reader for a cache to keep in step with, and no cache
    to remember to clear between tests.
    """
    try:
        return model()
    except ValidationError as exc:
        # The ValidationError carries the input it rejected, and for a variable reported as missing
        # that input is the raw environment — every credential at once, before SecretStr wraps any
        # of them. So only the message escapes: it is built here and raised below, because
        # `from None` clears `__cause__` but leaves `__context__` pointing at the ValidationError.
        # Outside the handler there is no exception left for Python to attach.
        message = _describe(exc)
    raise ConfigurationError(message) from None


@cache
def get_settings() -> Settings:
    """The configuration for this process — every variable, whichever ones this process reads.

    Cached, because the environment does not change while the process runs: validation happens once,
    at the first call, so `api`, `worker` and `poller` fail on startup rather than on whichever
    request first reads a bad value, and every module sees the same object. Tests that need a
    different environment call `get_settings.cache_clear()`.

    A service loads `Settings` and nothing narrower. A process that started without its Devin
    credentials would fail on the first session it tried to create — mid-remediation, against an
    issue somebody had already labelled — instead of on the deploy that misconfigured it.
    """
    return load_config(Settings)
