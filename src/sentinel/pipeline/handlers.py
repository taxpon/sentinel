"""What a claimed job does — the four job kinds of `docs/06-event-pipeline.md#jobs-and-claiming`.

Everything else in Sentinel observes or decides. This is the module that acts: it is the only place
that creates a Devin session, resumes one, or writes to the target repository on a remediation's
behalf. `worker.py` claims the jobs and decides how a failure is released; the handlers here decide
what a job *is*.

| Kind | Does |
|---|---|
| `create_session` | Policy checks, read the issue, `POST /v3/…/sessions`, enter `SESSION_CREATED` |
| `resume_session` | The review-fix loop: gather the failure, `POST /v3/…/messages`, go `RUNNING` |
| `escalate` | Apply `needs-human`, comment on the issue with the reason and the session link |
| `sync_acu` | Refresh `acu_ledger` from the consumption endpoint |

**Sentinel never merges** — there is no route for it in `sentinel.github.client` and no handler for
it here (`docs/adr/2026-08-07-humans-approve-every-merge.md`).

## Every handler runs twice sooner or later

`docs/adr/2026-08-08-one-claim-statement-and-a-fenced-lease.md` is the constraint the whole module
is shaped by: the lease fences the *row*, not the work. A worker that is slow rather than dead is
still running its handler when the lease expires, another worker claims the job, and the outbound
call is made a second time. For `create_session` that is two sessions, two ACU budgets and one
orphan nothing will ever observe, because Devin has no outbound webhook and the poller only knows
the session id that was written down.

`sentinel.policy.admit_session` refuses a remediation that already carries a `devin_session_id`,
and its own ADR is explicit that this **narrows the window rather than closing it**: the check reads
committed state, so it only sees an id the first worker committed. A handler that posted, wrote the
id and then called `complete()` would learn *there* that its lease had expired — and `LeaseLost`
would roll the id back with the completion, leaving the reclaimer to read `None` and post again.

So the ordering below is load-bearing, and it is the same in both handlers that call Devin:

1. do the outbound call;
2. **commit its effect in a transaction of its own** — `devin_session_id` for a creation, the
   `RUNNING` transition and the incremented cycle for a resume;
3. complete the job in a second transaction, where a `LeaseLost` can no longer undo step 2.

What is left is the genuinely unavoidable window: a worker that dies between the call and the
commit. v3 takes no idempotency token, so nothing available to Sentinel closes that one.

## The review-fix loop

`CI_FAILED → RUNNING` and `CHANGES_REQUESTED → RUNNING` both resume the **existing** session
(`docs/adr/2026-08-07-reuse-resumable-sessions.md`) — a new one would rediscover context already
paid for. Which of the two edges a job is on is read from the remediation's state and not from the
payload; see `docs/adr/2026-08-08-the-resume-edge-is-read-from-the-state.md`. The payload carries
only the facts the database does not hold, and every one of them is optional because every one of
them has an answer in GitHub or a parenthetical in the prompt templates:

| Key | Kind | Used for |
|---|---|---|
| `delivery_id` | `create_session` | The `run:<delivery_id>` correlation tag. **Required** |
| `head_sha` | `resume_session` | The commit CI failed on; falls back to the pull request's head |
| `review_id` | `resume_session` | Fetching the reviewer's inline comments |
| `review_body` | `resume_session` | The review's own text, which arrives on the webhook |
| `reason` | `escalate` | What to tell the human, and whether to tell them at all |

## Escalation is not automatic

`BLOCKED` and `FAILED` escalate rather than terminating quietly (`docs/04-state-machine.md`) — but
two of the reasons that reach `FAILED` are a maintainer removing the label or closing the issue
(`docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md`). Stamping `needs-human` on an issue
somebody has just deliberately closed is telling a person to look at the thing they were looking at
when they called it off, so those two reasons are suppressed. The transition is still recorded and
still visible on the dashboard.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sentinel import queue
from sentinel.config import Settings
from sentinel.devin.client import DevinClient
from sentinel.devin.playbooks import (
    UnknownIssueClass,
    changes_requested_message,
    ci_failure_message,
    cycle_tag,
)
from sentinel.devin.schemas import Consumption, Session, Unavailable
from sentinel.github.client import GitHubClient, ReviewComment
from sentinel.models import AcuLedger, Remediation, RemediationEvent
from sentinel.observability.logging import get_logger
from sentinel.pipeline.state import (
    CYCLE_LIMIT_EXHAUSTED,
    State,
    Transition,
    Trigger,
    is_legal,
    transition,
)
from sentinel.policy import admit_session, block
from sentinel.queue import ClaimedJob, JobKind

log = get_logger(__name__)

NEEDS_HUMAN_LABEL: Final = "needs-human"
"""The label an escalation applies, created by `docs/09-operations.md`'s repository bootstrap."""

UNKNOWN_ISSUE_CLASS: Final = "unknown_issue_class"
"""`blocked_reason` for the `QUEUED -> BLOCKED` row of `docs/04-state-machine.md` that reads "issue
class unrecognised". There is no playbook to run and no `class:` tag that would be accepted, so the
job can only end with a human being told."""

JOB_FAILED: Final = "job_failed"
"""`blocked_reason` when a job is retired without succeeding — `MAX_JOB_ATTEMPTS` spent, or a
failure the reliability policy does not retry. The error itself goes in the event detail; this is
the stable identifier the failure-breakdown panel groups on."""

CANCELLED_BY_A_HUMAN: Final[frozenset[str]] = frozenset({"autofix_label_removed", "issue_closed"})
"""The two `FAILED` reasons that must not be escalated: the maintainer called the work off at its
source, by removing the label or closing the issue.

**`pull_request_closed_unmerged` is deliberately not in this set.** It is also usually a human
closing something on purpose, and it is still escalated, because it leaves the *issue* open and
still labelled — see `docs/adr/2026-08-09-an-abandoned-pull-request-still-escalates.md`.

Spelled out here rather than imported from `sentinel.github.events`, which owns the vocabulary:
that module is unmerged and belongs to another task, and the ingress that writes these reasons to
`remediation.blocked_reason` belongs to a third. **Once T20 lands this should become
`frozenset({Reason.AUTOFIX_LABEL_REMOVED, Reason.ISSUE_CLOSED})`** — `Reason` is a `StrEnum` whose
module imports only `sentinel.config` and `sentinel.pipeline.state`, so it costs the worker nothing
and removes the drift this literal set carries. `tests/test_worker.py` pins the strings meanwhile.
The obligation is recorded in `docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md`, which
names this task."""

NO_FAILING_JOB: Final = "(GitHub reported no failing job for this commit)"
"""Stands in for the job name when nothing on the head SHA failed in Actions — a check suite whose
conclusion came from another app, or a run whose jobs were all cancelled. Written as a parenthetical
for the same reason `NO_LOG_OUTPUT` is: the absence of evidence is itself worth stating."""

TRANSITION_EVENT: Final = "transition"
ERROR_EVENT: Final = "error"
"""`remediation_event.kind` values from the vocabulary in `docs/03-data-model.md`."""

MAX_ERROR_LENGTH: Final = 2000
"""How much of a failure is kept on `job.last_error` and in the event detail."""


class MalformedJob(ValueError):
    """A job row this handler cannot act on: a missing payload key, or no remediation to act on.

    Never retried. The row will be identical on the next attempt, and the reason is on the job.
    """


@dataclass(frozen=True, slots=True)
class Context:
    """What every handler needs: the database, the two APIs, and the configuration.

    The session factory rather than a session, because a handler opens several transactions and the
    boundaries between them are the point — see the module docstring. Nothing is held open across an
    outbound call.
    """

    session_factory: async_sessionmaker[AsyncSession]
    devin: DevinClient
    github: GitHubClient
    settings: Settings


Handler = Callable[[Context, ClaimedJob], Awaitable[None]]


# --- create_session -------------------------------------------------------------------------


async def create_session(context: Context, job: ClaimedJob) -> None:
    """`QUEUED -> SESSION_CREATED`: ask the policy, read the issue, create the Devin session.

    The payload is read before anything is spent, so a job the ingress built wrongly fails without
    a consumption call and without a session.
    """
    delivery_id = _required(job, "delivery_id")

    facts = await _admit(context, job, delivery_id=delivery_id)
    if facts is None:
        return

    # The issue is read from GitHub rather than from the webhook body: it is a `GET`, so a `5xx`
    # costs a retry inside the client rather than a job attempt, and the title and body sent to
    # Devin are the current ones rather than the ones the label arrived with.
    issue = await context.github.get_issue(facts.issue_number)
    session = await context.devin.create_session(
        issue_number=facts.issue_number,
        issue_title=issue.title,
        issue_body=issue.body,
        issue_class=facts.issue_class,
        delivery_id=facts.delivery_id,
        repo=facts.repo,
    )

    await _record_session(context, job, session)
    await _complete(context, job)


@dataclass(frozen=True, slots=True)
class _SessionFacts:
    """What creating a session needs, read out of the database before the calls are made."""

    issue_number: int
    issue_class: str
    repo: str
    delivery_id: str


async def _admit(context: Context, job: ClaimedJob, *, delivery_id: str) -> _SessionFacts | None:
    """Run the admission policy, and return the facts to create a session with — or `None`.

    `None` means the job has already been released by the policy, and the only thing this handler
    may do is return (`docs/adr/2026-08-08-the-policy-releases-the-job-it-refuses.md`). The one
    ending the policy does not perform is an unrecognised issue class, which it raises rather than
    decides: it is the `QUEUED -> BLOCKED` row of the spec, and `block()` is what writes it.
    """
    async with context.session_factory() as db:
        remediation = await load_remediation(db, job)
        try:
            decision = await admit_session(
                db,
                job=job,
                remediation=remediation,
                devin=context.devin,
                settings=context.settings,
            )
        except UnknownIssueClass:
            await block(
                db,
                remediation=remediation,
                reason=UNKNOWN_ISSUE_CLASS,
                detail={"issue_class": remediation.issue_class},
            )
            await queue.complete(db, job)
            await db.commit()
            log.warning(
                "worker.session.unknown_class",
                issue=remediation.issue_number,
                issue_class=remediation.issue_class,
            )
            return None

        if not decision.admitted:
            await db.commit()
            return None

        facts = _SessionFacts(
            issue_number=remediation.issue_number,
            issue_class=remediation.issue_class,
            repo=remediation.repo,
            delivery_id=delivery_id,
        )
        await db.commit()
        return facts


async def _record_session(context: Context, job: ClaimedJob, session: Session) -> None:
    """Commit the session id, alone, before the job is completed.

    This is the transaction that closes the double-session window described at the top of the
    module. It writes what the transition table calls for — `devin_session_id`,
    `session_created_at`, the state and its event — and nothing about the job, so a `LeaseLost`
    raised later cannot roll the id back.

    The transition is applied only where it is legal, which is `QUEUED` and nowhere else. A
    remediation cancelled while the `POST` was in flight is terminal by now, and its session id is
    still written: it is the only record that an orphan session exists at all.
    """
    async with context.session_factory() as db:
        remediation = await load_remediation(db, job)
        remediation.devin_session_id = session.session_id
        remediation.session_created_at = _now()
        if session.url is not None:
            remediation.devin_session_url = session.url

        if is_legal(State(remediation.state), Trigger.SESSION_CREATED):
            outcome = transition(State(remediation.state), Trigger.SESSION_CREATED)
            remediation.state = str(outcome.to_state)
            db.add(_event(remediation, outcome, {"devin_session_id": session.session_id}))
        else:
            log.warning(
                "worker.session.orphaned",
                state=remediation.state,
                session_id=session.session_id,
                issue=remediation.issue_number,
            )
        await db.commit()
        log.info(
            "worker.session.created",
            session_id=session.session_id,
            issue=remediation.issue_number,
            issue_class=remediation.issue_class,
        )


# --- resume_session -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Resume:
    """One lap of the review-fix loop, read before any call is made.

    `state` is which of the two loop edges this is, and `cycle` is the lap number the message will
    announce — the value the transition will write, not the one currently in the column.
    """

    state: State
    session_id: str
    cycle: int
    pr_number: int
    pr_url: str


async def resume_session(context: Context, job: ClaimedJob) -> None:
    """`CI_FAILED -> RUNNING` or `CHANGES_REQUESTED -> RUNNING`: the review-fix loop.

    The same session is fed the new fact and told which lap it is on; a new session is never
    created. The order — message, cycle tag, then the committed transition — is the sequence
    diagram in `docs/04-state-machine.md#the-review-fix-loop`.
    """
    plan = await _plan_resume(context, job)
    if plan is None:
        return

    message = await _resume_message(context, job, plan)
    await context.devin.send_message(plan.session_id, message)
    await _tag_cycle(context, plan)
    await _record_resume(context, job)
    await _complete(context, job)


async def _plan_resume(context: Context, job: ClaimedJob) -> _Resume | None:
    """Decide whether this resume may happen, and on which edge — or release the job and return.

    Three of the four outcomes end here without a message being sent:

    - the remediation is terminal, cancelled while the job waited in the queue;
    - it is already `RUNNING`, because an earlier run of this handler committed its transition and
      then lost its lease — which is exactly what that ordering exists to make detectable;
    - `cycle` is spent, and `docs/06-event-pipeline.md` says `cycle > MAX_FIX_CYCLES` is `FAILED`
      rather than another lap.

    The first two are one condition: `SESSION_RESUMED` is legal from `CI_FAILED` and
    `CHANGES_REQUESTED` and from nowhere else, so `is_legal` answers both.
    """
    async with context.session_factory() as db:
        remediation = await load_remediation(db, job)
        state = State(remediation.state)
        if not is_legal(state, Trigger.SESSION_RESUMED):
            await queue.complete(db, job)
            await db.commit()
            log.info("worker.resume.skipped", state=remediation.state, job_id=job.id)
            return None

        outcome = _resume_transition(remediation, context.settings)
        if outcome.to_state is not State.RUNNING:
            await fail_remediation(
                db,
                remediation,
                reason=outcome.reason or CYCLE_LIMIT_EXHAUSTED,
                detail={
                    "cycle": remediation.cycle,
                    "max_fix_cycles": context.settings.max_fix_cycles,
                },
            )
            await queue.complete(db, job)
            await db.commit()
            log.warning(
                "worker.resume.cycle_limit",
                cycle=remediation.cycle,
                max_fix_cycles=context.settings.max_fix_cycles,
                issue=remediation.issue_number,
            )
            return None

        plan = _Resume(
            state=state,
            session_id=_session_id(remediation),
            cycle=outcome.cycle,
            pr_number=_pull_request_number(remediation),
            pr_url=_pull_request_url(remediation),
        )
        await db.commit()
        return plan


async def _resume_message(context: Context, job: ClaimedJob, plan: _Resume) -> str:
    """The message the session is resumed with, built from what GitHub can still be asked.

    Neither branch fails over missing evidence. An expired Actions log, a check suite from an app
    other than Actions, a review that requested changes with nothing written anywhere: each renders
    as the parenthetical `docs/05-devin-integration.md` specifies, because "there is no log" is a
    fact worth sending and a blank gap is not.
    """
    max_cycles = context.settings.max_fix_cycles
    if plan.state is State.CI_FAILED:
        sha = (
            _text(job.payload.get("head_sha"))
            or (await context.github.get_pull_request(plan.pr_number)).head_sha
        )
        failing = await context.github.get_failing_job(sha)
        return ci_failure_message(
            sha=sha,
            job_name=NO_FAILING_JOB if failing is None else failing.name,
            log="" if failing is None else failing.log_excerpt,
            cycle=plan.cycle,
            max_cycles=max_cycles,
        )

    # The review's body arrives on the webhook and is carried in the payload; the inline comments do
    # not, and a review can request changes with an empty body and say everything on the diff.
    review_id = job.payload.get("review_id")
    comments: Sequence[ReviewComment] = ()
    if review_id is not None:
        comments = await context.github.get_review_comments(plan.pr_number, int(review_id))
    return changes_requested_message(
        pr_url=plan.pr_url,
        review_body=_text(job.payload.get("review_body")),
        inline_comments=[_render_comment(comment) for comment in comments if comment.body.strip()],
        cycle=plan.cycle,
        max_cycles=max_cycles,
    )


async def _tag_cycle(context: Context, plan: _Resume) -> None:
    """Append `cycle:N`, and do not fail the lap over it.

    The tag is how a reviewer confirms in the Devin dashboard that the lap Sentinel claims to have
    started is the one Devin received, so it is sent. It is not worth the alternative, though: the
    message has already gone, and a job that failed here would either re-send it on the retry or
    spend the remediation's attempts on a label.
    """
    try:
        await context.devin.tag_session(plan.session_id, [cycle_tag(plan.cycle)])
    except Exception as exc:
        # Broad on purpose: the lap has succeeded by now, and the tag is the only casualty.
        log.warning(
            "worker.resume.tag_failed",
            session_id=plan.session_id,
            cycle=plan.cycle,
            error=describe_error(exc),
        )


async def _record_resume(context: Context, job: ClaimedJob) -> None:
    """Commit the `RUNNING` transition and the incremented cycle, before the job is completed.

    The remediation is re-read rather than carried across the call, so a webhook that moved it while
    the message was in flight is not overwritten — and a second worker that got there first leaves
    a state this transition is no longer legal from, which is how a duplicate lap is detected rather
    than counted.
    """
    async with context.session_factory() as db:
        remediation = await load_remediation(db, job)
        if not is_legal(State(remediation.state), Trigger.SESSION_RESUMED):
            log.warning("worker.resume.superseded", state=remediation.state, job_id=job.id)
            return

        outcome = _resume_transition(remediation, context.settings)
        remediation.state = str(outcome.to_state)
        remediation.cycle = outcome.cycle
        if outcome.to_state is State.FAILED:
            remediation.blocked_reason = outcome.reason
            remediation.closed_at = _now()
        db.add(_event(remediation, outcome, {"cycle": outcome.cycle}))
        await db.commit()
        log.info(
            "worker.resume.sent",
            cycle=outcome.cycle,
            to_state=str(outcome.to_state),
            issue=remediation.issue_number,
        )


def _resume_transition(remediation: Remediation, settings: Settings) -> Transition:
    return transition(
        State(remediation.state),
        Trigger.SESSION_RESUMED,
        cycle=remediation.cycle,
        pr_linked=remediation.pr_url is not None,
        max_fix_cycles=settings.max_fix_cycles,
    )


def _render_comment(comment: ReviewComment) -> str:
    """One inline comment, as a line of provenance and then the reviewer's words.

    The location is what makes an inline comment actionable — a review body says what is wrong, a
    diff comment says where — and `line` is null on a comment against a line that has since changed.
    """
    where = comment.path if comment.line is None else f"{comment.path}:{comment.line}"
    return f"{where}\n{comment.body.strip()}"


# --- escalate -------------------------------------------------------------------------------


async def escalate(context: Context, job: ClaimedJob) -> None:
    """Tell a human: apply `needs-human`, then comment with the reason and the session link.

    **The label goes on first, and that ordering is the idempotency.** `add_labels` leaves a label
    already present alone, so repeating it costs nothing, while a repeated comment is a second
    escalation on the same issue. A retry after the comment failed therefore re-labels and comments
    once; the only way to comment twice is the lease window every handler shares.

    **The reason is read from `remediation.blocked_reason`**, and falls back to the payload only
    when the column is empty. The column is the one place the reason is contractually obliged to be:
    `docs/adr/2026-08-08-cancellation-is-recorded-as-failed.md` requires the ingress to write it
    there, and the failure breakdown in `docs/07-observability.md` groups on it — so a reason
    missing from the column is already a hole in the dashboard, and reading it from anywhere else
    would hide that. The `escalate` payload is specified by no record, and preferring it would make
    the suppression decision depend on a third copy of a string nobody is obliged to fill. The
    payload is read for the *figures* behind the reason, which exist nowhere else.

    Suppressed for the reasons that mean a maintainer called the work off — see the module
    docstring, `CANCELLED_BY_A_HUMAN`, and
    `docs/adr/2026-08-09-an-abandoned-pull-request-still-escalates.md` for the neighbouring case
    that deliberately does not.
    """
    async with context.session_factory() as db:
        remediation = await load_remediation(db, job)
        issue_number = remediation.issue_number
        reason = _text(remediation.blocked_reason) or _text(job.payload.get("reason"))
        state = remediation.state
        session_url = remediation.devin_session_url

    if reason in CANCELLED_BY_A_HUMAN:
        log.info("worker.escalation.suppressed", reason=reason, issue=issue_number, state=state)
    else:
        await context.github.add_labels(issue_number, [NEEDS_HUMAN_LABEL])
        comment = escalation_comment(
            state=state,
            reason=reason,
            session_url=session_url,
            detail=_escalation_detail(job.payload),
        )
        posted = await context.github.comment_on_issue(issue_number, comment)
        log.info(
            "worker.escalation.posted",
            reason=reason,
            issue=issue_number,
            state=state,
            comment_url=posted.html_url,
        )

    await _complete(context, job)


def escalation_comment(
    *,
    state: str,
    reason: str,
    session_url: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> str:
    """The comment `docs/04-state-machine.md#escalation` asks for.

    The reason and a link to the session are the two things it names. The figures a policy refusal
    was reached on are appended when there are any, because "the daily budget is exhausted" without
    the numbers is a claim a reader has to take on trust.

    A pure function of its arguments, so the wording is asserted without a fake GitHub.
    """
    lines = [
        "**Sentinel could not complete this remediation.**",
        "",
        f"- State: `{state}`",
        f"- Reason: `{reason or 'unspecified'}`",
    ]
    if session_url:
        lines.append(f"- Devin session: {session_url}")
    lines += [f"- {key}: `{value}`" for key, value in sorted((detail or {}).items())]
    lines += [
        "",
        "This needs a human. Sentinel has stopped working on it and will not retry it "
        "automatically.",
    ]
    return "\n".join(lines)


def _escalation_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The escalation payload minus the two keys the comment already renders as its own lines."""
    return {key: value for key, value in payload.items() if key not in {"reason", "state"}}


# --- sync_acu -------------------------------------------------------------------------------


async def sync_acu(context: Context, job: ClaimedJob) -> None:
    """Refresh `acu_ledger` from the consumption endpoint.

    Every day the endpoint reports is written, not only today's: a sync that has not run for a while
    is exactly the case the ledger exists to repair, and a day Devin restates is a day it has
    changed its mind about.

    An organisation without consumption scope degrades to `Unavailable` rather than raising
    (`docs/adr/2026-08-08-enterprise-degradation-is-a-returned-value.md`). Nothing is written and
    the job succeeds: the budget guard reads the ledger as one of three sources and covers the gap
    with the other two.
    """
    consumption = await context.devin.daily_consumption()
    if isinstance(consumption, Unavailable):
        log.warning(
            "worker.acu_sync.unavailable",
            reason=consumption.reason.value,
            fallback=consumption.fallback,
        )
    else:
        async with context.session_factory() as db:
            await _write_ledger(db, consumption.value)
            await db.commit()
        log.info("worker.acu_sync.written", days=len(consumption.value.days))

    await _complete(context, job)


async def _write_ledger(db: AsyncSession, consumption: Consumption) -> None:
    """Upsert one row per reported day, keyed by the date, in the caller's transaction."""
    synced_at = _now()
    for day in consumption.days:
        statement = upsert(AcuLedger).values(
            # Via `str`, so a float arrives as the decimal it was printed as rather than its binary
            # neighbour: `acu_ledger.acus` is `numeric(10,3)` so that ACUs add up exactly.
            day=day.date,
            acus=Decimal(str(day.acus)),
            synced_at=synced_at,
        )
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[AcuLedger.day],
                set_={"acus": statement.excluded.acus, "synced_at": statement.excluded.synced_at},
            )
        )


# --- shared -----------------------------------------------------------------------------------


HANDLERS: Final[Mapping[str, Handler]] = {
    JobKind.CREATE_SESSION: create_session,
    JobKind.RESUME_SESSION: resume_session,
    JobKind.ESCALATE: escalate,
    JobKind.SYNC_ACU: sync_acu,
}
"""Every job kind this worker can run, keyed by the string in the column.

Keyed by `str` and not by `JobKind` for the reason `ClaimedJob.kind` is a `str`: a row written by a
future version of another process must be a job this worker refuses, not an exception raised while
claiming it.
"""


async def load_remediation(db: AsyncSession, job: ClaimedJob) -> Remediation:
    """The remediation a job is about, loaded in `db`.

    `scalar_one`, because `remediation` is never deleted and the foreign key is enforced: a miss is
    a fault to raise on rather than a branch to write.
    """
    if job.remediation_id is None:
        raise MalformedJob(f"job {job.id} of kind {job.kind!r} names no remediation")
    result = await db.execute(select(Remediation).where(Remediation.id == job.remediation_id))
    return result.scalar_one()


async def fail_remediation(
    db: AsyncSession,
    remediation: Remediation,
    *,
    reason: str,
    detail: Mapping[str, Any] | None = None,
    kind: str = TRANSITION_EVENT,
) -> bool:
    """Move a remediation to `FAILED` and enqueue its escalation, in the caller's transaction.

    The `FAILED` counterpart of `sentinel.policy.block()`, and written to the same rule: the state
    column, the `remediation_event` and the `escalate` job are one transaction, which is invariant 4
    of `docs/04-state-machine.md`. Returns whether the remediation moved — a terminal one absorbs
    the trigger and nothing is written.
    """
    outcome = transition(State(remediation.state), Trigger.FAILED, cycle=remediation.cycle)
    if not outcome.moved:
        return False
    remediation.state = str(outcome.to_state)
    remediation.blocked_reason = reason
    remediation.closed_at = _now()
    db.add(_event(remediation, outcome, {"reason": reason, **(detail or {})}, kind=kind))
    await queue.enqueue(
        db,
        kind=JobKind.ESCALATE,
        payload={"reason": reason, "state": str(outcome.to_state), **(detail or {})},
        remediation_id=remediation.id,
    )
    return True


def _event(
    remediation: Remediation,
    outcome: Transition,
    detail: Mapping[str, Any],
    *,
    kind: str = TRANSITION_EVENT,
) -> RemediationEvent:
    """The one `remediation_event` a transition writes.

    `webhook_delivery_id` is null: the worker acts on a job, and the delivery that enqueued it is
    carried as the `run` correlation id rather than as a foreign key on every row it causes.
    """
    return RemediationEvent(
        remediation_id=remediation.id,
        from_state=outcome.from_state,
        to_state=str(outcome.to_state),
        kind=kind,
        detail={"source": "worker", "trigger": outcome.trigger.value, **detail},
    )


async def _complete(context: Context, job: ClaimedJob) -> None:
    """Retire a job that succeeded, in a transaction that writes nothing else.

    Separate from whatever the handler committed, on purpose: `LeaseLost` raised here rolls back an
    empty transaction rather than the effect the handler has already had.
    """
    async with context.session_factory() as db:
        await queue.complete(db, job)
        await db.commit()


def _required(job: ClaimedJob, key: str) -> str:
    value = _text(job.payload.get(key))
    if not value:
        raise MalformedJob(f"job {job.id} of kind {job.kind!r} has no {key!r} in its payload")
    return value


def _session_id(remediation: Remediation) -> str:
    if remediation.devin_session_id is None:
        raise MalformedJob(f"remediation {remediation.id} is {remediation.state} with no session")
    return remediation.devin_session_id


def _pull_request_number(remediation: Remediation) -> int:
    if remediation.pr_number is None:
        raise MalformedJob(
            f"remediation {remediation.id} is {remediation.state} with no pull request"
        )
    return remediation.pr_number


def _pull_request_url(remediation: Remediation) -> str:
    if remediation.pr_url is None:
        raise MalformedJob(
            f"remediation {remediation.id} is {remediation.state} with no pull request"
        )
    return remediation.pr_url


def _text(value: Any) -> str:
    """A payload field that should be a string, from JSONB that is entitled to hold anything."""
    return value.strip() if isinstance(value, str) else ""


def describe_error(exc: BaseException) -> str:
    """One failure, as `job.last_error` and `remediation_event.detail` record it.

    The class and the message, truncated. Both clients scrub their credentials out of the messages
    they raise, because this text reaches the database and the dashboard from there.
    """
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)
