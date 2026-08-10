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
        intent=Intent.IGNORED,
        reason=Reason.LINKED_BY_THE_POLLER,
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
    # Every `completed` maps the same way and carries no trigger, whatever it concluded. The
    # conclusion belongs to one of the fork's 46 workflows and is not the CI verdict; a worker reads
    # that from the check runs on the head SHA. `cancelled` and `skipped` are here because they used
    # to be dropped, and dropping them now would lose the completion that finally makes a SHA green.
    Row(
        id="check_suite.completed-success",
        event="check_suite",
        build=lambda: github_payload("check_suite.completed.success"),
        intent=Intent.EVALUATE_CI,
    ),
    Row(
        id="check_suite.completed-failure",
        event="check_suite",
        build=lambda: github_payload("check_suite.completed.failure"),
        intent=Intent.EVALUATE_CI,
    ),
    Row(
        id="check_suite.completed-timed_out",
        event="check_suite",
        build=lambda: variant("check_suite.completed.failure", check_suite__conclusion="timed_out"),
        intent=Intent.EVALUATE_CI,
    ),
    Row(
        id="check_suite.completed-cancelled",
        event="check_suite",
        build=lambda: variant("check_suite.completed.failure", check_suite__conclusion="cancelled"),
        intent=Intent.EVALUATE_CI,
    ),
    Row(
        id="check_suite.completed-skipped",
        event="check_suite",
        build=lambda: variant("check_suite.completed.success", check_suite__conclusion="skipped"),
        intent=Intent.EVALUATE_CI,
    ),
    Row(
        id="issue_comment.created-mentioning-the-bot",
        event="issue_comment",
        build=lambda: github_payload("issue_comment.created"),
        intent=Intent.FORWARD_COMMENT,
    ),
    Row(id="ping", event="ping", build=a_ping, intent=Intent.IGNORED, reason=Reason.PING),
]


def by_id(row: Row) -> str:
    return row.id


@pytest.mark.parametrize("row", SUBSCRIBED, ids=by_id)
def test_every_subscribed_event_maps_to_its_intent(row: Row, settings: Settings) -> None:
    mapped = map_event(row.event, row.build(), settings=settings)

    assert (mapped.intent, mapped.trigger, mapped.reason) == (row.intent, row.trigger, row.reason)
    # The property the ingress path branches on, asserted for every row rather than for the two the
    # word "ignored" appears in: a wrong answer here silently drops a whole intent.
    assert mapped.ignored == (row.intent is Intent.IGNORED)


def test_the_table_accounts_for_every_intent() -> None:
    """No member of `Intent` exists that no subscribed event produces, and none is missing a row."""
    assert {row.intent for row in SUBSCRIBED} == set(Intent)


@pytest.mark.parametrize("name", GITHUB_PAYLOADS)
def test_no_recorded_payload_falls_through_unclassified(name: str, settings: Settings) -> None:
    """Every recording is a row of the table, so the only one that may be ignored is
    `pull_request.opened`, and only for the documented reason. Any other route to `ignored` means
    the mapping read a payload GitHub really sends through the wrong key."""
    allowed = {Reason.LINKED_BY_THE_POLLER} if name == "pull_request.opened" else set()

    mapped = map_event(name.split(".")[0], github_payload(name), settings=settings)

    assert not mapped.ignored or mapped.reason in allowed, f"{name} was ignored: {mapped.reason}"


# --------------------------------------------------------------------- what the delivery is about


def test_a_labelled_issue_carries_the_issue_it_is_about(settings: Settings) -> None:
    mapped = map_event("issues", github_payload("issues.labeled"), settings=settings)

    assert (mapped.repo, mapped.issue_number) == ("taxpon/superset", 42876)


def test_an_opened_pull_request_is_left_to_the_poller(settings: Settings) -> None:
    """It carries nothing that can find a remediation: `remediation` is keyed `(repo,
    issue_number)`, `pr_number` is NULL until `PR_OPENED` has been applied, and the recorded body
    references Sentry rather than the issue. The poller links it from `pull_requests[]`."""
    mapped = map_event("pull_request", github_payload("pull_request.opened"), settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.LINKED_BY_THE_POLLER)
    assert mapped.trigger is None


def test_a_closed_pull_request_is_found_by_its_number(settings: Settings) -> None:
    """By then `remediation.pr_number` is populated, which is what makes `closed` resolvable where
    `opened` is not."""
    mapped = map_event(
        "pull_request", github_payload("pull_request.closed.merged"), settings=settings
    )

    assert (mapped.repo, mapped.pr_number) == ("taxpon/superset", 42889)
    assert mapped.issue_number is None
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


def test_a_check_suite_on_several_pull_requests_takes_the_first(settings: Settings) -> None:
    """GitHub sends every pull request a head SHA sits on. Nothing in the payload says which the
    suite ran for, so the choice is arbitrary and pinned here rather than left to depend on how
    many the fixtures happen to carry."""
    recorded = github_payload("check_suite.completed.success")["check_suite"]["pull_requests"]
    body = variant(
        "check_suite.completed.success",
        check_suite__pull_requests=[*recorded, {**recorded[0], "number": 99001}],
    )

    mapped = map_event("check_suite", body, settings=settings)

    assert mapped.pr_number == 42889


def test_a_comment_on_a_pull_request_is_found_by_the_pull_request_number(
    settings: Settings,
) -> None:
    """GitHub delivers pull request conversation as `issue_comment`, with `issue.number` holding the
    pull request number and `issue.pull_request` present to say so — and that is where the reviewer
    talks to the session, so reading it as an issue number would find nothing."""
    body = variant(
        "issue_comment.created",
        issue__number=42889,
        issue__pull_request={"url": "https://api.github.com/repos/taxpon/superset/pulls/42889"},
    )

    mapped = map_event("issue_comment", body, settings=settings)

    assert mapped.intent is Intent.FORWARD_COMMENT
    assert (mapped.pr_number, mapped.issue_number) == (42889, None)


def test_a_comment_on_a_plain_issue_is_found_by_the_issue_number(settings: Settings) -> None:
    mapped = map_event("issue_comment", github_payload("issue_comment.created"), settings=settings)

    assert (mapped.issue_number, mapped.pr_number) == (42876, None)


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


ELSEWHERE: Final = "taxpon/sentinel-demo"


def test_the_recorded_repository_is_turned_away_when_it_is_not_the_target(
    settings: Settings,
) -> None:
    """`TARGET_REPO` is the whole of what stops another repository steering the pipeline, so a
    hardcoded one would keep accepting `taxpon/superset` however the deployment is configured."""
    moved = settings.model_copy(update={"target_repo": ELSEWHERE})

    mapped = map_event("issues", github_payload("issues.labeled"), settings=moved)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.OTHER_REPOSITORY)


@pytest.mark.parametrize("row", SUBSCRIBED, ids=by_id)
def test_every_row_takes_the_repository_from_the_configuration(
    row: Row, settings: Settings
) -> None:
    """Reconfigured, every row behaves identically and every intent carries the new name — which
    pins `repo` down at each of the four places a subject is built, rather than at the one place the
    single-row test happens to reach."""
    moved = settings.model_copy(update={"target_repo": ELSEWHERE})
    body = row.build()
    body["repository"]["full_name"] = ELSEWHERE

    mapped = map_event(row.event, body, settings=moved)

    assert (mapped.intent, mapped.trigger, mapped.reason) == (row.intent, row.trigger, row.reason)
    assert mapped.repo == (None if mapped.ignored else ELSEWHERE)


@pytest.mark.parametrize("conclusion", ["cancelled", "neutral", "skipped", "stale", None])
def test_every_completed_check_suite_asks_for_an_evaluation_whatever_it_concluded(
    conclusion: str | None, settings: Settings
) -> None:
    """These five used to be dropped as `unhandled_conclusion`, which was right while the
    conclusion *was* the verdict. It is not: the verdict is the aggregate over the head SHA, and the
    completion that finally settles it can perfectly well be a skipped suite. Dropping one would
    leave the remediation waiting on an evaluation nothing else will ask for."""
    body = variant("check_suite.completed.success", check_suite__conclusion=conclusion)

    mapped = map_event("check_suite", body, settings=settings)

    assert (mapped.intent, mapped.trigger) == (Intent.EVALUATE_CI, None)


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


AUTOFIX_LABEL: Final = {"name": "devin:autofix"}


@pytest.mark.parametrize(
    ("event", "body", "reason"),
    [
        pytest.param("issues", {}, Reason.UNHANDLED_ACTION, id="empty-body"),
        pytest.param("issues", {"action": "labeled"}, Reason.OTHER_LABEL, id="no-label"),
        pytest.param(
            "issues",
            {"action": "labeled", "label": AUTOFIX_LABEL},
            Reason.NO_SUBJECT,
            id="no-issue",
        ),
        pytest.param(
            "issues",
            {"action": "labeled", "label": AUTOFIX_LABEL, "issue": "42876"},
            Reason.NO_SUBJECT,
            id="issue-is-a-string",
        ),
        pytest.param(
            "issues",
            {"action": "labeled", "label": AUTOFIX_LABEL, "issue": {}},
            Reason.NO_SUBJECT,
            id="issue-without-a-number",
        ),
        pytest.param(
            "issues",
            {"action": "labeled", "label": AUTOFIX_LABEL, "issue": {"number": True}},
            Reason.NO_SUBJECT,
            id="issue-number-is-a-bool",
        ),
        pytest.param(
            "pull_request",
            {"action": "closed", "pull_request": None},
            Reason.NO_SUBJECT,
            id="null-object",
        ),
        pytest.param(
            "check_suite",
            {"action": "completed", "check_suite": {"conclusion": "success"}},
            Reason.NO_SUBJECT,
            id="no-pull-requests-key",
        ),
        pytest.param(
            "check_suite",
            # Non-empty, so that it reaches the subscript: an empty mapping is falsy and would be
            # turned away by the truthiness check before the type of the container mattered.
            {
                "action": "completed",
                "check_suite": {"conclusion": "success", "pull_requests": {"a": 1}},
            },
            Reason.NO_SUBJECT,
            id="pull-requests-is-not-a-list",
        ),
        pytest.param(
            "check_suite",
            {"action": "completed", "check_suite": {"conclusion": "success", "pull_requests": [7]}},
            Reason.NO_SUBJECT,
            id="pull-request-is-not-an-object",
        ),
        pytest.param(
            "issue_comment", {"action": "created", "comment": {}}, Reason.NO_MENTION, id="no-body"
        ),
        pytest.param(
            "issue_comment",
            {"action": "created", "comment": {"body": 42}},
            Reason.NO_MENTION,
            id="comment-body-is-not-a-string",
        ),
        pytest.param(
            # Past the mention check, so that the missing subject is what decides it: an intent with
            # no lookup key is the defect this whole guard exists to prevent.
            "issue_comment",
            {"action": "created", "comment": {"body": DEVIN_BOT_MENTION}, "issue": {}},
            Reason.NO_SUBJECT,
            id="mention-on-nothing",
        ),
        pytest.param(
            "pull_request_review",
            {"action": "submitted"},
            Reason.UNHANDLED_REVIEW_STATE,
            id="no-review",
        ),
        pytest.param(
            "pull_request_review",
            {"action": "submitted", "review": {"state": "approved"}},
            Reason.NO_SUBJECT,
            id="review-of-no-pull-request",
        ),
        pytest.param(
            "pull_request_review",
            {"action": "submitted", "review": {"state": "changes_requested"}, "pull_request": {}},
            Reason.NO_SUBJECT,
            id="review-of-a-pull-request-without-a-number",
        ),
        pytest.param("ping", {}, Reason.PING, id="ping-without-a-repository"),
    ],
)
def test_a_payload_missing_what_it_should_carry_is_ignored_rather_than_raising(
    event: str, body: dict[str, Any], reason: Reason, settings: Settings
) -> None:
    """GitHub retries a delivery that failed, so a body the mapping cannot read has to end as a
    `202` and not as an exception on the request path.

    The reason is asserted exactly, not merely as "some reason": an organisation-level ping carries
    no `repository`, and it has to reach `_ping` rather than be turned away as another repository's.
    """
    mapped = map_event(event, body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, reason)


@pytest.mark.parametrize("body", [None, [], ["issues"], "labeled", 42, 4.2, True])
def test_a_body_that_is_not_an_object_is_ignored_rather_than_raising(
    body: Any, settings: Settings
) -> None:
    """`json.loads` of a well-formed body produces any of these, so the mapping is handed them
    before anything has checked."""
    mapped = map_event("issues", body, settings=settings)

    assert (mapped.intent, mapped.reason) == (Intent.IGNORED, Reason.MALFORMED_BODY)


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


@pytest.mark.parametrize("row", [row for row in SUBSCRIBED if row.trigger is not None], ids=by_id)
def test_no_trigger_survives_a_remediation_that_does_not_exist(
    row: Row, settings: Settings
) -> None:
    """The lookup finding nothing is the other half of the existence boundary, and every trigger
    but `ISSUE_LABELLED` raises from `None`. Asserting it for the whole table rather than for the
    one case that came up: this is what the caller relies on when a pull request is closed by hand
    on the fork, or a check suite runs on a branch Sentinel never touched.

    Whatever `trigger_for` hands back must be applicable, so each returned trigger is put through
    `transition()` here rather than merely compared.
    """
    mapped = map_event(row.event, row.build(), settings=settings)

    trigger = trigger_for(mapped, None)

    if trigger is None:
        return
    assert trigger is Trigger.ISSUE_LABELLED
    assert transition(None, trigger).to_state is State.QUEUED


def test_an_illegal_trigger_within_the_lifecycle_is_still_returned(settings: Settings) -> None:
    """Only the two edges of the existence boundary are absorbed here. A check suite for a
    remediation that exists but is in no CI state is a bug, and `transition()` is what says so.

    `requested` rather than `completed`, because a completion no longer carries a trigger at all —
    it asks for an evaluation, and the worker chooses the trigger from the whole head SHA.
    """
    mapped = map_event(
        "check_suite",
        variant("check_suite.completed.success", action="requested", check_suite__conclusion=None),
        settings=settings,
    )

    assert trigger_for(mapped, State.QUEUED) is Trigger.CHECK_SUITE_REQUESTED


def test_a_completed_check_suite_carries_no_trigger_from_any_state(settings: Settings) -> None:
    """`trigger_for` has nothing to hand back for an evaluation, in any state — including the two
    it absorbs for other intents. The ingress reads that as "enqueue the job and move nothing"."""
    mapped = map_event(
        "check_suite", github_payload("check_suite.completed.success"), settings=settings
    )

    assert [trigger_for(mapped, state) for state in (None, State.QUEUED, State.IN_REVIEW)] == [
        None,
        None,
        None,
    ]


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
    assert not MappedEvent(Intent.CI_STARTED, Trigger.CHECK_SUITE_REQUESTED).ignored


@pytest.mark.parametrize("row", SUBSCRIBED, ids=by_id)
def test_only_a_labelled_issue_may_create_a_remediation(row: Row, settings: Settings) -> None:
    """A delivery about a pull request or a closed issue is looked up, never upserted — otherwise
    the ingress path would create a remediation for work Sentinel never took on."""
    mapped = map_event(row.event, row.build(), settings=settings)

    assert mapped.creates_remediation == (row.intent is Intent.START_REMEDIATION)
