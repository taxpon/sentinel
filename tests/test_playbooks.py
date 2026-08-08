"""`docs/05-devin-integration.md` specifies the playbooks, the schema, the tags and the prompts as
tables and literal blocks, and reviewers check the same values in the Devin dashboard. These tests
therefore read the spec and compare against it, so the code cannot drift from the document without
one of them failing."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sentinel.devin import playbooks as pb

DOCS = Path(__file__).resolve().parents[1] / "docs"
SPEC_TEXT = (DOCS / "05-devin-integration.md").read_text()
OVERVIEW = (DOCS / "01-overview.md").read_text()
STATE_MACHINE = (DOCS / "04-state-machine.md").read_text()

JSON_SCHEMA_KEYWORDS = {"object", "array", "string", "number", "integer", "boolean", "null"}


# --- Spec parsing --------------------------------------------------------------------------------


def section(heading: str, spec: str = SPEC_TEXT) -> str:
    """The body of one `## ` section of a spec document."""
    body: list[str] = []
    found = False
    for line in spec.splitlines():
        if line.startswith("## "):
            if found:
                break
            found = line.strip() == f"## {heading}"
            continue
        if found:
            body.append(line)
    assert found, f"spec has no section {heading!r}"
    return "\n".join(body)


def table_rows(text: str) -> list[list[str]]:
    """Data rows of the first Markdown table in `text`, as lists of cells."""
    rows = [line for line in text.splitlines() if line.startswith("|")]
    return [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in rows
        if not set(line) <= set("|- ")
    ][1:]


def fenced_blocks(text: str) -> list[str]:
    return re.findall(r"^```[a-z]*\n(.*?)^```", text, re.MULTILINE | re.DOTALL)


def backticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


SPEC_PLAYBOOK_ROWS = table_rows(section("Playbooks and ACU caps"))
SPEC_TAG_ROWS = table_rows(section("Tag vocabulary"))
SPEC_PROMPT, SPEC_CI_RESUME, SPEC_CHANGES_REQUESTED, SPEC_CYCLE_NOTICE = fenced_blocks(
    section("Prompt construction")
)


# --- Playbooks, caps and baselines ---------------------------------------------------------------


def test_spec_tables_were_parsed() -> None:
    """A parsing bug would silently turn every table-driven test below into a no-op."""
    assert len(SPEC_PLAYBOOK_ROWS) == 4
    assert len(SPEC_TAG_ROWS) == 7


@pytest.mark.parametrize("row", SPEC_PLAYBOOK_ROWS, ids=lambda row: backticked(row[0])[0])
def test_playbook_row_matches_the_spec(row: list[str]) -> None:
    name, classes, cap, hours = backticked(row[0])[0], backticked(row[1]), row[2], row[3]
    for issue_class in classes:
        playbook = pb.playbook_for(issue_class)
        assert playbook.name == name
        assert playbook.max_acu_limit == int(cap)
        assert playbook.baseline_hours == float(hours)
        assert pb.acu_cap_for(issue_class) == playbook.max_acu_limit
        assert pb.baseline_hours_for(issue_class) == playbook.baseline_hours


def test_every_issue_class_has_a_playbook() -> None:
    assert set(pb.PLAYBOOK_BY_CLASS) == set(pb.IssueClass)


def test_the_classes_covered_are_exactly_the_classes_the_spec_lists() -> None:
    spec_classes = {name for row in SPEC_PLAYBOOK_ROWS for name in backticked(row[1])}
    assert spec_classes == {c.value for c in pb.IssueClass}


def test_the_issue_classes_are_the_ones_the_overview_defines() -> None:
    """`docs/01-overview.md` is where the classes are defined and where the `class:` labels on the
    target repository come from; the playbook table in `docs/05` only has to cover them."""
    rows = table_rows(section("Issue classes", spec=OVERVIEW))
    assert {backticked(row[0])[0] for row in rows} == {c.value for c in pb.IssueClass}


def test_playbooks_are_the_four_the_spec_names() -> None:
    assert {p.name for p in pb.PLAYBOOKS} == {backticked(row[0])[0] for row in SPEC_PLAYBOOK_ROWS}
    assert set(pb.PLAYBOOK_BY_CLASS.values()) == set(pb.PLAYBOOKS)


@pytest.mark.parametrize(
    "unknown", ["", "Security", "security ", "docs", "class:security", "flaky_test"]
)
def test_an_unknown_class_fails_loudly_rather_than_defaulting(unknown: str) -> None:
    for call in (pb.playbook_for, pb.acu_cap_for, pb.baseline_hours_for, pb.parse_issue_class):
        with pytest.raises(pb.UnknownIssueClass, match="unknown issue class"):
            call(unknown)


def test_the_unknown_class_error_lists_what_is_accepted() -> None:
    """The worker puts this message on the issue when it blocks the remediation."""
    with pytest.raises(pb.UnknownIssueClass) as excinfo:
        pb.playbook_for("chore")
    assert all(c.value in str(excinfo.value) for c in pb.IssueClass)


def test_playbook_id_resolves_from_an_issue_class_key() -> None:
    ids = {"security": "playbook-aaa", "bug": "playbook-bbb"}
    assert pb.playbook_id_for("security", ids) == "playbook-aaa"
    assert pb.playbook_id_for("bug", ids) == "playbook-bbb"


def test_playbook_id_falls_back_to_the_playbook_name_key() -> None:
    ids = {"security-fix": "playbook-aaa", "dep-upgrade": "playbook-bbb"}
    assert pb.playbook_id_for("security", ids) == "playbook-aaa"
    assert pb.playbook_id_for("bug", ids) == "playbook-aaa"
    assert pb.playbook_id_for("frontend-dep", ids) == "playbook-bbb"


def test_an_issue_class_key_wins_over_the_playbook_name_key() -> None:
    ids = {"security-fix": "playbook-shared", "bug": "playbook-special"}
    assert pb.playbook_id_for("bug", ids) == "playbook-special"
    assert pb.playbook_id_for("security", ids) == "playbook-shared"


def test_a_missing_playbook_id_names_both_keys_that_would_have_worked() -> None:
    with pytest.raises(pb.MissingPlaybookId) as excinfo:
        pb.playbook_id_for("perf", {"security": "playbook-aaa"})
    assert "'perf'" in str(excinfo.value)
    assert "'deprecation'" in str(excinfo.value)


def test_a_missing_playbook_id_is_not_an_unrecognised_issue_class() -> None:
    """An unrecognised class escalates to a human (`QUEUED -> BLOCKED`, `docs/04-state-machine.md`).
    A missing config key is a deployment fault: the class is known, the id was never supplied. The
    two must not be caught by the same `except`, or one typo escalates every issue of that class."""
    assert not issubclass(pb.MissingPlaybookId, pb.UnknownIssueClass)
    with pytest.raises(pb.MissingPlaybookId):
        pb.playbook_id_for("perf", {})
    assert pb.playbook_for("perf").name == "deprecation", "the class itself is recognised"


def test_an_unknown_class_is_rejected_before_the_configured_ids_are_consulted() -> None:
    with pytest.raises(pb.UnknownIssueClass, match="unknown issue class"):
        pb.playbook_id_for("chore", {"chore": "playbook-aaa"})


# --- Structured output ---------------------------------------------------------------------------


def test_schema_is_the_spec_schema() -> None:
    (block,) = fenced_blocks(section("Structured output"))
    assert json.loads(block) == pb.STRUCTURED_OUTPUT_SCHEMA


def test_required_fields_match_the_spec() -> None:
    assert pb.STRUCTURED_OUTPUT_SCHEMA["required"] == [
        "outcome",
        "root_cause",
        "changes",
        "tests",
        "risk",
    ]
    assert pb.STRUCTURED_OUTPUT_SCHEMA["properties"]["tests"]["required"] == [
        "added",
        "command",
        "passed",
    ]


def test_optional_fields_are_present_but_not_required() -> None:
    """`blocked_reason`, `pr_url` and `confidence` drive panels; requiring them would fail a
    session that simply had nothing to report there."""
    properties = pb.STRUCTURED_OUTPUT_SCHEMA["properties"]
    for field in ("blocked_reason", "pr_url", "confidence"):
        assert field in properties
        assert field not in pb.STRUCTURED_OUTPUT_SCHEMA["required"]


def test_schema_is_a_valid_json_schema_object() -> None:
    schema = pb.STRUCTURED_OUTPUT_SCHEMA
    assert json.loads(json.dumps(schema)) == schema, "must survive the request body round trip"

    def check(node: dict[str, object], path: str) -> None:
        if "type" in node:
            assert node["type"] in JSON_SCHEMA_KEYWORDS, path
        if "enum" in node:
            assert isinstance(node["enum"], list) and node["enum"], path
        properties = node.get("properties", {})
        assert isinstance(properties, dict), path
        for name in node.get("required", []):
            assert name in properties, f"{path}: required {name!r} is not a declared property"
        if node.get("type") == "array":
            assert "items" in node, path
        for name, child in properties.items():
            assert isinstance(child, dict), f"{path}.{name}"
            check(child, f"{path}.{name}")

    check(schema, "$")


def test_outcome_enum_covers_the_states_the_pipeline_reacts_to() -> None:
    outcome = pb.STRUCTURED_OUTPUT_SCHEMA["properties"]["outcome"]
    assert outcome["enum"] == ["fixed", "partial", "blocked"]
    assert pb.STRUCTURED_OUTPUT_SCHEMA["properties"]["risk"]["enum"] == ["low", "medium", "high"]


def test_structured_output_is_required() -> None:
    assert pb.STRUCTURED_OUTPUT_REQUIRED is True


# --- Tags ----------------------------------------------------------------------------------------


def test_session_tags_are_the_creation_time_tags_in_spec_order() -> None:
    assert pb.session_tags(
        repo="taxpon/superset",
        issue_number=42,
        issue_class=pb.IssueClass.SECURITY,
        delivery_id="8f1c2d3e-4b5a",
    ) == [
        "sentinel",
        "repo:taxpon/superset",
        "issue:42",
        "class:security",
        "run:8f1c2d3e-4b5a",
    ]


def test_the_exported_vocabulary_is_what_validation_enforces() -> None:
    """`make bootstrap-devin` registers `NAMESPACE_TAG` and `TAG_PREFIXES` with
    `PUT /v3/organizations/{org_id}/tags`. If the bootstrap script could register a list that
    validation does not enforce — or the reverse — the mismatch would surface as a 422 at session
    creation (B7), so both come off the same mapping."""
    assert tuple(pb.TAG_VALUE_PATTERNS) == pb.TAG_PREFIXES
    for prefix in pb.TAG_PREFIXES:
        with pytest.raises(pb.UnregisteredTag, match="value"):
            # Reaching the *value* complaint proves the prefix itself was accepted.
            pb.validate_tag(f"{prefix}:")


def test_a_prefix_outside_the_exported_vocabulary_is_refused() -> None:
    assert "priority" not in pb.TAG_PREFIXES
    with pytest.raises(pb.UnregisteredTag, match="outside the vocabulary"):
        pb.validate_tag("priority:high")


def test_every_tag_sentinel_sends_comes_from_the_exported_vocabulary() -> None:
    sent = [
        *pb.session_tags(**VALID_TAG_INPUTS),
        pb.cycle_tag(2),
        pb.outcome_tag("merged"),
    ]
    for tag in sent:
        prefix = tag.partition(":")[0]
        assert tag == pb.NAMESPACE_TAG or prefix in pb.TAG_PREFIXES


def test_the_vocabulary_covers_every_tag_the_spec_tabulates() -> None:
    for row in SPEC_TAG_ROWS:
        # The `run:` example is elided in the spec (`run:8f1c…`); the ellipsis stands for the rest
        # of the delivery id, not for a character a tag may contain.
        example = backticked(row[1])[0].replace("…", "")
        assert pb.validate_tag(example) == example


def test_lifecycle_tags_are_appended_in_the_spec_form() -> None:
    assert pb.cycle_tag(2) == "cycle:2"
    assert pb.outcome_tag("merged") == "outcome:merged"


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "Sentinel",
        "sentinel:",
        "cog",
        "issue",
        "issue:",
        "issue:0",
        "issue:-1",
        "issue:forty-two",
        "class:chore",
        "class:Security",
        "class:",
        "repo:superset",
        "repo:taxpon/superset/extra",
        "run:",
        "run:8f1c 2d3e",
        "cycle:0",
        "outcome:done",
        "outcome:MERGED",
        "priority:high",
    ],
)
def test_a_tag_outside_the_registered_vocabulary_is_rejected(tag: str) -> None:
    """An unregistered tag is a 422 at session creation (B7), so it is caught before it is sent."""
    with pytest.raises(pb.UnregisteredTag):
        pb.validate_tag(tag)


VALID_TAG_INPUTS = {
    "repo": "taxpon/superset",
    "issue_number": 42,
    "issue_class": "security",
    "delivery_id": "8f1c2d3e",
}


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"repo": "superset"}, "repo"),
        ({"issue_number": 0}, "issue"),
        ({"delivery_id": ""}, "run"),
        ({"delivery_id": "has spaces"}, "run"),
    ],
)
def test_session_tags_reject_values_the_vocabulary_would_not_accept(
    kwargs: dict[str, object], field: str
) -> None:
    with pytest.raises(pb.UnregisteredTag, match=field):
        pb.session_tags(**{**VALID_TAG_INPUTS, **kwargs})


def test_an_unknown_class_reaches_session_tags_as_an_unknown_class() -> None:
    """`QUEUED -> BLOCKED` on an unrecognised class is one state-machine edge, so the worker should
    not have to catch a second exception type to implement it."""
    with pytest.raises(pb.UnknownIssueClass, match="unknown issue class"):
        pb.session_tags(**{**VALID_TAG_INPUTS, "issue_class": "chore"})


@pytest.mark.parametrize("cycle", [0, -1])
def test_a_cycle_tag_starts_at_one(cycle: int) -> None:
    with pytest.raises(pb.UnregisteredTag):
        pb.cycle_tag(cycle)


def test_outcome_tags_are_the_terminal_states_of_the_state_machine() -> None:
    """`outcome:<state>` is appended on reaching a terminal state, so the accepted values are
    whatever `docs/04-state-machine.md` marks terminal — read from there, not restated here."""
    states = table_rows(section("States", spec=STATE_MACHINE))
    terminal = {backticked(row[0])[0].lower() for row in states if "**Terminal" in row[1]}
    assert terminal, "the state table was not parsed"
    assert terminal == pb.OUTCOMES
    for outcome in pb.OUTCOMES:
        assert pb.outcome_tag(outcome) == f"outcome:{outcome}"


def test_session_title_identifies_sentinel_and_the_issue() -> None:
    assert pb.session_title(42, "Fix the cache key collision") == (
        "[sentinel] #42 Fix the cache key collision"
    )


# --- Prompts -------------------------------------------------------------------------------------

ISSUE = {
    "issue_number": 42,
    "issue_title": "Cache key ignores the schema, so two datasources collide",
    "issue_body": "Steps to reproduce:\n1. Query dataset A\n2. Query dataset B",
    "repo": "taxpon/superset",
    "base_branch": "master",
}


def test_initial_prompt_renders_the_spec_template() -> None:
    expected = (
        SPEC_PROMPT.strip()
        .replace("{number}", str(ISSUE["issue_number"]))
        .replace("{repo}", ISSUE["repo"])
        .replace("{title}", ISSUE["issue_title"])
        .replace("{issue body}", ISSUE["issue_body"])
        .replace("{base_branch}", ISSUE["base_branch"])
    )
    assert pb.initial_prompt(**ISSUE) == expected


def test_initial_prompt_carries_the_issue_and_the_acceptance_criteria() -> None:
    prompt = pb.initial_prompt(**ISSUE)
    assert prompt.startswith("GitHub issue #42 in taxpon/superset: Cache key ignores the schema")
    assert ISSUE["issue_body"] in prompt
    assert "Diagnose the underlying cause and fix it." in prompt
    assert "A regression test that fails before your change and passes after it." in prompt
    assert 'report outcome "blocked"' in prompt


def test_the_base_branch_is_never_hard_coded() -> None:
    """The fork's default branch is `master` (B3); a literal `main` would target nothing."""
    prompt = pb.initial_prompt(**{**ISSUE, "base_branch": "release-4.0"})
    assert prompt.count("A branch off release-4.0 with the fix.") == 1
    assert prompt.count("A pull request against release-4.0") == 1
    assert "master" not in prompt


def test_the_prompt_delegates_the_task_and_not_the_steps() -> None:
    """The rule in `docs/adr/2026-08-07-delegate-task-not-steps.md`: no instruction on how to
    investigate or implement, and no repository facts that belong in a knowledge note."""
    prompt = pb.initial_prompt(**ISSUE).lower()
    for step in ("git checkout", "open the file", "run pytest", "first,", "step 1"):
        assert step not in prompt


def test_an_issue_with_no_body_still_renders_a_complete_prompt() -> None:
    prompt = pb.initial_prompt(**{**ISSUE, "issue_body": "   \n\n  "})
    assert pb.NO_ISSUE_BODY in prompt
    assert "\n\n\n" not in prompt


# --- Resume messages -----------------------------------------------------------------------------


def cycle_notice(cycle: int, max_cycles: int) -> str:
    """The spec's cycle-notice block, rendered — both resume messages end with it."""
    return (
        SPEC_CYCLE_NOTICE.strip()
        .replace("{cycle}", str(cycle))
        .replace("{max_cycles}", str(max_cycles))
    )


def test_ci_failure_message_renders_the_spec_template() -> None:
    log = "E   assert 1 == 2\nFAILED tests/test_cache.py::test_key"
    expected = (
        SPEC_CI_RESUME.strip()
        .replace("{sha}", "abc1234")
        .replace("{job_name}", "python-tests")
        .replace("{log excerpt, last 100 lines}", log)
    )
    message = pb.ci_failure_message(
        sha="abc1234", job_name="python-tests", log=log, cycle=1, max_cycles=3
    )
    assert message == f"{expected}\n\n{cycle_notice(1, 3)}"


def test_changes_requested_message_renders_the_spec_template() -> None:
    review = "This widens the cache key.\n\nsuperset/common/query_context.py:120 — use the schema"
    expected = (
        SPEC_CHANGES_REQUESTED.strip()
        .replace("{pr_url}", "https://github.com/taxpon/superset/pull/7")
        .replace("{review body, then inline comments}", review)
    )
    message = pb.changes_requested_message(
        pr_url="https://github.com/taxpon/superset/pull/7",
        review_body="This widens the cache key.",
        inline_comments=["superset/common/query_context.py:120 — use the schema"],
        cycle=2,
        max_cycles=3,
    )
    assert message == f"{expected}\n\n{cycle_notice(2, 3)}"


def test_the_empty_substitution_fallbacks_are_the_ones_the_spec_names() -> None:
    """A reviewer diffing the spec against a real session should find the same parentheticals."""
    prose = section("Prompt construction")
    for fallback in (pb.NO_LOG_OUTPUT, pb.NO_REVIEW_FEEDBACK, pb.NO_ISSUE_BODY):
        assert f"`{fallback}`" in prose


def test_ci_failure_message_carries_the_cycle_and_the_failure_context() -> None:
    message = pb.ci_failure_message(
        sha="abc1234",
        job_name="python-tests",
        log="FAILED tests/test_cache.py::test_key",
        cycle=2,
        max_cycles=3,
    )
    assert "abc1234" in message
    assert "python-tests" in message
    assert "FAILED tests/test_cache.py::test_key" in message
    assert "fix cycle 2 of 3" in message
    assert 'report outcome "blocked"' in message


@pytest.mark.parametrize("log", ["", "   ", "\n\n", "\t\n  \n"])
def test_a_failing_job_with_no_log_output_says_so_rather_than_leaving_a_hole(log: str) -> None:
    """A job that fails during setup, or a log fetch that returns nothing, would otherwise leave a
    blank gap where the evidence belongs — the same defect `NO_ISSUE_BODY` guards in the prompt."""
    message = pb.ci_failure_message(
        sha="abc1234", job_name="python-tests", log=log, cycle=1, max_cycles=3
    )
    assert pb.NO_LOG_OUTPUT in message
    assert "\n\n\n" not in message


def test_a_log_excerpt_never_leaves_a_blank_run_in_the_message() -> None:
    message = pb.ci_failure_message(
        sha="abc1234",
        job_name="python-tests",
        log="\n\nFAILED tests/test_cache.py::test_key\n\n",
        cycle=1,
        max_cycles=3,
    )
    assert "\n\n\n" not in message


def test_a_long_log_is_cut_to_the_last_hundred_lines() -> None:
    log = "\n".join(f"line {n}" for n in range(500))
    message = pb.ci_failure_message(
        sha="abc1234", job_name="python-tests", log=log, cycle=1, max_cycles=3
    )
    assert "line 499" in message
    assert "line 400" in message
    assert "line 399" not in message


def test_changes_requested_message_carries_the_review() -> None:
    message = pb.changes_requested_message(
        pr_url="https://github.com/taxpon/superset/pull/7",
        review_body="This widens the cache key more than necessary.",
        inline_comments=["superset/common/query_context.py:120 — use the schema, not the database"],
        cycle=1,
        max_cycles=3,
    )
    assert "https://github.com/taxpon/superset/pull/7" in message
    assert "This widens the cache key more than necessary." in message
    assert "superset/common/query_context.py:120" in message
    assert "Address the review and push a fix to the same branch." in message
    assert "fix cycle 1 of 3" in message


def test_a_review_that_says_everything_inline_still_forwards_the_comments() -> None:
    message = pb.changes_requested_message(
        pr_url="https://github.com/taxpon/superset/pull/7",
        review_body="",
        inline_comments=["tests/test_cache.py:12 — this assertion does not fail before the fix"],
        cycle=1,
        max_cycles=3,
    )
    assert "tests/test_cache.py:12" in message
    assert pb.NO_REVIEW_FEEDBACK not in message


def test_review_parts_that_arrive_with_stray_whitespace_do_not_open_a_blank_run() -> None:
    message = pb.changes_requested_message(
        pr_url="https://github.com/taxpon/superset/pull/7",
        review_body="This widens the cache key more than necessary.\n\n",
        inline_comments=["\nsuperset/common/query_context.py:120 — use the schema\n"],
        cycle=1,
        max_cycles=3,
    )
    assert "\n\n\n" not in message
    assert "superset/common/query_context.py:120 — use the schema" in message


def test_a_review_with_no_written_feedback_says_so_rather_than_leaving_a_hole() -> None:
    message = pb.changes_requested_message(
        pr_url="https://github.com/taxpon/superset/pull/7",
        review_body="  ",
        cycle=1,
        max_cycles=3,
    )
    assert pb.NO_REVIEW_FEEDBACK in message
    assert "\n\n\n" not in message


def test_resume_messages_delegate_the_fix() -> None:
    """The state machine is explicit that the message states the failure and the goal, and does not
    prescribe the fix."""
    message = pb.ci_failure_message(
        sha="abc1234", job_name="python-tests", log="boom", cycle=1, max_cycles=3
    ).lower()
    for step in ("revert", "change line", "instead, use", "you should edit"):
        assert step not in message


@pytest.mark.parametrize(("cycle", "max_cycles"), [(0, 3), (-1, 3), (4, 3)])
def test_a_cycle_outside_the_budget_is_a_programming_error(cycle: int, max_cycles: int) -> None:
    """`cycle > MAX_FIX_CYCLES` forces `FAILED`; the worker must not be resuming at all."""
    with pytest.raises(ValueError, match="cycle"):
        pb.ci_failure_message(
            sha="abc1234", job_name="python-tests", log="boom", cycle=cycle, max_cycles=max_cycles
        )
    with pytest.raises(ValueError, match="cycle"):
        pb.changes_requested_message(
            pr_url="https://example.invalid/pull/7",
            review_body="please fix",
            cycle=cycle,
            max_cycles=max_cycles,
        )
