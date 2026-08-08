"""Process configuration, loaded from the environment.

Every variable in the table in `docs/09-operations.md#configuration` appears here exactly once, with
the documented default and required-ness. `.env.example` is the canonical list of names;
`tests/test_env_example.py` keeps that file and the table in step.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DATABASE_URL_SCHEME = "postgresql+asyncpg://"

LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class ConfigurationError(RuntimeError):
    """The environment does not describe a usable configuration."""


def _decode_json(value: Any, variable: str) -> Any:
    """Decode a JSON-valued variable, leaving anything already decoded alone."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        # `exc.msg` describes the syntax, never the payload, so this stays safe to log.
        raise ValueError(f"{variable} must be valid JSON: {exc.msg}") from None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `.env.example` ships every variable, with the required ones blank, and `make db` copies it
        # verbatim. Without this, a blank line would configure an empty token rather than leaving
        # the variable unset, and the required-variable check would never fire.
        env_ignore_empty=True,
        # The same `.env` feeds Compose, which reads variables of its own (POSTGRES_PORT, API_PORT).
        extra="ignore",
    )

    # --- Devin ---
    devin_api_base: str = Field(default="https://api.devin.ai", min_length=1)
    devin_api_token: SecretStr = Field(min_length=1)
    devin_org_id: str = Field(min_length=1)
    devin_enterprise_id: str | None = None
    # NoDecode turns off the built-in JSON handling so that a malformed value fails as a validation
    # error naming the variable, rather than as a SettingsError raised before validation begins.
    devin_playbook_ids: Annotated[dict[str, str], NoDecode]
    devin_knowledge_ids: Annotated[list[str], NoDecode] = []

    # --- GitHub ---
    github_token: SecretStr = Field(min_length=1)
    github_webhook_secret: SecretStr = Field(min_length=1)
    target_repo: str = Field(default="taxpon/superset", min_length=1)
    target_base_branch: str = Field(default="master", min_length=1)
    autofix_label: str = Field(default="devin:autofix", min_length=1)

    # --- Database ---
    # A SecretStr because the DSN embeds the password: it must not surface in a repr or a log line
    # any more than a token does. Read it with `.get_secret_value()`.
    database_url: SecretStr

    # --- Policy ---
    max_concurrent_sessions: int = Field(default=3, gt=0)
    daily_acu_budget: float = Field(default=100.0, gt=0)
    # Zero is meaningful: escalate to a human on the first review rather than attempting a fix.
    max_fix_cycles: int = Field(default=3, ge=0)
    max_job_attempts: int = Field(default=5, gt=0)
    job_lease_timeout_seconds: int = Field(default=900, gt=0)
    poll_interval_seconds: int = Field(default=20, gt=0)

    # --- Reporting ---
    acu_unit_cost_usd: float = Field(default=2.25, ge=0)
    log_level: LogLevel = "info"

    @field_validator("devin_playbook_ids", mode="before")
    @classmethod
    def _parse_playbook_ids(cls, value: Any) -> Any:
        return _decode_json(value, "DEVIN_PLAYBOOK_IDS")

    @field_validator("devin_knowledge_ids", mode="before")
    @classmethod
    def _parse_knowledge_ids(cls, value: Any) -> Any:
        return _decode_json(value, "DEVIN_KNOWLEDGE_IDS")

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        return value.lower() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        # Checking the driver here turns "psycopg2 is not installed", raised from inside the engine
        # on the first query, into one legible error at startup. The message must not echo the URL.
        if not value.get_secret_value().startswith(DATABASE_URL_SCHEME):
            raise ValueError(f"DATABASE_URL must use the {DATABASE_URL_SCHEME} scheme")
        return value


def _describe(error: ValidationError) -> str:
    lines: list[str] = []
    for item in error.errors(include_url=False):
        parts = [str(part) for part in item["loc"]]
        variable = ".".join([parts[0].upper(), *parts[1:]])
        lines.append(f"  {variable}: {item['msg']}")
    return "\n".join(
        ["invalid configuration — see .env.example and docs/09-operations.md:", *lines]
    )


@cache
def get_settings() -> Settings:
    """The configuration for this process.

    Cached, because the environment does not change while the process runs: validation happens once,
    at the first call, so `api`, `worker` and `poller` fail on startup rather than on whichever
    request first reads a bad value, and every module sees the same object. Tests that need a
    different environment call `get_settings.cache_clear()`.
    """
    try:
        return Settings()
    except ValidationError as exc:
        # `from None` drops the ValidationError: it carries the rejected input, which for
        # DEVIN_API_TOKEN or DATABASE_URL is a credential that must not reach a traceback.
        raise ConfigurationError(_describe(exc)) from None
