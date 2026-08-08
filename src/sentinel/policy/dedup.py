"""Create-if-absent for a remediation, on the constraint the database already enforces.

`docs/adr/2026-08-07-two-layer-deduplication.md` puts both deduplication layers in the schema:
`webhook_delivery.delivery_id` is UNIQUE, and so is `remediation (repo, issue_number)`. Nothing in
this module re-implements either. What it does is make the *caller's* path correct — a caller that
read first and inserted second would have a window between the two, which is exactly where a second
Devin session for one issue comes from.

`INSERT … ON CONFLICT DO NOTHING RETURNING id` is atomic even against a concurrent inserter that has
not committed yet, which is the case the read-then-write version cannot handle. Postgres does not
skip the conflict and carry on: the second inserter waits on the first transaction, and only once it
has committed does the statement return no id. The follow-up `SELECT` then runs under a new
statement snapshot and sees the committed row, so "no id back" always means "somebody else's row,
and here it is" rather than "nothing here". Both halves are asserted on two connections in
`tests/test_policy.py`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.models import Remediation
from sentinel.pipeline.state import State


@dataclass(frozen=True, slots=True)
class Upsert:
    """Which remediation an event belongs to, and whether this caller is the one that created it.

    `created` is the answer to the question `sentinel.pipeline.state` refuses to answer for you:
    `Trigger.ISSUE_LABELLED` is legal only from `None`, so a label removed and re-added raises there
    rather than being ignored. A caller applies that trigger — and enqueues the `create_session` job
    that follows it — only when `created` is true. A second event about the same issue gets the
    existing id and no new work.
    """

    remediation_id: int
    created: bool


async def ensure_remediation(
    session: AsyncSession,
    *,
    repo: str,
    issue_number: int,
    issue_class: str,
    labeled_at: datetime.datetime,
    state: State = State.QUEUED,
) -> Upsert:
    """The remediation for `(repo, issue_number)`, creating it if there is not one already.

    Runs in the caller's transaction and commits nothing, so the row, the first
    `remediation_event` and the enqueued job are still written together or not at all
    (`docs/06-event-pipeline.md#ingress-path`).

    `issue_class`, `labeled_at` and `state` describe the remediation being created and are ignored
    when one already exists: the first event about an issue is the one that starts the clock, and a
    later event must not restate `labeled_at` or walk the state backwards.
    """
    created = (
        await session.execute(
            insert(Remediation)
            .values(
                repo=repo,
                issue_number=issue_number,
                issue_class=issue_class,
                state=str(state),
                labeled_at=labeled_at,
            )
            .on_conflict_do_nothing(index_elements=["repo", "issue_number"])
            .returning(Remediation.id)
        )
    ).scalar_one_or_none()
    if created is not None:
        return Upsert(remediation_id=created, created=True)

    existing = (
        await session.execute(
            select(Remediation.id).where(
                Remediation.repo == repo, Remediation.issue_number == issue_number
            )
        )
    ).scalar_one()
    return Upsert(remediation_id=existing, created=False)
