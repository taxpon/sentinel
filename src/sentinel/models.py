"""The five tables of `docs/03-data-model.md`, as SQLAlchemy 2.0 models.

Column order, types, nullability and defaults follow the tables in that document row by row; it is
the authoritative source, and a change here without a change there is a divergence.

Three properties of this schema are load-bearing elsewhere and are enforced by the database rather
than by application code, so that they hold under concurrent workers:

- `webhook_delivery.delivery_id` is UNIQUE — the delivery deduplication layer.
- `remediation (repo, issue_number)` is UNIQUE — the domain deduplication layer, which lets the
  worker express "create if absent" as `INSERT … ON CONFLICT DO NOTHING RETURNING id`.
- No foreign key cascades. `remediation_event` is append-only and doubles as the audit trail, so a
  remediation that has events cannot be deleted at all rather than taking its history with it.

`state`, `kind` and `status` are `text`, not Postgres enums: the vocabularies live in
`sentinel.pipeline.state` and `docs/06-event-pipeline.md`, and adding a state should not need a
migration and an exclusive lock.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Without a convention, Postgres names constraints and indexes itself, and Alembic cannot then
# reproduce those names in a migration. The drift test compares the migrated schema against this
# metadata, so every name has to be derivable from the models alone.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Every instant in this schema is stored with its offset. The metrics in `docs/07-observability.md`
# subtract timestamps written by three different processes, one of which is Postgres itself.
TIMESTAMPTZ: Final = DateTime(timezone=True)

# `numeric(10,3)` throughout, so ACUs add up exactly. Devin reports them to three decimal places.
ACUS: Final = Numeric(10, 3)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class WebhookDelivery(Base):
    """Everything GitHub sent, stored before it is interpreted."""

    __tablename__ = "webhook_delivery"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # GitHub's `X-GitHub-Delivery`. Unique because GitHub retries a delivery it thinks failed, and a
    # retry must not create a second session. It is also the correlation id end to end.
    delivery_id: Mapped[str] = mapped_column(Text, unique=True)
    event: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ)
    processed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)
    handler_result: Mapped[str | None] = mapped_column(Text)


class Remediation(Base):
    """One labelled issue and everything that follows from it."""

    __tablename__ = "remediation"
    __table_args__ = (
        UniqueConstraint("repo", "issue_number"),
        # The worker's in-flight query, the analytics time window, and the poller's reconciliation
        # of a session id back to its remediation.
        Index(None, "state"),
        Index(None, "labeled_at"),
        Index(None, "devin_session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo: Mapped[str] = mapped_column(Text)
    issue_number: Mapped[int] = mapped_column(Integer)
    issue_class: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    cycle: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    devin_session_id: Mapped[str | None] = mapped_column(Text)
    devin_session_url: Mapped[str | None] = mapped_column(Text)
    devin_status: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(Text)
    acus_consumed: Mapped[Decimal] = mapped_column(ACUS, server_default=text("0"))
    structured_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    human_message_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    # The clock starts at `labeled_at`; every other timestamp is null until the remediation reaches
    # that point, and the metric that reads it is undefined for the rows where it never does.
    labeled_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ)
    session_created_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)
    pr_opened_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)
    ci_green_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)
    merged_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)
    closed_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)


class RemediationEvent(Base):
    """Append-only transition log. Never updated, never deleted."""

    __tablename__ = "remediation_event"
    __table_args__ = (Index(None, "remediation_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    remediation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("remediation.id"))
    webhook_delivery_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("webhook_delivery.id")
    )
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Postgres's clock, rather than that of whichever of the three processes wrote the row. Note
    # what `now()` is: `transaction_timestamp()`. The pipeline writes the transition, the event and
    # the enqueued job in one transaction, and every row of that transaction carries the same value
    # to the microsecond — so `created_at` orders the log *between* transactions and not within
    # one. Anything reading the log in order tiebreaks on `id`.
    created_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class Job(Base):
    """The work queue, claimed with `FOR UPDATE SKIP LOCKED` (`docs/06-event-pipeline.md`)."""

    __tablename__ = "job"
    __table_args__ = (Index(None, "status", "run_after"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Null for repo-wide jobs, such as the ACU sync, that belong to no single remediation.
    remediation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("remediation.id"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Backoff and deferral both write here, so the claiming query needs no second predicate.
    run_after: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMPTZ)
    last_error: Mapped[str | None] = mapped_column(Text)


class AcuLedger(Base):
    """Daily ACU consumption, synced from Devin's consumption API."""

    __tablename__ = "acu_ledger"

    day: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    acus: Mapped[Decimal] = mapped_column(ACUS)
    # How stale the budget guard's view is. The dashboard shows its age.
    synced_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMPTZ)
