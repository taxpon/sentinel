"""`POST /webhooks/github` — the ingress path of `docs/06-event-pipeline.md#ingress-path`.

The whole request is: verify the signature over the raw bytes, write the delivery, resolve the
remediation, apply the trigger the mapping produced, enqueue the job that trigger implies, and
answer. **Nothing external is called from here, ever**
(`docs/adr/2026-08-07-respond-202-before-external-calls.md`): GitHub abandons a delivery that takes
more than about ten seconds, and creating a Devin session is far slower than that. Every call to
Devin or GitHub happens in a worker, reached through `job`.

Everything after the signature check runs in one transaction, so the delivery row, the state change,
the `remediation_event` and the enqueued job are durable together or not at all. An exception rolls
all four back, which is why `handler_result` never takes the value `error` on this path: a delivery
whose handling raised leaves no row behind at all, and GitHub sees a `500` and can redeliver it.

**Neither deduplication layer is implemented here.** Both are database constraints
(`docs/adr/2026-08-07-two-layer-deduplication.md`), and this module is written so that it never has
to read before it writes — a check that races reads as protection and is not:

- *Delivery.* `INSERT … ON CONFLICT (delivery_id) DO NOTHING RETURNING id` is the check. No id back
  means GitHub has sent this delivery before, and the answer is `200` with a `duplicate` body — an
  error status would make GitHub retry it for ever.
- *Domain.* `sentinel.policy.dedup.ensure_remediation` is the same statement on
  `remediation (repo, issue_number)`, and its `created` flag is what says whether this delivery is
  the one that brought the remediation into existence. A label removed and re-added, or a comment on
  an issue already being worked on, resolves to the row that is already there.

Three things the modules this composes require of their caller, none of them enforced by a type:

- **`trigger_for()` may return `None`, and `transition()` must then not be called at all.** It comes
  back `None` for an intent that carries no trigger, for a delivery about a remediation that does
  not exist, and for a label re-added to an issue that already has one — the last two being the two
  sides of `Trigger.ISSUE_LABELLED` being legal only from `None`. Applying the trigger anyway raises
  `IllegalTransitionError` on a perfectly ordinary event
  (`docs/adr/2026-08-08-the-mapping-decides-what-there-is-to-apply.md`).
- **The state before the upsert is what the state machine is asked about**, not the state after it.
  `ensure_remediation` writes a `QUEUED` row, so a caller that read the state back would ask
  "`ISSUE_LABELLED` from `QUEUED`?" and be told no. `created` is that prior state: `None` when this
  delivery created the row, the row's own state when it did not.
- **`automatic_trigger()` is not applied for anyone.** Entering `CI_PASSED` obliges the caller to
  apply `REVIEW_REQUESTED`, so a green check suite writes two `remediation_event` rows. It is
  applied only when the first transition *moved*: a second success absorbed from `IN_REVIEW` would
  otherwise re-apply `REVIEW_REQUESTED` from a state it is illegal from.

`main.py` (T26) is the sole owner of registration; this module exports `router` and registers
nothing.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from typing import Annotated, Any, Final, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import ColumnElement, and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel import db
from sentinel.config import Settings, get_settings
from sentinel.github.events import Intent, MappedEvent, Reason, map_event, trigger_for
from sentinel.models import Remediation, RemediationEvent, WebhookDelivery
from sentinel.observability.logging import correlate, get_logger
from sentinel.pipeline.state import (
    TERMINAL_STATES,
    State,
    Transition,
    Trigger,
    automatic_trigger,
    transition,
)
from sentinel.policy.dedup import ensure_remediation
from sentinel.queue import JobKind, enqueue
from sentinel.security.hmac import SIGNATURE_HEADER, SignatureResult, verify_signature

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

DELIVERY_HEADER: Final = "X-GitHub-Delivery"
"""GitHub's per-delivery id. The delivery deduplication key, and the correlation id end to end —
the `run:` Devin tag, the `run` field on every log line, and a column on `remediation_event`."""

EVENT_HEADER: Final = "X-GitHub-Event"
"""The event name `map_event` dispatches on."""

STATUS_FOR_SIGNATURE: Final[Mapping[SignatureResult, int]] = {
    SignatureResult.BODY_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
}
"""`docs/06-event-pipeline.md#signature-verification` maps an oversized body to `413`; every other
rejection is a `401`, which is the default below."""

CLASS_LABEL_PREFIX: Final = "class:"
"""How the target repository carries an issue's class (`docs/09-operations.md`, and the labels
`scripts/bootstrap_github.py` creates). It is the only thing on a webhook payload that says which
playbook and ACU cap a remediation gets."""

UNCLASSIFIED: Final = "unclassified"
"""`remediation.issue_class` for an issue labelled `devin:autofix` and no `class:` label.

Not one of the classes in `docs/01-overview.md`, deliberately: `playbook_for` raises
`UnknownIssueClass` for it, which `docs/04-state-machine.md` already routes to `BLOCKED` with a
human asked to classify the issue. See the ADR — guessing a class here would spend a real ACU
budget on a guess.
"""

SOURCE: Final = "webhook"
"""Recorded on every `remediation_event` this module writes. The poller records `poller`, so the
audit trail says which process moved a remediation even where the delivery id is not enough."""

ENQUEUED: Final = "enqueued"
IGNORED: Final = "ignored"
DUPLICATE: Final = "duplicate"
"""`webhook_delivery.handler_result`, from the vocabulary in `docs/03-data-model.md`. `duplicate` is
a response body only — the second delivery writes no row to put it in."""

JOB_FOR_STATE: Final[Mapping[State, JobKind]] = {
    State.QUEUED: JobKind.CREATE_SESSION,
    State.CI_FAILED: JobKind.RESUME_SESSION,
    State.CHANGES_REQUESTED: JobKind.RESUME_SESSION,
    State.FAILED: JobKind.ESCALATE,
}
"""The **Side effects** column of `docs/04-state-machine.md#transitions`, for the transitions a
webhook can cause, keyed by the state they land in rather than by the trigger — `CI_FAILED` enqueues
a resume whether it was reached from `CI_RUNNING` or straight from `PR_OPENED`.

`MERGED` and `IN_REVIEW` have side effects too — a tag, a review request, an issue closed, a summary
comment — and no job kind in `docs/06-event-pipeline.md#jobs-and-claiming` covers them, so this
records the timestamps and leaves those to whoever adds a kind for them.
"""

ESCALATION_SUPPRESSED: Final[frozenset[Reason]] = frozenset(
    {Reason.AUTOFIX_LABEL_REMOVED, Reason.ISSUE_CLOSED}
)
"""The two reasons `docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md` ends a remediation
for without escalating it. Commenting `needs-human` on an issue a maintainer has just closed, or
unlabelled, is telling a person to look at the thing they were looking at when they called it off.
The transition is still recorded and the reason still reaches `blocked_reason`."""


class DeliveryResult(TypedDict):
    """What the endpoint answers with, on every status it can return but the signature ones.

    `result` is the `handler_result` written to the delivery row, or `duplicate` for one that was
    not written because GitHub had sent it before. `reason` is the mapping's own identifier for why
    it reached the intent it did — `other_repository` for a stray delivery, `issue_closed` for a
    cancellation — and is derived from no part of the payload, so it is safe to hand back.
    """

    delivery_id: str
    result: str
    reason: str | None
    remediation_id: int | None
    state: str | None


SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request, response: Response, settings: SettingsDep
) -> DeliveryResult:
    """Accept one GitHub webhook delivery.

    `202` once the delivery has been recorded and whatever it implies has been written, `200` for a
    delivery already seen, `401` for a signature that does not verify and `413` for a body above
    `MAX_BODY_BYTES`. An event the mapping cannot classify is recorded and answered `202`: GitHub
    redelivers what it thinks failed, so a `5xx` for an event we do not handle would come back for
    ever.
    """
    # The raw bytes, before anything decodes them. Verification is over exactly what GitHub signed:
    # decoding and re-encoding, or parsing and re-serialising the JSON, changes the digest.
    body = await request.body()
    delivery_id = request.headers.get(DELIVERY_HEADER)

    signature = verify_signature(
        body,
        request.headers.get(SIGNATURE_HEADER),
        settings.github_webhook_secret.get_secret_value(),
    )
    if not signature.ok:
        # `SignatureResult` members are fixed identifiers carrying no part of the request, so the
        # whole result is safe to log and to hand back. Nothing is written: it is not our delivery.
        log.warning(
            "webhook.signature.rejected",
            signature_result=signature.value,
            delivery_id=delivery_id,
            client_ip=request.client.host if request.client else None,
        )
        raise HTTPException(
            STATUS_FOR_SIGNATURE.get(signature, status.HTTP_401_UNAUTHORIZED),
            detail=signature.value,
        )

    event = request.headers.get(EVENT_HEADER)
    if not delivery_id or not event:
        # Both are on every delivery GitHub sends, and both are columns this cannot write without:
        # `delivery_id` is the deduplication key, so a delivery lacking one could be replayed
        # indefinitely by anything holding the secret.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{DELIVERY_HEADER} and {EVENT_HEADER} are required",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # `webhook_delivery.payload` is `jsonb NOT NULL` and holds the body verbatim, so a body that
        # is not JSON cannot be recorded at all. A body that *is* JSON but is not an object can be,
        # and is: `map_event` classifies it as `malformed_body` and it is stored like any other.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="body is not valid JSON") from None

    with correlate(run=delivery_id):
        async with db.session_scope() as session:
            return await _handle(
                session,
                response,
                event=event,
                delivery_id=delivery_id,
                payload=payload,
                settings=settings,
            )


async def _handle(
    session: AsyncSession,
    response: Response,
    *,
    event: str,
    delivery_id: str,
    payload: Any,
    settings: Settings,
) -> DeliveryResult:
    """Everything the delivery implies, in one transaction."""
    now = _now()
    delivery_row_id = await _record(
        session, event=event, delivery_id=delivery_id, payload=payload, received_at=now
    )
    if delivery_row_id is None:
        log.info("webhook.duplicate", github_event=event)
        response.status_code = status.HTTP_200_OK
        return _answer(delivery_id, DUPLICATE)

    mapped = map_event(event, payload, settings=settings)
    if mapped.ignored:
        # `webhook_delivery` has no `detail` column, so the mapping's reason lives on this line and
        # on the response rather than on the row; `handler_result = ignored` plus the stored payload
        # is what `docs/09-operations.md` troubleshoots from.
        log.info("webhook.ignored", github_event=event, reason=_value(mapped.reason))
        await _finish(session, delivery_row_id, IGNORED, now)
        return _answer(delivery_id, IGNORED, reason=_value(mapped.reason))

    resolved = await _resolve(session, mapped, payload, now=now)
    if resolved is None:
        # Every intent but one is *about* a remediation that should already exist. A pull request
        # closed on the fork by hand, or a check suite for a branch Sentinel never touched, belongs
        # to nothing — recorded, and no row invented for it.
        log.info("webhook.unresolved", github_event=event, intent=mapped.intent.value)
        await _finish(session, delivery_row_id, IGNORED, now)
        return _answer(delivery_id, IGNORED, reason=Reason.NO_SUBJECT.value)

    remediation, prior_state = resolved
    enqueued = await _act(
        session,
        remediation,
        prior_state,
        mapped=mapped,
        delivery_row_id=delivery_row_id,
        delivery_id=delivery_id,
        settings=settings,
        now=now,
    )
    result = ENQUEUED if enqueued else IGNORED
    await _finish(session, delivery_row_id, result, now)
    log.info(
        "webhook.handled",
        github_event=event,
        intent=mapped.intent.value,
        result=result,
        remediation_id=remediation.id,
        state=remediation.state,
    )
    return _answer(
        delivery_id,
        result,
        reason=_value(mapped.reason),
        remediation_id=remediation.id,
        state=remediation.state,
    )


# --- The delivery row -----------------------------------------------------------------------------


async def _record(
    session: AsyncSession,
    *,
    event: str,
    delivery_id: str,
    payload: Any,
    received_at: datetime.datetime,
) -> int | None:
    """Store the delivery, or `None` if `delivery_id` has been stored before.

    This statement *is* the delivery deduplication layer. `ON CONFLICT DO NOTHING` is atomic against
    a concurrent inserter that has not committed yet, which a read-then-write cannot be: the second
    request waits on the first transaction and then gets no id back, rather than reading an absence
    that was true a moment ago.
    """
    return (
        await session.execute(
            insert(WebhookDelivery)
            .values(
                delivery_id=delivery_id,
                event=event,
                action=_action(payload),
                payload=payload,
                received_at=received_at,
            )
            .on_conflict_do_nothing(index_elements=["delivery_id"])
            .returning(WebhookDelivery.id)
        )
    ).scalar_one_or_none()


async def _finish(
    session: AsyncSession, delivery_row_id: int, result: str, processed_at: datetime.datetime
) -> None:
    """Stamp the delivery with what handling it produced."""
    await session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_row_id)
        .values(processed_at=processed_at, handler_result=result)
    )


# --- The remediation ------------------------------------------------------------------------------


async def _resolve(
    session: AsyncSession, mapped: MappedEvent, payload: Any, *, now: datetime.datetime
) -> tuple[Remediation, State | None] | None:
    """The remediation this delivery is about, with the state it was in **before** this delivery.

    A prior state of `None` means the delivery created the remediation, which is the only shape
    `Trigger.ISSUE_LABELLED` is legal from. `None` for the whole result means there is no
    remediation to act on and nothing to invent one from.
    """
    repo, issue_number = mapped.repo, mapped.issue_number
    # `map_event` gives every non-ignored intent a repo and exactly one of `issue_number` and
    # `pr_number`, so neither guard below excludes a delivery any handler produces — and a delivery
    # with no key resolves to no remediation, which is the answer this function already returns for
    # one that names a remediation nobody created.
    if mapped.creates_remediation and repo is not None and issue_number is not None:
        upsert = await ensure_remediation(
            session,
            repo=repo,
            issue_number=issue_number,
            issue_class=issue_class(payload),
            labeled_at=now,
        )
        remediation = await session.get_one(Remediation, upsert.remediation_id)
        return remediation, None if upsert.created else State(remediation.state)

    criterion = _criterion(mapped)
    if criterion is None:
        return None
    existing = (await session.execute(select(Remediation).where(criterion))).scalar_one_or_none()
    return None if existing is None else (existing, State(existing.state))


def _criterion(mapped: MappedEvent) -> ColumnElement[bool] | None:
    """How `remediation` is looked up for this delivery.

    `remediation` is keyed `(repo, issue_number)` and `pr_number` is null until `PR_OPENED` has been
    applied, so an issue delivery resolves by issue number and everything about a pull request — a
    review, a check suite, a close — by `pr_number`, which is populated by the time any of them can
    arrive. `map_event` has already decided which of the two this delivery carries.
    """
    if mapped.repo is None:
        return None
    if mapped.issue_number is not None:
        return and_(
            Remediation.repo == mapped.repo, Remediation.issue_number == mapped.issue_number
        )
    if mapped.pr_number is not None:
        return and_(Remediation.repo == mapped.repo, Remediation.pr_number == mapped.pr_number)
    return None


def issue_class(payload: Any) -> str:
    """The issue's class, from the first `class:` label on it, or `UNCLASSIFIED`.

    The first rather than the only one: nothing stops a maintainer applying two, and `map_event`
    resolves the same kind of ambiguity the same way for a check suite on several pull requests.
    """
    if not isinstance(payload, Mapping):
        return UNCLASSIFIED
    issue = payload.get("issue")
    labels = issue.get("labels") if isinstance(issue, Mapping) else None
    for label in labels if isinstance(labels, list) else ():
        name = label.get("name") if isinstance(label, Mapping) else None
        if isinstance(name, str) and name.startswith(CLASS_LABEL_PREFIX):
            return name[len(CLASS_LABEL_PREFIX) :]
    return UNCLASSIFIED


# --- Applying the delivery ------------------------------------------------------------------------


async def _act(
    session: AsyncSession,
    remediation: Remediation,
    prior_state: State | None,
    *,
    mapped: MappedEvent,
    delivery_row_id: int,
    delivery_id: str,
    settings: Settings,
    now: datetime.datetime,
) -> bool:
    """Apply what the delivery carries. Returns whether a job was enqueued.

    Two shapes, and the branch between them is `trigger_for`'s answer rather than a rule of this
    module's own.
    """
    trigger = trigger_for(mapped, prior_state)
    if trigger is None:
        return await _act_without_trigger(
            session, remediation, mapped=mapped, delivery_id=delivery_id
        )

    enqueued = False
    state = prior_state
    applied: Trigger | None = trigger
    while applied is not None:
        result = transition(
            state,
            applied,
            cycle=remediation.cycle,
            pr_linked=remediation.pr_url is not None,
            max_fix_cycles=settings.max_fix_cycles,
        )
        enqueued |= await _write(
            session,
            remediation,
            result,
            mapped=mapped,
            delivery_row_id=delivery_row_id,
            delivery_id=delivery_id,
            now=now,
        )
        state = result.to_state
        # Only after a move: `REVIEW_REQUESTED` is legal from `CI_PASSED` alone, so a success
        # absorbed from `IN_REVIEW` would raise on the second lap rather than doing nothing.
        applied = automatic_trigger(result.to_state) if result.moved else None
    return enqueued


async def _write(
    session: AsyncSession,
    remediation: Remediation,
    result: Transition,
    *,
    mapped: MappedEvent,
    delivery_row_id: int,
    delivery_id: str,
    now: datetime.datetime,
) -> bool:
    """One transition: the columns, the one `remediation_event`, and the job it implies.

    An absorbed transition writes the event and nothing else. A late webhook against a terminal
    remediation, and a check suite carrying a verdict the state already has, are both real events
    and both belong in the log — `docs/04-state-machine.md` calls that "recorded and otherwise
    ignored" — but neither changed anything for a worker to act on.
    """
    reason = _value(mapped.reason) or result.reason
    if result.moved:
        remediation.state = result.to_state.value
        remediation.cycle = result.cycle
        _stamp(remediation, result.to_state, reason, now)

    # Outside the `moved` branch on purpose, and the only column that is. Every other timestamp
    # records a state the remediation entered; `merged_at` records something that happened to the
    # pull request, and that stays true when the remediation is somewhere a merge cannot move it
    # from. Remediation 1 sat in `BLOCKED` while a human resolved the escalation by merging its
    # pull request: the trigger absorbed, nothing was stamped, and the funnel read `merged: 0`
    # against a merge anybody could see on GitHub. Being terminal for *work* was allowed to mean
    # terminal for *observation* — see `docs/04-state-machine.md`, invariant 1.
    if result.trigger is Trigger.PR_MERGED:
        remediation.merged_at = remediation.merged_at or now

    session.add(
        RemediationEvent(
            remediation_id=remediation.id,
            webhook_delivery_id=delivery_row_id,
            from_state=_value(result.from_state),
            to_state=result.to_state.value,
            kind="transition",
            detail=_detail(
                source=SOURCE,
                trigger=result.trigger.value,
                intent=mapped.intent.value,
                reason=reason,
                absorbed=result.absorbed or None,
            ),
        )
    )
    log.info(
        "webhook.remediation.moved" if result.moved else "webhook.remediation.absorbed",
        from_state=_value(result.from_state),
        to_state=result.to_state.value,
        trigger=result.trigger.value,
        reason=reason,
    )

    kind = JOB_FOR_STATE.get(result.to_state) if result.moved else None
    if kind is JobKind.ESCALATE and mapped.reason in ESCALATION_SUPPRESSED:
        kind = None
    if kind is None:
        return False
    await enqueue(
        session,
        kind=kind,
        payload=_job_payload(mapped, delivery_id, trigger=result.trigger.value, reason=reason),
        remediation_id=remediation.id,
    )
    return True


def _stamp(
    remediation: Remediation, to_state: State, reason: str | None, now: datetime.datetime
) -> None:
    """The timestamp columns `docs/03-data-model.md` puts on entering a state.

    `ci_green_at` is the *first* green verdict, and the state machine absorbs a second one from
    `CI_PASSED` and `IN_REVIEW` — but not from `CI_FAILED`, where a re-run that goes green is a real
    move onto a pull request that has been green before. The guard is what keeps the column the
    first one.

    **`merged_at` is not here**, though `MERGED` is a state. It is stamped by the caller from the
    trigger instead, because a merge is observed whether or not it moves anything.
    """
    if to_state is State.CI_PASSED:
        remediation.ci_green_at = remediation.ci_green_at or now
    elif to_state is State.FAILED:
        # The column, not only the event. `docs/07-observability.md` computes the failure breakdown
        # as a count grouped by `blocked_reason` over `state in (BLOCKED, FAILED)`, so a reason that
        # lived only on the event would appear in that panel as an unlabelled null bucket
        # (`docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md`).
        remediation.blocked_reason = reason
        remediation.closed_at = now


async def _act_without_trigger(
    session: AsyncSession, remediation: Remediation, *, mapped: MappedEvent, delivery_id: str
) -> bool:
    """The deliveries that move nothing *here*: an approval, a comment, a check suite ending.

    An approval is recorded by the delivery row alone — review latency is
    `merged_at - ci_green_at`, both of which are stamped by the transitions that produce them.

    A comment mentioning the bot is different: `docs/06-event-pipeline.md` says to forward it to the
    session and count it as human intervention, and `human_message_count` is the denominator the
    autonomy rate in `docs/07-observability.md` rests on.

    A completed check suite is different again. It is not that nothing moves, but that what moves
    cannot be decided from the payload: the conclusion belongs to one of the fork's 46 workflows
    and says nothing about the others, and the verdict takes a call to GitHub that this path may
    not make
    ([ADR](../../../docs/adr/2026-08-10-ci-green-is-the-aggregate-of-the-check-runs.md)). So the
    work is handed to `evaluate_ci`, which reads every check run on the head SHA and applies
    whichever trigger the aggregate implies.

    All three are skipped for a terminal remediation — nothing will read the message, a comment on a
    merged issue is conversation rather than intervention, and a suite finishing after a merge has
    nothing left to move.
    """
    if State(remediation.state) in TERMINAL_STATES:
        return False

    if mapped.intent is Intent.EVALUATE_CI:
        await enqueue(
            session,
            kind=JobKind.EVALUATE_CI,
            payload=_job_payload(mapped, delivery_id),
            remediation_id=remediation.id,
        )
        return True

    if mapped.intent is not Intent.FORWARD_COMMENT:
        return False
    remediation.human_message_count += 1
    await enqueue(
        session,
        kind=JobKind.RESUME_SESSION,
        payload=_job_payload(mapped, delivery_id),
        remediation_id=remediation.id,
    )
    return True


def _job_payload(
    mapped: MappedEvent, delivery_id: str, *, trigger: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """What a job carries: why it exists, and what the handler cannot look up for itself.

    `delivery_id` is on every job because it is the correlation id end to end — the `run:` Devin tag
    a reviewer opens the session by. `trigger` is absent exactly when no transition caused the job,
    which is the handler's signal that there is a message to forward and no state to move.
    """
    return _detail(
        delivery_id=delivery_id,
        intent=mapped.intent.value,
        trigger=trigger,
        reason=reason,
        repo=mapped.repo,
        issue_number=mapped.issue_number,
        pr_number=mapped.pr_number,
        pr_url=mapped.pr_url,
        head_sha=mapped.head_sha,
    )


# --- Reading and writing small things -------------------------------------------------------------


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _action(payload: Any) -> str | None:
    """`payload.action`, for the column of that name. Absent on `ping` and on a malformed body."""
    if not isinstance(payload, Mapping):
        return None
    action = payload.get("action")
    return action if isinstance(action, str) else None


def _value(member: object) -> str | None:
    """A `StrEnum` member as the text stored in a `text` column, or `None`."""
    return None if member is None else str(member)


def _detail(**fields: Any) -> dict[str, Any]:
    """Drop the keys with nothing in them. A `"reason": null` claims there was no reason, when the
    truth is that this kind of event does not have one."""
    return {name: value for name, value in fields.items() if value is not None}


def _answer(
    delivery_id: str,
    result: str,
    *,
    reason: str | None = None,
    remediation_id: int | None = None,
    state: str | None = None,
) -> DeliveryResult:
    return {
        "delivery_id": delivery_id,
        "result": result,
        "reason": reason,
        "remediation_id": remediation_id,
        "state": state,
    }
