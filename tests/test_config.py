"""The configuration table in `docs/09-operations.md#configuration` is the contract; these tests
check the model against it. Names, required-ness and every documented default are read back out of
the table itself, via the parser `tests/test_env_example.py` already uses to hold `.env.example` to
it — so the file, the table and the model cannot drift apart in any pair. Two rows document no
default at all, and for those the table can only say that the model must not invent one; their
values are pinned by the hand-transcribed DEFAULTS below, as are the types, which the table's
strings cannot express."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.config import ConfigurationError, Settings, get_settings
from test_env_example import documented_variables

# The required column of the table, with a value that satisfies each one.
REQUIRED = {
    "DEVIN_API_TOKEN": "cog_secret_token",
    "DEVIN_ORG_ID": "org-abc123",
    "DEVIN_PLAYBOOK_IDS": '{"security": "playbook-sec", "bug": "playbook-bug"}',
    "GITHUB_TOKEN": "github_pat_secret",
    "GITHUB_WEBHOOK_SECRET": "webhook-secret-value",
    "DATABASE_URL": "postgresql+asyncpg://sentinel:dbpassword@localhost:5432/sentinel",
}

# The default column of the table, as the value the model must produce when the variable is unset.
DEFAULTS: dict[str, object] = {
    "DEVIN_API_BASE": "https://api.devin.ai",
    "DEVIN_ENTERPRISE_ID": None,
    "DEVIN_KNOWLEDGE_IDS": (),
    "TARGET_REPO": "taxpon/superset",
    "TARGET_BASE_BRANCH": "master",
    "AUTOFIX_LABEL": "devin:autofix",
    "MAX_CONCURRENT_SESSIONS": 3,
    "DAILY_ACU_BUDGET": 100,
    "MAX_FIX_CYCLES": 3,
    "MAX_JOB_ATTEMPTS": 5,
    "JOB_LEASE_TIMEOUT_SECONDS": 900,
    "POLL_INTERVAL_SECONDS": 20,
    "ACU_UNIT_COST_USD": 2.25,
    "LOG_LEVEL": "info",
}

Load = Callable[..., Settings]


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No test may read the developer's environment, or the `.env` in their working tree.

    `env_file` is resolved against the working directory, so the tests run from an empty one.
    Clearing on the way out matters as much as on the way in: the cache outlives this module, and a
    later test calling `get_settings()` would otherwise be handed the fake environment built here.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def load(monkeypatch: pytest.MonkeyPatch) -> Load:
    """Load the configuration from a complete environment, with the given variables changed.

    `None` removes a variable rather than setting it.
    """

    def _load(**overrides: str | None) -> Settings:
        for name, value in {**REQUIRED, **overrides}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        return get_settings()

    return _load


def test_the_model_covers_the_documented_table_and_nothing_else() -> None:
    # Without this, a variable dropped from the model would simply stop being tested below.
    documented = set(documented_variables())

    assert {field.upper() for field in Settings.model_fields} == documented
    assert set(REQUIRED) | set(DEFAULTS) == documented  # the transcription above is complete


def test_the_model_requires_exactly_the_variables_the_table_marks_required() -> None:
    documented = {name for name, spec in documented_variables().items() if spec.required}
    required = {
        name.upper() for name, field in Settings.model_fields.items() if field.is_required()
    }

    assert required == documented
    assert set(REQUIRED) == documented


@pytest.mark.parametrize("variable", sorted(DEFAULTS))
def test_the_model_default_is_the_value_the_table_documents(load: Load, variable: str) -> None:
    # The hand-transcribed DEFAULTS above pin the type; this pins them to the document itself, by
    # comparing the unset variable against the same variable set to its documented default.
    documented = documented_variables()[variable].default
    attribute = variable.lower()
    unset = getattr(load(**{variable: None}), attribute)

    if not documented:
        # DEVIN_ENTERPRISE_ID and DEVIN_KNOWLEDGE_IDS document no default. Setting them to the
        # documented value would just be setting them blank, which means unset, so the comparison
        # below would be with themselves. What the table does say is that there is nothing to fall
        # back to, so the model must not invent a value.
        assert not unset
        return

    assert unset == getattr(load(**{variable: documented}), attribute)


@pytest.mark.parametrize(("variable", "expected"), sorted(DEFAULTS.items()))
def test_an_unset_variable_takes_its_documented_default(
    load: Load, variable: str, expected: object
) -> None:
    settings = load(**{variable: None})

    assert getattr(settings, variable.lower()) == expected


@pytest.mark.parametrize("variable", sorted(REQUIRED))
def test_a_missing_required_variable_is_named_in_the_error(load: Load, variable: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load(**{variable: None})

    assert variable in str(raised.value)
    assert ".env.example" in str(raised.value)


@pytest.mark.parametrize("variable", sorted(REQUIRED))
@pytest.mark.parametrize("blank", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_a_blank_required_variable_is_rejected(load: Load, variable: str, blank: str) -> None:
    # `cp .env.example .env` leaves exactly these blank, so blank must mean "not filled in yet"
    # rather than "configured as the empty string" — and a line left as whitespace is still blank.
    with pytest.raises(ConfigurationError) as raised:
        load(**{variable: blank})

    assert variable in str(raised.value)


def test_an_empty_variable_says_so_rather_than_reading_as_absent(load: Load) -> None:
    """ "Field required" is true of an empty value and of no value, and the fixes are opposite ones.

    `env_ignore_empty` is what makes an empty value mean "unset", which `.env.example` shipping the
    required variables blank depends on. The cost lands on the operator: Compose gives a shell
    variable precedence over the same name in `env_file`, and a shell with the `gh` CLI configured
    exports an empty `GITHUB_TOKEN` — so a correct `.env` is overridden and the error sends them to
    inspect the one file that was already right. It cost a stack crash-looping through four restarts
    to find.
    """
    with pytest.raises(ConfigurationError) as raised:
        load(GITHUB_TOKEN="")

    assert "set but blank" in str(raised.value)
    assert "precedence" in str(raised.value)


def test_a_whitespace_only_variable_needs_no_hint(load: Load) -> None:
    """It never claimed to be absent. `env_ignore_empty` ignores only the exactly-empty value, so
    whitespace survives to `str_strip_whitespace` and fails `min_length` — an error that already
    says the value was read and found wanting."""
    with pytest.raises(ConfigurationError) as raised:
        load(GITHUB_TOKEN="   ")

    assert "Field required" not in str(raised.value)
    assert "blank" not in str(raised.value)


def test_a_variable_nobody_set_is_not_blamed_on_the_shell(load: Load) -> None:
    """The hint is only true when the variable is really there. Offering it for an absent one would
    send the operator to unset something that does not exist."""
    with pytest.raises(ConfigurationError) as raised:
        load(GITHUB_TOKEN=None)

    assert "GITHUB_TOKEN: Field required" in str(raised.value)
    assert "blank" not in str(raised.value)


def test_the_hint_is_not_offered_for_a_value_that_was_read_and_rejected(load: Load) -> None:
    """A malformed `DATABASE_URL` is present and non-blank: it failed a validator, not the
    required-ness check, and "set but blank" would be a false explanation of it."""
    with pytest.raises(ConfigurationError) as raised:
        load(DATABASE_URL="postgresql+psycopg2://a:b@c/d")

    assert "blank" not in str(raised.value)


def test_whitespace_around_a_pasted_value_is_discarded(load: Load) -> None:
    settings = load(
        DEVIN_ORG_ID="  org-pasted  ",
        DEVIN_API_TOKEN="  cog_pasted\t",
        LOG_LEVEL="  INFO  ",
    )

    assert settings.devin_org_id == "org-pasted"
    assert settings.devin_api_token.get_secret_value() == "cog_pasted"
    assert settings.log_level == "info"


def test_an_optional_variable_left_as_whitespace_reads_as_absent(load: Load) -> None:
    # The enterprise metrics panel falls back when this is unset, and the consumer asks `is None`.
    # An empty string would tell it the panel is configured.
    assert load(DEVIN_ENTERPRISE_ID="   ").devin_enterprise_id is None
    assert load(DEVIN_ENTERPRISE_ID="ent-1").devin_enterprise_id == "ent-1"


def test_missing_required_variables_are_reported_together(load: Load) -> None:
    # One run of `docker compose up` should reveal everything that is unset, not the first of six.
    with pytest.raises(ConfigurationError) as raised:
        load(DEVIN_API_TOKEN=None, GITHUB_TOKEN=None, DATABASE_URL=None)

    message = str(raised.value)
    assert "DEVIN_API_TOKEN" in message
    assert "GITHUB_TOKEN" in message
    assert "DATABASE_URL" in message


def test_playbook_ids_parse_into_a_mapping_of_issue_class_to_playbook(load: Load) -> None:
    settings = load(DEVIN_PLAYBOOK_IDS='{"security": "playbook-sec", "flaky-test": "playbook-fl"}')

    assert settings.devin_playbook_ids == {
        "security": "playbook-sec",
        "flaky-test": "playbook-fl",
    }


def test_knowledge_ids_parse_into_a_sequence_in_the_documented_order(load: Load) -> None:
    settings = load(DEVIN_KNOWLEDGE_IDS='["note-1", "note-2"]')

    assert settings.devin_knowledge_ids == ("note-1", "note-2")


def test_the_parsed_containers_cannot_be_edited_in_place(load: Load) -> None:
    # Freezing the model stops a field being rebound, not the container behind it. This one decides
    # which playbook Devin runs, on an object every module in the process shares.
    settings = load()

    with pytest.raises(TypeError):
        settings.devin_playbook_ids["security"] = "hijacked"  # type: ignore[index]  # the point
    with pytest.raises(AttributeError):
        settings.devin_knowledge_ids.append("hijacked")  # type: ignore[attr-defined]  # the point

    assert settings.devin_playbook_ids["security"] == "playbook-sec"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DEVIN_PLAYBOOK_IDS", "not json at all"),
        ("DEVIN_PLAYBOOK_IDS", '{"security": "playbook-sec",}'),
        ("DEVIN_PLAYBOOK_IDS", '["playbook-sec"]'),  # a list, not a map
        ("DEVIN_PLAYBOOK_IDS", '{"security": 17}'),  # ids are strings
        ("DEVIN_KNOWLEDGE_IDS", "note-1,note-2"),  # comma-separated, not JSON
        ("DEVIN_KNOWLEDGE_IDS", '{"note": "note-1"}'),  # a map, not a list
        ("DEVIN_KNOWLEDGE_IDS", "[17]"),  # ids are strings
    ],
)
def test_malformed_json_variables_are_rejected(load: Load, variable: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load(**{variable: value})

    assert variable in str(raised.value)


@pytest.mark.parametrize(
    ("variable", "value", "located"),
    [
        (
            "DEVIN_PLAYBOOK_IDS",
            '{"security": "ok", "flaky-test": 17}',
            "DEVIN_PLAYBOOK_IDS.flaky-test",
        ),
        ("DEVIN_KNOWLEDGE_IDS", '["note-1", 17]', "DEVIN_KNOWLEDGE_IDS[1]"),
    ],
)
def test_a_bad_entry_is_located_within_its_variable(
    load: Load, variable: str, value: str, located: str
) -> None:
    # Naming the variable is not enough when it holds a dozen entries; the operator needs the one.
    with pytest.raises(ConfigurationError) as raised:
        load(**{variable: value})

    assert located in str(raised.value)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_a_database_url_naming_no_driver_has_the_asyncpg_one_applied(
    load: Load, scheme: str
) -> None:
    # What every managed provider issues, and what `fly postgres attach` writes into the app's
    # secrets by itself. Rejecting it would make a correct deployment crash-loop all three
    # processes over a prefix the operator never chose.
    settings = load(DATABASE_URL=f"{scheme}://sentinel:dbpassword@db.internal:5432/sentinel")

    assert settings.database_url.get_secret_value() == (
        "postgresql+asyncpg://sentinel:dbpassword@db.internal:5432/sentinel"
    )


def test_applying_the_driver_leaves_the_rest_of_the_url_alone(load: Load) -> None:
    # The query string carries `sslmode`, which a hosted database needs and which is the part most
    # easily lost to a naive rewrite.
    settings = load(DATABASE_URL="postgres://u:p@host:5432/db?sslmode=require&application_name=x")

    assert settings.database_url.get_secret_value() == (
        "postgresql+asyncpg://u:p@host:5432/db?sslmode=require&application_name=x"
    )


@pytest.mark.parametrize(
    "value",
    [
        "postgresql+psycopg2://sentinel:dbpassword@localhost:5432/sentinel",
        "postgresql+psycopg://sentinel:dbpassword@localhost:5432/sentinel",
        "mysql+aiomysql://sentinel:dbpassword@localhost:3306/sentinel",
        "sqlite+aiosqlite:///sentinel.db",
    ],
)
def test_a_database_url_naming_another_driver_is_rejected(load: Load, value: str) -> None:
    # A URL that names a driver is a deliberate choice of one, and the wrong choice fails deep
    # inside the engine on the first query instead of at startup.
    with pytest.raises(ConfigurationError) as raised:
        load(DATABASE_URL=value)

    assert "DATABASE_URL" in str(raised.value)
    assert "postgresql+asyncpg://" in str(raised.value)


@pytest.mark.parametrize("value", ["localhost:5432/sentinel", "sentinel", "postgres:/sentinel"])
def test_a_database_url_that_is_not_a_url_is_rejected(load: Load, value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load(DATABASE_URL=value)

    assert "DATABASE_URL" in str(raised.value)


@pytest.mark.parametrize(
    "value",
    [
        "postgres://sentinel:dbpassword@localhost:5432/sentinel",
        "postgresql+psycopg2://sentinel:dbpassword@localhost:5432/sentinel",
        "dbpassword",
    ],
    ids=["normalised", "rejected-driver", "not-a-url"],
)
def test_the_database_url_is_never_echoed_back_whatever_it_was(load: Load, value: str) -> None:
    # It is a DSN with a password in it, on the path a misconfigured deployment actually takes.
    # Accepting a URL must not put it in the startup log either, so the accepted one is checked
    # through the renderings rather than the error.
    try:
        settings = load(DATABASE_URL=value)
    except ConfigurationError as error:
        assert "dbpassword" not in str(error)
        assert value not in str(error)
        return

    for rendering in (repr(settings), str(settings.model_dump()), settings.model_dump_json()):
        assert "dbpassword" not in rendering


def test_the_database_url_survives_validation_intact(load: Load) -> None:
    settings = load()

    assert settings.database_url.get_secret_value() == REQUIRED["DATABASE_URL"]


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("MAX_CONCURRENT_SESSIONS", "7", 7),
        ("DAILY_ACU_BUDGET", "42.5", 42.5),
        ("MAX_FIX_CYCLES", "0", 0),
        ("MAX_JOB_ATTEMPTS", "1", 1),
        ("JOB_LEASE_TIMEOUT_SECONDS", "60", 60),
        ("POLL_INTERVAL_SECONDS", "5", 5),
        ("ACU_UNIT_COST_USD", "0", 0.0),
    ],
)
def test_numeric_variables_are_coerced_from_their_string_form(
    load: Load, variable: str, value: str, expected: float
) -> None:
    settings = load(**{variable: value})
    actual = getattr(settings, variable.lower())

    assert actual == expected
    assert isinstance(actual, type(expected))


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MAX_CONCURRENT_SESSIONS", "none"),
        ("MAX_CONCURRENT_SESSIONS", "0"),  # a cap of zero would run nothing at all
        ("MAX_JOB_ATTEMPTS", "0"),  # a job that is never attempted cannot fail either
        ("POLL_INTERVAL_SECONDS", "-1"),
        ("DAILY_ACU_BUDGET", "0"),  # no budget at all is a stopped pipeline, not a configuration
        ("ACU_UNIT_COST_USD", "-1"),
    ],
)
def test_nonsensical_numeric_values_are_rejected(load: Load, variable: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        load(**{variable: value})

    assert variable in str(raised.value)


def test_the_log_level_is_a_known_level_in_any_case(load: Load) -> None:
    assert load(LOG_LEVEL="WARNING").log_level == "warning"

    with pytest.raises(ConfigurationError) as raised:
        load(LOG_LEVEL="verbose")

    assert "LOG_LEVEL" in str(raised.value)


def test_secrets_are_masked_wherever_the_settings_are_rendered(load: Load) -> None:
    settings = load()
    secrets = [
        REQUIRED["DEVIN_API_TOKEN"],
        REQUIRED["GITHUB_TOKEN"],
        REQUIRED["GITHUB_WEBHOOK_SECRET"],
        "dbpassword",
    ]
    renderings = [
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
        f"{settings.devin_api_token} {settings.github_token} {settings.database_url}",
    ]

    for rendering in renderings:
        for secret in secrets:
            assert secret not in rendering, rendering

    # Masking is only useful if the value itself is still reachable deliberately.
    assert settings.devin_api_token.get_secret_value() == REQUIRED["DEVIN_API_TOKEN"]


def test_a_rejected_secret_is_not_quoted_back_in_the_error(load: Load) -> None:
    # pydantic's own ValidationError repeats the input it rejected, which for these variables is a
    # credential. Whatever `get_settings` raises must not carry it, by any route.
    with pytest.raises(ConfigurationError) as raised:
        load(
            DATABASE_URL="mysql://sentinel:dbpassword@localhost:3306/sentinel",
            DEVIN_PLAYBOOK_IDS="{",
            GITHUB_TOKEN="",
        )

    assert "dbpassword" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    # __context__ is the one that bites: `from None` does not clear it, and anything walking the
    # chain structurally rather than formatting it would find the whole rejected environment there.
    assert raised.value.__context__ is None


def test_a_missing_variable_does_not_expose_the_environment_that_was_read(load: Load) -> None:
    # pydantic reports a missing field with the entire input mapping — here, every other
    # credential — as the rejected value.
    with pytest.raises(ConfigurationError) as raised:
        load(GITHUB_WEBHOOK_SECRET=None)

    reachable = str(raised.value) + repr(raised.value.__context__) + repr(raised.value.__cause__)
    for secret in (REQUIRED["DEVIN_API_TOKEN"], REQUIRED["GITHUB_TOKEN"], "dbpassword"):
        assert secret not in reachable


def test_the_settings_cannot_be_reconfigured_after_loading(load: Load) -> None:
    # The same object is shared by the api, the worker and the poller, and assignment would run no
    # validator: an invalid driver, or a credential silently downgraded from SecretStr to str.
    settings = load()

    with pytest.raises(ValidationError):
        settings.database_url = "mysql://oops"  # type: ignore[assignment]  # the point of the test
    with pytest.raises(ValidationError):
        settings.max_concurrent_sessions = -5

    assert settings.database_url.get_secret_value() == REQUIRED["DATABASE_URL"]
    assert settings.max_concurrent_sessions == 3


def test_the_dotenv_file_is_read_and_the_environment_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                *(f"{name}={value}" for name, value in REQUIRED.items()),
                "TARGET_BASE_BRANCH=from-dotenv",
                "MAX_FIX_CYCLES=9",
                "POSTGRES_PORT=54321",  # Compose reads the same file; this is not ours
            ]
        )
    )
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAX_FIX_CYCLES", "4")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.devin_org_id == REQUIRED["DEVIN_ORG_ID"]
    assert settings.target_base_branch == "from-dotenv"
    assert settings.max_fix_cycles == 4


def test_the_blank_lines_in_env_example_do_not_configure_empty_values(load: Load) -> None:
    # `make db` copies `.env.example` verbatim, so every optional variable arrives as `NAME=`.
    settings = load(DEVIN_ENTERPRISE_ID="", DEVIN_KNOWLEDGE_IDS="", TARGET_REPO="")

    assert settings.devin_enterprise_id is None
    assert settings.devin_knowledge_ids == ()
    assert settings.target_repo == "taxpon/superset"


def test_the_settings_are_read_once_per_process(
    load: Load, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load()
    monkeypatch.setenv("TARGET_BASE_BRANCH", "changed-after-loading")

    assert get_settings() is settings

    get_settings.cache_clear()
    assert get_settings().target_base_branch == "changed-after-loading"


def test_the_playbook_map_accepts_the_shape_the_example_documents(load: Load) -> None:
    # The comment in `.env.example` shows the format an operator is expected to paste in.
    documented = json.dumps({"security": "playbook-abc", "bug": "playbook-def"})

    assert load(DEVIN_PLAYBOOK_IDS=documented).devin_playbook_ids["bug"] == "playbook-def"
