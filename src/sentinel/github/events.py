"""What one GitHub webhook delivery means to the pipeline.

A pure function over the subscribed-events table in `docs/06-event-pipeline.md`: given the
`X-GitHub-Event` header and the decoded body, it answers which remediation the delivery is about,
which `Trigger` — if any — to apply to it, and nothing else. No database, no HTTP, no clock. The
ingress path in [06](../../../docs/06-event-pipeline.md#ingress-path) calls it between storing the
delivery and upserting the remediation.

Two properties the spec is explicit about, both of which shape the code:

- **Nothing here raises.** An unknown event, an action the table does not list, a payload from
  another repository, a body missing the object it should carry — every one of them is
  `Intent.IGNORED` with a `Reason`. GitHub retries a delivery it thinks failed, so an event the
  mapping cannot classify must be a `202`, never a `5xx` that comes back forever. Every field is
  read defensively for the same reason: a payload GitHub reshapes is a delivery to ignore, not an
  exception on the request path.
- **Mapping is separate from applying.** `map_event` sees no remediation, because the ingress path
  has not looked one up yet. `trigger_for` is the second step, taken once the state is known — see
  [ADR](../../../docs/adr/2026-08-08-the-mapping-decides-what-there-is-to-apply.md), which also
  explains why re-labelling an issue is resolved there rather than by the caller.

The table's `issues.unlabeled` / `issues.closed` row — "cancel if not yet terminal" — has no
trigger and no state of its own in `docs/04-state-machine.md`. It is mapped to `Trigger.FAILED`
with a cancellation `Reason`;
[ADR](../../../docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md) records what that costs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sentinel.config import Settings
from sentinel.pipeline.state import State, Trigger, is_legal

DEVIN_BOT_MENTION: Final = "@devin-ai-integration"
"""The login of Devin's GitHub app, as a comment would mention it.

Not configurable: it is the account Devin itself pushes and comments as, so it is a fact about the
integration rather than a deployment choice. It becomes a setting the day a second bot has to be
recognised.
"""

CI_FAILURE_CONCLUSIONS: Final = frozenset({"failure", "timed_out"})
"""The two conclusions the spec treats as a CI failure. Anything else — `cancelled`, `neutral`,
`skipped`, `stale`, `action_required` — is ignored rather than guessed at."""


class Intent(StrEnum):
    """What a delivery means. One member per row of the subscribed-events table."""

    IGNORED = "ignored"
    START_REMEDIATION = "start_remediation"
    CANCEL = "cancel"
    LINK_PULL_REQUEST = "link_pull_request"
    PULL_REQUEST_MERGED = "pull_request_merged"
    PULL_REQUEST_ABANDONED = "pull_request_abandoned"
    CI_STARTED = "ci_started"
    CI_PASSED = "ci_passed"
    RESUME_FOR_CI_FAILURE = "resume_for_ci_failure"
    RESUME_FOR_REVIEW = "resume_for_review"
    RECORD_REVIEW_APPROVAL = "record_review_approval"
    FORWARD_COMMENT = "forward_comment"


class Reason(StrEnum):
    """Why the mapping reached the intent it did.

    A fixed identifier derived from no part of the payload, so it is safe to log and to store in
    `webhook_delivery.detail` alongside `handler_result = ignored`, or in
    `remediation_event.detail` for the two intents that end a remediation early.
    """

    UNKNOWN_EVENT = "unknown_event"
    UNHANDLED_ACTION = "unhandled_action"
    UNHANDLED_CONCLUSION = "unhandled_conclusion"
    UNHANDLED_REVIEW_STATE = "unhandled_review_state"
    OTHER_REPOSITORY = "other_repository"
    OTHER_LABEL = "other_label"
    NO_MENTION = "no_mention"
    NO_SUBJECT = "no_subject"
    PING = "ping"
    AUTOFIX_LABEL_REMOVED = "autofix_label_removed"
    ISSUE_CLOSED = "issue_closed"
    PULL_REQUEST_CLOSED_UNMERGED = "pull_request_closed_unmerged"


@dataclass(frozen=True, slots=True)
class MappedEvent:
    """One delivery, interpreted: what it means, what it is about, and what to apply.

    The identifiers are the ones the ingress path needs in order to *find* the remediation and to
    link its pull request. Everything else a later job wants is read from
    `webhook_delivery.payload`, which holds the delivery verbatim — this is a routing decision, not
    a copy of the body.
    """

    intent: Intent
    trigger: Trigger | None = None
    reason: Reason | None = None
    repo: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None

    @property
    def ignored(self) -> bool:
        """True when there is nothing to do but record the delivery and return `202`."""
        return self.intent is Intent.IGNORED

    @property
    def creates_remediation(self) -> bool:
        """True for the one intent that may bring a remediation into existence.

        The `INSERT … ON CONFLICT DO NOTHING` of the ingress path belongs to this intent alone.
        Every other one is *about* a remediation that should already exist, so the caller looks one
        up and records the delivery if it finds none — rather than creating a row for a pull request
        or a closed issue Sentinel never took on.
        """
        return self.intent is Intent.START_REMEDIATION


def map_event(event: str, payload: Mapping[str, Any], *, settings: Settings) -> MappedEvent:
    """Interpret one delivery. `event` is the `X-GitHub-Event` header, `payload` the decoded body.

    Never raises, whatever the body contains.
    """
    repository = _object(payload, "repository")
    # Checked before the event is dispatched, and for every event alike: the webhook is configured
    # on one repository, so anything else is a misconfiguration or a stray delivery, and the point
    # of `TARGET_REPO` is that no other repository can steer this pipeline. A payload with no
    # repository at all — an organisation-level ping — falls through to its own handler.
    if "full_name" in repository and _text(repository, "full_name") != settings.target_repo:
        return _ignored(Reason.OTHER_REPOSITORY)

    handler = _HANDLERS.get(event)
    if handler is None:
        return _ignored(Reason.UNKNOWN_EVENT)
    return handler(payload, settings)


def trigger_for(mapped: MappedEvent, state: State | None) -> Trigger | None:
    """The trigger to apply to a remediation currently in `state`, or `None` if there is none.

    `None` covers two cases the caller should not have to tell apart, both of which mean "record the
    delivery against the remediation and stop":

    - an intent that carries no trigger at all — an approving review, a comment forwarded to the
      session — which changes no state by design;
    - a re-labelled issue. `Trigger.ISSUE_LABELLED` is legal only from `None`, so applying it to a
      remediation that already exists would raise. The domain deduplication layer treats a label
      removed and re-added as one remediation, so this is the routine case rather than a fault.

    Every *other* illegal combination is left for `transition()` to reject. A
    `check_suite.completed` for a remediation in no CI state is a bug worth hearing about; a second
    `devin:autofix` label is a person clicking twice.

    `pr_linked` is not asked for, because `ISSUE_LABELLED` is the only trigger resolved here and it
    puts no condition on the pull request link. `transition()` still needs it.
    """
    if mapped.trigger is None:
        return None
    if mapped.trigger is Trigger.ISSUE_LABELLED and not is_legal(state, Trigger.ISSUE_LABELLED):
        return None
    return mapped.trigger


# ------------------------------------------------------------------------------------- handlers


def _issues(payload: Mapping[str, Any], settings: Settings) -> MappedEvent:
    action = _text(payload, "action")
    if action not in {"labeled", "unlabeled", "closed"}:
        return _ignored(Reason.UNHANDLED_ACTION)

    if action in {"labeled", "unlabeled"} and (
        _text(_object(payload, "label"), "name") != settings.autofix_label
    ):
        return _ignored(Reason.OTHER_LABEL)

    issue_number = _number(_object(payload, "issue"), "number")
    if issue_number is None:
        return _ignored(Reason.NO_SUBJECT)

    if action == "labeled":
        return MappedEvent(
            Intent.START_REMEDIATION,
            Trigger.ISSUE_LABELLED,
            repo=settings.target_repo,
            issue_number=issue_number,
        )
    return MappedEvent(
        Intent.CANCEL,
        Trigger.FAILED,
        reason=(Reason.AUTOFIX_LABEL_REMOVED if action == "unlabeled" else Reason.ISSUE_CLOSED),
        repo=settings.target_repo,
        issue_number=issue_number,
    )


def _pull_request(payload: Mapping[str, Any], settings: Settings) -> MappedEvent:
    action = _text(payload, "action")
    if action not in {"opened", "closed"}:
        return _ignored(Reason.UNHANDLED_ACTION)

    pull_request = _object(payload, "pull_request")
    pr_number = _number(pull_request, "number")
    if pr_number is None:
        return _ignored(Reason.NO_SUBJECT)

    subject: dict[str, Any] = {
        "repo": settings.target_repo,
        "pr_number": pr_number,
        "pr_url": _text(pull_request, "html_url"),
        "head_sha": _text(_object(pull_request, "head"), "sha"),
    }
    if action == "opened":
        return MappedEvent(Intent.LINK_PULL_REQUEST, Trigger.PR_OPENED, **subject)
    if pull_request.get("merged") is True:
        return MappedEvent(Intent.PULL_REQUEST_MERGED, Trigger.PR_MERGED, **subject)
    return MappedEvent(
        Intent.PULL_REQUEST_ABANDONED,
        Trigger.FAILED,
        reason=Reason.PULL_REQUEST_CLOSED_UNMERGED,
        **subject,
    )


def _pull_request_review(payload: Mapping[str, Any], settings: Settings) -> MappedEvent:
    if _text(payload, "action") != "submitted":
        return _ignored(Reason.UNHANDLED_ACTION)

    review = _object(payload, "review")
    # The webhook sends the review state in lower case and the REST API in upper case, and the
    # recorded payloads were reassembled from API responses. Neither casing is wrong to receive.
    state = (_text(review, "state") or "").lower()
    if state not in {"changes_requested", "approved"}:
        return _ignored(Reason.UNHANDLED_REVIEW_STATE)

    pull_request = _object(payload, "pull_request")
    pr_number = _number(pull_request, "number")
    if pr_number is None:
        return _ignored(Reason.NO_SUBJECT)

    subject: dict[str, Any] = {
        "repo": settings.target_repo,
        "pr_number": pr_number,
        "pr_url": _text(pull_request, "html_url"),
    }
    if state == "changes_requested":
        return MappedEvent(Intent.RESUME_FOR_REVIEW, Trigger.CHANGES_REQUESTED, **subject)
    # No trigger: `IN_REVIEW` is where an approval leaves the remediation, and the merge is what
    # moves it on. The delivery is still worth recording — review latency is measured from it.
    return MappedEvent(Intent.RECORD_REVIEW_APPROVAL, **subject)


def _check_suite(payload: Mapping[str, Any], settings: Settings) -> MappedEvent:
    action = _text(payload, "action")
    if action not in {"requested", "completed"}:
        return _ignored(Reason.UNHANDLED_ACTION)

    suite = _object(payload, "check_suite")
    conclusion = _text(suite, "conclusion")
    if action == "completed" and not (
        conclusion == "success" or conclusion in CI_FAILURE_CONCLUSIONS
    ):
        return _ignored(Reason.UNHANDLED_CONCLUSION)

    # A check suite is resolved to its remediation through the pull request, because that is the
    # link `remediation` stores; the head SHA comes along for the log line and the resume message.
    # A suite attached to no pull request belongs to no remediation — the CI triggers require a
    # linked one — so there is nothing to look up.
    pull_requests = suite.get("pull_requests")
    first = pull_requests[0] if isinstance(pull_requests, list) and pull_requests else None
    pr_number = _number(first, "number") if isinstance(first, Mapping) else None
    if pr_number is None:
        return _ignored(Reason.NO_SUBJECT)

    subject: dict[str, Any] = {
        "repo": settings.target_repo,
        "pr_number": pr_number,
        "head_sha": _text(suite, "head_sha"),
    }
    if action == "requested":
        return MappedEvent(Intent.CI_STARTED, Trigger.CHECK_SUITE_REQUESTED, **subject)
    if conclusion == "success":
        return MappedEvent(Intent.CI_PASSED, Trigger.CHECK_SUITE_SUCCEEDED, **subject)
    return MappedEvent(Intent.RESUME_FOR_CI_FAILURE, Trigger.CHECK_SUITE_FAILED, **subject)


def _issue_comment(payload: Mapping[str, Any], settings: Settings) -> MappedEvent:
    if _text(payload, "action") != "created":
        return _ignored(Reason.UNHANDLED_ACTION)

    body = _text(_object(payload, "comment"), "body") or ""
    if DEVIN_BOT_MENTION not in body.lower():
        return _ignored(Reason.NO_MENTION)

    issue_number = _number(_object(payload, "issue"), "number")
    if issue_number is None:
        return _ignored(Reason.NO_SUBJECT)

    # No trigger: a person talking to the session does not move the remediation. It is forwarded and
    # counted against `human_message_count`, which is what makes the autonomy rate in
    # `docs/07-observability.md` mean anything.
    return MappedEvent(
        Intent.FORWARD_COMMENT,
        repo=settings.target_repo,
        issue_number=issue_number,
    )


def _ping(payload: Mapping[str, Any], settings: Settings) -> MappedEvent:
    """Acknowledge and take no action. Ignored with a reason of its own, so that the delivery GitHub
    sends when the webhook is created is distinguishable in the log from an event we do not know."""
    return _ignored(Reason.PING)


Handler = Callable[[Mapping[str, Any], Settings], MappedEvent]

_HANDLERS: Final[Mapping[str, Handler]] = {
    "issues": _issues,
    "pull_request": _pull_request,
    "pull_request_review": _pull_request_review,
    "check_suite": _check_suite,
    "issue_comment": _issue_comment,
    "ping": _ping,
}


# -------------------------------------------------------------------------------------- reading


def _ignored(reason: Reason) -> MappedEvent:
    return MappedEvent(Intent.IGNORED, reason=reason)


def _object(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """A nested object, or an empty one where the payload does not carry it."""
    value = payload.get(key)
    return value if isinstance(value, Mapping) else {}


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _number(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    # `bool` is an `int`, and a `True` where an issue number belongs would be looked up as issue 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None
