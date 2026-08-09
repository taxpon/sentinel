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

LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class ConfigurationError(RuntimeError):
    """The environment does not describe a usable configuration."""


def normalise_database_url(url: str) -> str:
    """Return `url` with the asyncpg driver named, rejecting one that names a different driver.

    Every managed provider issues `postgres://` or `postgresql://`, and `fly postgres attach` writes
    one straight into the app's secrets. Demanding the driver made deployment a step where the
    operator has to know to rewrite a URL a platform command had just written for them, and getting
    it wrong crash-loops all three processes. The driver is Sentinel's choice rather than theirs, so
    it is applied instead of demanded.

    What the check still catches is a URL naming a *different* driver — the case it existed for,
    where "psycopg2 is not installed" surfaces from inside the engine on the first query rather than
    at startup.

    The DSN embeds the Postgres password, so the message must not echo `url`.
    """
    if url.startswith(DATABASE_URL_SCHEME):
        return url
    scheme, separator, rest = url.partition("://")
    if separator and scheme in PLAIN_POSTGRES_SCHEMES:
        return DATABASE_URL_SCHEME + rest
    raise ValueError(
        "DATABASE_URL must be a postgres:// or postgresql:// URL, or already name the "
        f"{DATABASE_URL_SCHEME} driver"
    )


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
    # NoDecode turns off the built-in JSON handling so that a malformed value fails as a validation
    # error naming the variable, rather than as a SettingsError raised before validation begins.
    #
    # Both are held immutably. This map decides which playbook Devin runs for an issue class, on an
    # object every module in the process shares, so a stray write to it would silently redirect the
    # remediation; freezing the model above stops a field being rebound but not edited in place.
    devin_playbook_ids: Annotated[Mapping[str, str], NoDecode, AfterValidator(MappingProxyType)]
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
