"""The subscribed-events table of `docs/06-event-pipeline.md`, executed.

Every case starts from a recorded payload in `tests/fixtures/github/`, because a hand-written dict
proves only that the mapping reads the keys the test author remembered. The rows outnumber the
recordings — there is no `issues.unlabeled` file and no unmerged `pull_request.closed` — so the
missing ones are *derived* from a recording with `variant()`, which keeps the envelope, the
repository and every object GitHub sends and changes only the field the row is about.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import pytest

from factories import GITHUB_PAYLOADS, github_payload
from sentinel.config import Settings
from sentinel.github.events import (
    DEVIN_BOT_MENTION,
    Intent,
    MappedEvent,
    Reason,
    map_event,
    trigger_for,
)
from sentinel.pipeline.state import State, Trigger, transition

ABSENT: Final = object()
"""Marks a field `variant()` should remove rather than replace."""

Build = Callable[[], dict[str, Any]]


def variant(name: str, **overrides: Any) -> dict[str, Any]:
    """A recorded payload with fields replaced, addressed by `__`-separated path.

    variant("issues.labeled", action="closed", label=ABSENT)
    variant("pull_request.closed.merged", pull_request__merged=False)
    """
    body = github_payload(name)
    for path, value in overrides.items():
        *parents, leaf = path.split("__")
        target: dict[str, Any] = body
        for part in parents:
            target = target[part]
        if value is ABSENT:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return body


def a_ping() -> dict[str, Any]:
    """The delivery GitHub sends when the webhook is created. There is no recording of one, so it
    is assembled around a recorded `repository`, which is the only part the mapping reads."""
    return {
        "zen": "Non-blocking is better than blocking.",
        "hook_id": 580_318_446,
        "repository": github_payload("issues.labeled")["repository"],
    }


# ------------------------------------------------------- every row of the subscribed-events table


@dataclass(frozen=True)
class Row:
    """One row of the subscribed-events table, and the delivery that exercises it."""

    id: str
    event: str
    build: Build
    intent: Intent
    trigger: Trigger | None = None
    reason: Reason | None = None


SUBSCRIBED: Final = [
    Row(
        id="issues.labeled",
        event="issues",
        build=lambda: github_payload("issues.labeled"),
        intent=Intent.START_REMEDIATION,
        trigger=Trigger.ISSUE_LABELLED,
    ),
    Row(
        id="issues.unlabeled",
        event="issues",
        build=lambda: variant("issues.labeled", action="unlabeled"),
        intent=Intent.CANCEL,
        trigger=Trigger.FAILED,
        reason=Reason.AUTOFIX_LABEL_REMOVED,
    ),
    Row(
        id="issues.closed",
        event="issues",
        build=lambda: variant(
            "issues.labeled",
            action="closed",
            label=ABSENT,
            issue__state="closed",
            issue__state_reason="not_planned",
        ),
        intent=Intent.CANCEL,
        trigger=Trigger.FAILED,
        reason=Reason.ISSUE_CLOSED,
    ),
    Row(
        id="pull_request.opened",
        event="pull_request",
        build=lambda: github_payload("pull_request.opened"),
        intent=Intent.LINK_PULL_REQUEST,
        trigger=Trigger.PR_OPENED,
    ),
    Row(
        id="pull_request.closed-merged",
        event="pull_request",
        build=lambda: github_payload("pull_request.closed.merged"),
        intent=Intent.PULL_REQUEST_MERGED,
        trigger=Trigger.PR_MERGED,
    ),
    Row(
        id="pull_request.closed-unmerged",
        event="pull_request",
        build=lambda: variant(
            "pull_request.closed.merged",
            pull_request__merged=False,
            pull_request__merged_at=None,
            pull_request__merge_commit_sha=None,
        ),
        intent=Intent.PULL_REQUEST_ABANDONED,
        trigger=Trigger.FAILED,
        reason=Reason.PULL_REQUEST_CLOSED_UNMERGED,
    ),
    Row(
        id="pull_request_review.changes_requested",
        event="pull_request_review",
        build=lambda: github_payload("pull_request_review.submitted.changes_requested"),
        intent=Intent.RESUME_FOR_REVIEW,
        trigger=Trigger.CHANGES_REQUESTED,
    ),
    Row(
        id="pull_request_review.approved",
        event="pull_request_review",
        build=lambda: variant(
            "pull_request_review.submitted.changes_requested",
            review__state="APPROVED",
            review__body="LGTM",
        ),
        intent=Intent.RECORD_REVIEW_APPROVAL,
    ),
    Row(
        id="check_suite.requested",
        event="check_suite",
        build=lambda: variant(
            "check_suite.completed.success",
            action="requested",
            check_suite__status="queued",
            check_suite__conclusion=None,
        ),
        intent=Intent.CI_STARTED,
        trigger=Trigger.CHECK_SUITE_REQUESTED,
    ),
    Row(
        id="check_suite.completed-success",
        event="check_suite",
        build=lambda: github_payload("check_suite.completed.success"),
        intent=Intent.CI_PASSED,
        trigger=Trigger.CHECK_SUITE_SUCCEEDED,
    ),
    Row(
        id="check_suite.completed-failure",
        event="check_suite",
        build=lambda: github_payload("check_suite.completed.failure"),
        intent=Intent.RESUME_FOR_CI_FAILURE,
        trigger=Trigger.CHECK_SUITE_FAILED,
    ),
    Row(
        id="check_suite.completed-timed_out",
        event="check_suite",
        build=lambda: variant("check_suite.completed.failure", check_suite__conclusion="timed_out"),
        intent=Intent.RESUME_FOR_CI_FAILURE,
        trigger=Trigger.CHECK_SUITE_FAILED,
    ),
    Row(
        id="issue_comment.created-mentioning-the-bot",
        event="issue_comment",
        build=lambda: github_payload("issue_comment.created"),
        intent=Intent.FORWARD_COMMENT,
    ),
    Row(id="ping", event="ping", build=a_ping, intent=Intent.IGNORED, reason=Reason.PING),
]


@pytest.mark.parametrize("row", SUBSCRIBED, ids=lambda row: row.id)
def test_every_subscribed_event_maps_to_its_intent(row: Row, settings: Settings) -> None:
    mapped = map_event(row.event, row.build(), settings=settings)

    assert (mapped.intent, mapped.trigger, mapped.reason) == (row.intent, row.trigger, row.reason)


def test_the_table_accounts_for_every_intent() -> None:
    """No member of `Intent` exists that no subscribed event produces, and none is missing a row."""
    assert {row.intent for row in SUBSCRIBED} == set(Intent)


@pytest.mark.parametrize("name", GITHUB_PAYLOADS)
def test_every_recorded_payload_is_recognised(name: str, settings: Settings) -> None:
    """The recordings are one per acting row of the table, so none of them may be ignored — this is
    what catches a payload whose real shape the mapping reads through the wrong key."""
    mapped = map_event(name.split(".")[0], github_payload(name), settings=settings)

    assert not mapped.ignored, f"{name} was ignored: {mapped.reason}"


# --------------------------------------------------------------------- what the delivery is about


def test_a_labelled_issue_carries_the_issue_it_is_about(settings: Settings) -> None:
    mapped = map_event("issues", github_payload("issues.labeled"), settings=settings)

    assert (mapped.repo, mapped.issue_number) == ("taxpon/superset", 42876)


def test_an_opened_pull_request_carries_what_linking_it_needs(settings: Settings) -> None:
    mapped = map_event("pull_request", github_payload("pull_request.opened"), settings=settings)

    assert mapped.pr_number == 42889
    assert mapped.pr_url == "https://github.com/taxpon/superset/pull/42889"
    assert mapped.head_sha == "349a7f639dfb353669c001187706d7fd0112ed2f"


def test_a_check_suite_is_resolved_through_its_pull_request(settings: Settings) -> None:
    """`remediation` stores `pr_number`, not a head SHA, so that is the key the lookup needs."""
    mapped = map_event(
        "check_suite", github_payload("check_suite.completed.failure"), settings=settings
    )

    assert mapped.pr_number == 42889
    assert mapped.head_sha == "349a7f639dfb353669c001187706d7fd0112ed2f"


def test_a_check_suite_attached_to_no_pull_request_is_ignored(settings: Settings) -> None:
    body = variant("check_suite.completed.success", check_suite__pull_requests=[])

    mapped = map_event("check_suite", body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.NO_SUBJECT)


# ------------------------------------------------------------------------------ what is ignored


@pytest.mark.parametrize("event", ["deployment_status", "workflow_run", "push", ""])
def test_an_unknown_event_is_ignored_rather_than_an_error(event: str, settings: Settings) -> None:
    mapped = map_event(event, github_payload("issues.labeled"), settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.UNKNOWN_EVENT)


@pytest.mark.parametrize(
    ("event", "build"),
    [
        pytest.param(
            "issues", lambda: variant("issues.labeled", action="reopened"), id="issues.reopened"
        ),
        pytest.param(
            "pull_request",
            lambda: variant("pull_request.opened", action="synchronize"),
            id="pull_request.synchronize",
        ),
        pytest.param(
            "pull_request_review",
            lambda: variant("pull_request_review.submitted.changes_requested", action="dismissed"),
            id="pull_request_review.dismissed",
        ),
        pytest.param(
            "check_suite",
            lambda: variant("check_suite.completed.success", action="rerequested"),
            id="check_suite.rerequested",
        ),
        pytest.param(
            "issue_comment",
            lambda: variant("issue_comment.created", action="deleted"),
            id="issue_comment.deleted",
        ),
    ],
)
def test_a_subscribed_event_with_an_unhandled_action_is_ignored(
    event: str, build: Build, settings: Settings
) -> None:
    mapped = map_event(event, build(), settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.UNHANDLED_ACTION)


@pytest.mark.parametrize("name", GITHUB_PAYLOADS)
def test_a_payload_from_another_repository_is_ignored(name: str, settings: Settings) -> None:
    """`TARGET_REPO` is the whole point: nothing else may steer this pipeline, whatever it sends."""
    body = variant(name, repository__full_name="apache/superset")

    mapped = map_event(name.split(".")[0], body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.OTHER_REPOSITORY)


@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
def test_a_label_that_is_not_the_autofix_label_is_ignored(action: str, settings: Settings) -> None:
    body = variant("issues.labeled", action=action, label__name="deploy:helm")

    mapped = map_event("issues", body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.OTHER_LABEL)


def test_the_autofix_label_is_the_configured_one(settings: Settings) -> None:
    """Not the constant in the payload: a deployment that renames the label keeps working."""
    renamed = settings.model_copy(update={"autofix_label": "sentinel:fix"})

    assert map_event("issues", github_payload("issues.labeled"), settings=renamed).ignored
    body = variant("issues.labeled", label__name="sentinel:fix")
    assert map_event("issues", body, settings=renamed).intent is Intent.START_REMEDIATION


@pytest.mark.parametrize("conclusion", ["cancelled", "neutral", "skipped", "stale", None])
def test_a_check_suite_conclusion_the_spec_does_not_list_is_ignored(
    conclusion: str | None, settings: Settings
) -> None:
    body = variant("check_suite.completed.success", check_suite__conclusion=conclusion)

    mapped = map_event("check_suite", body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.UNHANDLED_CONCLUSION)


@pytest.mark.parametrize("state", ["COMMENTED", "PENDING", "DISMISSED"])
def test_a_review_state_that_is_neither_approval_nor_changes_is_ignored(
    state: str, settings: Settings
) -> None:
    body = variant("pull_request_review.submitted.changes_requested", review__state=state)

    mapped = map_event("pull_request_review", body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.UNHANDLED_REVIEW_STATE)


def test_a_review_state_is_read_in_either_casing(settings: Settings) -> None:
    """The webhook sends it lower case; the recordings were reassembled from API responses, which
    send it upper case."""
    body = variant(
        "pull_request_review.submitted.changes_requested", review__state="changes_requested"
    )

    mapped = map_event("pull_request_review", body, settings=settings)

    assert mapped.intent is Intent.RESUME_FOR_REVIEW


def test_a_comment_that_does_not_mention_the_bot_is_ignored(settings: Settings) -> None:
    body = variant("issue_comment.created", comment__body="Any progress on this?")

    mapped = map_event("issue_comment", body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.NO_MENTION)


def test_a_mention_is_recognised_whatever_its_casing(settings: Settings) -> None:
    body = variant("issue_comment.created", comment__body=f"{DEVIN_BOT_MENTION.title()} ping?")

    mapped = map_event("issue_comment", body, settings=settings)

    assert mapped.intent is Intent.FORWARD_COMMENT


@pytest.mark.parametrize(
    ("event", "body"),
    [
        pytest.param("issues", {}, id="empty-body"),
        pytest.param("issues", {"action": "labeled"}, id="no-label-and-no-issue"),
        pytest.param(
            "issues", {"action": "labeled", "label": {"name": "devin:autofix"}}, id="no-issue"
        ),
        pytest.param("issues", {"action": "labeled", "issue": "42876"}, id="issue-is-a-string"),
        pytest.param(
            "issues",
            {"action": "labeled", "label": {"name": "devin:autofix"}, "issue": {}},
            id="issue-without-a-number",
        ),
        pytest.param("pull_request", {"action": "closed", "pull_request": None}, id="null-object"),
        pytest.param(
            "check_suite",
            {"action": "completed", "check_suite": {"conclusion": "success"}},
            id="no-pull-requests-key",
        ),
        pytest.param(
            "check_suite",
            {"action": "completed", "check_suite": {"conclusion": "success", "pull_requests": {}}},
            id="pull-requests-is-not-a-list",
        ),
        pytest.param("issue_comment", {"action": "created", "comment": {}}, id="comment-no-body"),
        pytest.param("pull_request_review", {"action": "submitted"}, id="no-review"),
        pytest.param("ping", {}, id="ping-without-a-repository"),
    ],
)
def test_a_payload_missing_what_it_should_carry_is_ignored_rather_than_raising(
    event: str, body: dict[str, Any], settings: Settings
) -> None:
    """GitHub retries a delivery that failed, so a body the mapping cannot read has to end as a
    `202` and not as an exception on the request path."""
    mapped = map_event(event, body, settings=settings)

    assert mapped.ignored
    assert mapped.reason is not None


# ------------------------------------------------------------- from an intent to a state machine


def test_a_re_labelled_issue_has_no_trigger_to_apply(settings: Settings) -> None:
    """`Trigger.ISSUE_LABELLED` is legal only from `None`, and the domain deduplication layer treats
    a label removed and re-added as the same remediation — so the second delivery records an event
    and does nothing, rather than raising."""
    mapped = map_event("issues", github_payload("issues.labeled"), settings=settings)

    assert trigger_for(mapped, None) is Trigger.ISSUE_LABELLED
    assert trigger_for(mapped, State.RUNNING) is None
    assert trigger_for(mapped, State.MERGED) is None


@pytest.mark.parametrize(
    ("event", "build"),
    [
        pytest.param(
            "pull_request_review",
            lambda: variant(
                "pull_request_review.submitted.changes_requested", review__state="APPROVED"
            ),
            id="an-approval",
        ),
        pytest.param(
            "issue_comment", lambda: github_payload("issue_comment.created"), id="a-mention"
        ),
    ],
)
def test_an_intent_that_changes_no_state_has_no_trigger(
    event: str, build: Build, settings: Settings
) -> None:
    mapped = map_event(event, build(), settings=settings)

    assert mapped.trigger is None
    assert trigger_for(mapped, State.IN_REVIEW) is None


def test_an_illegal_trigger_other_than_re_labelling_is_still_returned(
    settings: Settings,
) -> None:
    """Only the re-labelling case is absorbed here. A check suite for a remediation in no CI state
    is a bug, and `transition()` is what says so."""
    mapped = map_event(
        "check_suite", github_payload("check_suite.completed.success"), settings=settings
    )

    assert trigger_for(mapped, State.QUEUED) is Trigger.CHECK_SUITE_SUCCEEDED


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: variant("issues.labeled", action="unlabeled"), id="unlabeled"),
        pytest.param(lambda: variant("issues.labeled", action="closed", label=ABSENT), id="closed"),
    ],
)
@pytest.mark.parametrize("state", [State.QUEUED, State.RUNNING, State.CHANGES_REQUESTED])
def test_a_cancellation_ends_a_remediation_that_is_not_yet_terminal(
    build: Build, state: State, settings: Settings
) -> None:
    mapped = map_event("issues", build(), settings=settings)

    trigger = trigger_for(mapped, state)
    assert trigger is not None
    moved = transition(state, trigger, pr_linked=True)

    assert moved.to_state is State.FAILED
    assert moved.moved


@pytest.mark.parametrize("state", [State.MERGED, State.BLOCKED, State.FAILED])
def test_a_cancellation_is_absorbed_once_a_remediation_is_terminal(
    state: State, settings: Settings
) -> None:
    """ "Cancel if not yet terminal" — and Sentinel closes the issue itself after a merge, so the
    `issues.closed` this produces has to be a no-op rather than an undo of the merge."""
    mapped = map_event("issues", variant("issues.labeled", action="closed"), settings=settings)

    trigger = trigger_for(mapped, state)
    assert trigger is not None
    moved = transition(state, trigger)

    assert moved.to_state is state
    assert moved.absorbed


def test_an_ignored_event_is_reported_as_such() -> None:
    assert MappedEvent(Intent.IGNORED, reason=Reason.PING).ignored
    assert not MappedEvent(Intent.CI_PASSED, Trigger.CHECK_SUITE_SUCCEEDED).ignored


@pytest.mark.parametrize("row", SUBSCRIBED, ids=lambda row: row.id)
def test_only_a_labelled_issue_may_create_a_remediation(row: Row, settings: Settings) -> None:
    """A delivery about a pull request or a closed issue is looked up, never upserted — otherwise
    the ingress path would create a remediation for work Sentinel never took on."""
    mapped = map_event(row.event, row.build(), settings=settings)

    assert mapped.creates_remediation == (row.intent is Intent.START_REMEDIATION)
