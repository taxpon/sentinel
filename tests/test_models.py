"""The schema, against a real Postgres.

What is asserted here is what the rest of the system is entitled to assume: that the two
deduplication constraints are refused by the database rather than by a caller remembering to check,
that a remediation cannot be deleted out from under its event log, that the documented server
defaults and metric timestamps behave as `docs/03-data-model.md` and `docs/07-observability.md`
describe, that the queue's claiming statement holds up between two connections, and that the
migration and the models agree.

Every test starts from an empty schema and runs the migration, so what is exercised is the schema
`alembic upgrade head` produces — not one `Base.metadata.create_all` would have produced. A model
changed without a migration therefore fails here rather than in production.

The fixtures below are deliberately local: `tests/conftest.py` belongs to T04, which lifts them out.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import AsyncIterator, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import (
    Connection,
    DefaultClause,
    MetaData,
    Text,
    delete,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from sentinel import db
from sentinel.config import Settings
from sentinel.models import (
    NAMING_CONVENTION,
    AcuLedger,
    Base,
    Job,
    Remediation,
    RemediationEvent,
    WebhookDelivery,
)

# The host-side URL documented in `.env.example`, for a run that has not exported its own. CI sets
# DATABASE_URL; a worktree sharing the machine with another one has to.
DEFAULT_DATABASE_URL = "postgresql+asyncpg://sentinel:sentinel@localhost:5432/sentinel"

DOCUMENTED_TABLES = {"webhook_delivery", "remediation", "remediation_event", "job", "acu_ledger"}

# `docs/06-event-pipeline.md`, verbatim. A worker claims exactly one job with it, and it is repeated
# here so that a column renamed out from under it fails a test rather than a deployment.
CLAIM_ONE_JOB = text("""
    UPDATE job SET status = 'running', locked_by = :worker_id, locked_at = now()
    WHERE id = (
        SELECT id FROM job
        WHERE status = 'pending' AND run_after <= now()
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
""")

LABELED_AT = datetime.datetime(2026, 8, 8, 9, 0, tzinfo=datetime.UTC)


def a_remediation(**overrides: Any) -> Remediation:
    """A remediation with only the columns the schema requires."""
    return Remediation(
        **{
            "repo": "taxpon/superset",
            "issue_number": 42,
            "issue_class": "security",
            "state": "QUEUED",
            "labeled_at": LABELED_AT,
            **overrides,
        }
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    """Configuration whose only real value is the database URL.

    The credentials are placeholders: nothing in this module talks to Devin or GitHub, but
    `Settings` requires them, and going through it rather than around it is what proves `db.py`
    builds an engine from configuration.
    """
    return Settings(
        devin_api_token="devin-token",
        devin_org_id="org-test",
        devin_playbook_ids={},
        github_token="github-token",
        github_webhook_secret="webhook-secret",
        database_url=database_url,
    )


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    # Located from this file rather than the working directory: `script_location` in the ini is
    # relative to the ini, so this is the only path that has to be found.
    return Config(Path(__file__).resolve().parents[1] / "alembic.ini")


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = db.create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture
async def empty_database(engine: AsyncEngine) -> None:
    """An empty schema — no tables, no `alembic_version`, whatever the last run left behind."""
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture
async def migrated(
    engine: AsyncEngine, alembic_config: Config, empty_database: None
) -> AsyncEngine:
    await upgrade(engine, alembic_config, "head")
    return engine


@pytest.fixture
async def session(migrated: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with db.create_session_factory(migrated)() as session:
        yield session


@pytest.fixture
def process_engine(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point `db.get_engine()` — the process-wide engine — at the test database."""
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()
    yield
    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()


async def upgrade(engine: AsyncEngine, config: Config, revision: str) -> None:
    await run_alembic(engine, config, command.upgrade, revision)


async def downgrade(engine: AsyncEngine, config: Config, revision: str) -> None:
    await run_alembic(engine, config, command.downgrade, revision)


async def run_alembic(
    engine: AsyncEngine,
    config: Config,
    action: Callable[[Config, str], None],
    revision: str,
) -> None:
    """Run an Alembic command on a connection of ours, as `alembic/env.py` allows."""

    def run(connection: Connection) -> None:
        config.attributes["connection"] = connection
        action(config, revision)

    async with engine.begin() as connection:
        await connection.run_sync(run)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(await connection.run_sync(lambda c: inspect(c).get_table_names()))


async def test_upgrade_creates_the_documented_tables(migrated: AsyncEngine) -> None:
    assert await table_names(migrated) == DOCUMENTED_TABLES | {"alembic_version"}


async def test_downgrade_leaves_the_database_empty(
    migrated: AsyncEngine, alembic_config: Config
) -> None:
    await downgrade(migrated, alembic_config, "base")

    # `alembic_version` is Alembic's own bookkeeping and outlives the schema it tracks.
    assert await table_names(migrated) - {"alembic_version"} == set()


async def differences(engine: AsyncEngine, metadata: MetaData) -> list[Any]:
    """What autogenerate would write to turn the database into `metadata`.

    `compare_server_default` is off in Alembic by default and has to be asked for: with it off, a
    column whose default has drifted from its migration is not reported as a difference at all.
    `compare_type` has been on by default since Alembic 1.12 and is named anyway, so the comparison
    is pinned rather than inherited. Both have to be set here as well as in `alembic/env.py`, which
    this context does not go through — and that they are set is not left to be read off the code:
    `test_the_comparison_sees_*` below assert the comparison can actually fail.

    `compare_metadata` groups per table, so its result is a list of operations and lists of
    operations. Flattening it makes both the assertion and the failure message read plainly.
    """

    def compare(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        return compare_metadata(context, metadata)

    async with engine.connect() as connection:
        found = await connection.run_sync(compare)
    return [
        operation
        for group in found
        for operation in (group if isinstance(group, list) else [group])
    ]


async def operations(engine: AsyncEngine, metadata: MetaData) -> set[str]:
    """Just the names of those operations — `modify_default`, `add_column`, and so on."""
    return {operation[0] for operation in await differences(engine, metadata)}


def a_copy_of_the_models() -> MetaData:
    """`Base.metadata` copied, so that a test can drift one column without touching the real one."""
    copy = MetaData(naming_convention=NAMING_CONVENTION)
    for table in Base.metadata.tables.values():
        table.to_metadata(copy)
    return copy


async def test_the_migrated_schema_matches_the_models(migrated: AsyncEngine) -> None:
    """The test that catches a model changed without a migration.

    Autogenerate compares the live schema against `Base.metadata`; anything it would write into a
    new revision is a difference between the two.
    """
    assert await differences(migrated, Base.metadata) == []


async def test_the_comparison_sees_a_changed_server_default(migrated: AsyncEngine) -> None:
    """Proof that the test above can fail — and the one option that has to be asked for.

    `compare_server_default` is off in Alembic by default, and with it off the comparison reports
    *no difference at all* for a changed default: the drift test then passes on a model whose
    `server_default` no longer matches its migration. That is not hypothetical — it is how this
    test module shipped in its first version — and the stake is real, since
    `job.run_after DEFAULT now()` is what makes an un-backed-off job claimable.
    """
    drifted = a_copy_of_the_models()
    drifted.tables["remediation"].c.cycle.server_default = DefaultClause(text("7"))

    # Membership rather than equality: if a model really has drifted from its migration, the test
    # above is the one that should say so, and these two should not pile on with the same news.
    assert "modify_default" in await operations(migrated, drifted)


async def test_the_comparison_sees_a_changed_column_type(migrated: AsyncEngine) -> None:
    """The same for a changed column type, which is the weaker of the two guarantees.

    `compare_type` defaults to on, so *removing* it from the options changes nothing and this test
    stays green; only setting it to `False` breaks it. It is here because the property is worth
    holding either way, and because this default has already moved once — it was off before
    Alembic 1.12.
    """
    drifted = a_copy_of_the_models()
    drifted.tables["remediation"].c.issue_number.type = Text()

    assert "modify_type" in await operations(migrated, drifted)


async def test_a_redelivered_webhook_is_refused_by_the_database(session: AsyncSession) -> None:
    """The delivery deduplication layer: GitHub retries, and a retry must not be stored twice."""
    received_at = datetime.datetime.now(datetime.UTC)
    for _ in range(2):
        session.add(
            WebhookDelivery(
                delivery_id="8f1c-repeated",
                event="issues",
                payload={"action": "labeled"},
                received_at=received_at,
            )
        )

    with pytest.raises(IntegrityError) as raised:
        await session.flush()

    assert "uq_webhook_delivery_delivery_id" in str(raised.value)


async def test_a_second_remediation_for_one_issue_is_refused_by_the_database(
    session: AsyncSession,
) -> None:
    """The domain layer: several different events about one issue are still one remediation."""
    session.add(a_remediation())
    session.add(a_remediation(issue_class="flaky-test", state="RUNNING"))

    with pytest.raises(IntegrityError) as raised:
        await session.flush()

    assert "uq_remediation_repo_issue_number" in str(raised.value)


async def test_create_if_absent_is_one_statement(session: AsyncSession) -> None:
    """`INSERT … ON CONFLICT DO NOTHING RETURNING id`, the statement the worker uses.

    The second caller gets no id back, which is how it learns the remediation already existed
    without a read of its own — and without a window between the read and the write.
    """
    values = {
        "repo": "taxpon/superset",
        "issue_number": 42,
        "issue_class": "security",
        "state": "QUEUED",
        "labeled_at": LABELED_AT,
    }
    statement = (
        insert(Remediation)
        .values(values)
        .on_conflict_do_nothing(index_elements=["repo", "issue_number"])
        .returning(Remediation.id)
    )

    created = (await session.execute(statement)).scalar_one_or_none()
    already_there = (await session.execute(statement)).scalar_one_or_none()

    assert created is not None
    assert already_there is None
    assert (await session.execute(select(Remediation.id))).scalars().all() == [created]


async def test_a_minimal_remediation_takes_the_documented_defaults(session: AsyncSession) -> None:
    remediation = a_remediation()
    session.add(remediation)
    await session.flush()
    await session.refresh(remediation)

    assert remediation.cycle == 0
    assert remediation.acus_consumed == Decimal(0)
    assert remediation.human_message_count == 0
    # Every metric timestamp is absent until the remediation reaches that point, and the metrics in
    # `docs/07-observability.md` that read them are undefined for it until then.
    assert remediation.session_created_at is None
    assert remediation.pr_opened_at is None
    assert remediation.ci_green_at is None
    assert remediation.merged_at is None
    assert remediation.closed_at is None


async def test_the_metric_timestamps_subtract_to_the_documented_durations(
    session: AsyncSession,
) -> None:
    """The durations in `docs/07-observability.md` are a subtraction over one row."""
    hour = datetime.timedelta(hours=1)
    remediation = a_remediation(
        state="MERGED",
        session_created_at=LABELED_AT + hour,
        pr_opened_at=LABELED_AT + 2 * hour,
        ci_green_at=LABELED_AT + 3 * hour,
        merged_at=LABELED_AT + 5 * hour,
    )
    session.add(remediation)
    await session.flush()

    durations = (
        await session.execute(
            select(
                Remediation.session_created_at - Remediation.labeled_at,
                Remediation.pr_opened_at - Remediation.labeled_at,
                Remediation.ci_green_at - Remediation.labeled_at,
                Remediation.merged_at - Remediation.labeled_at,
                Remediation.merged_at - Remediation.ci_green_at,
                Remediation.closed_at - Remediation.labeled_at,
            )
        )
    ).one()

    assert durations == (hour, 2 * hour, 3 * hour, 5 * hour, 2 * hour, None)


async def test_events_written_in_one_transaction_share_a_created_at(
    session: AsyncSession,
) -> None:
    """`now()` is `transaction_timestamp()`, so `created_at` does not order a transaction.

    `docs/06-event-pipeline.md` writes the transition, the event and the job together, so several
    events per transaction is the normal case, and every one of them carries the timestamp of the
    transaction rather than of the insert. A consumer ordering the log — the timeline in
    `docs/07-observability.md`, the analytics cycle count — must tiebreak on `id`.
    """
    remediation = a_remediation()
    session.add(remediation)
    await session.flush()

    first = RemediationEvent(
        remediation_id=remediation.id, from_state=None, to_state="QUEUED", kind="transition"
    )
    session.add(first)
    await session.flush()

    second = RemediationEvent(
        remediation_id=remediation.id,
        from_state="QUEUED",
        to_state="SESSION_CREATED",
        kind="transition",
        detail={"session_id": "devin-1"},
    )
    session.add(second)
    await session.flush()

    for event in (first, second):
        await session.refresh(event)
        assert event.created_at.tzinfo is not None
    # Two separate flushes, one transaction: the timestamps are equal, not merely ordered.
    assert first.created_at == second.created_at
    assert first.id < second.id


async def test_a_remediation_with_events_cannot_be_deleted(session: AsyncSession) -> None:
    """The foreign key refuses to let a remediation take its history with it.

    Only the parent side is enforced. `DELETE FROM remediation_event` is still legal SQL, and that
    the log is never written to that way is application discipline, not a constraint.
    """
    remediation = a_remediation()
    session.add(remediation)
    await session.flush()
    session.add(
        RemediationEvent(remediation_id=remediation.id, to_state="QUEUED", kind="transition")
    )
    await session.flush()

    with pytest.raises(IntegrityError) as raised:
        await session.execute(delete(Remediation).where(Remediation.id == remediation.id))
        await session.flush()

    assert "fk_remediation_event_remediation_id_remediation" in str(raised.value)


async def test_an_event_needs_a_remediation_that_exists(session: AsyncSession) -> None:
    session.add(RemediationEvent(remediation_id=99999, to_state="QUEUED", kind="transition"))

    with pytest.raises(IntegrityError) as raised:
        await session.flush()

    assert "fk_remediation_event_remediation_id_remediation" in str(raised.value)


async def test_a_repo_wide_job_needs_no_remediation(session: AsyncSession) -> None:
    """The ACU sweep belongs to no single issue, which is why `job.remediation_id` is nullable."""
    job = Job(kind="sync_acu", payload={}, status="pending")
    session.add(job)
    await session.flush()
    await session.refresh(job)

    assert job.remediation_id is None
    assert job.attempts == 0
    # `run_after` defaulting to now() is what makes a job with no backoff immediately claimable.
    assert job.run_after <= datetime.datetime.now(datetime.UTC)


async def test_the_documented_claim_statement_claims_one_pending_job(
    session: AsyncSession,
) -> None:
    """The queue's claiming SQL against the real schema: what it selects, and what it writes back.

    One connection, so the row lock is never contended — that is the next test.
    """
    session.add_all(
        [
            Job(kind="create_session", payload={"issue": 42}, status="pending"),
            Job(
                kind="create_session",
                payload={"issue": 43},
                status="pending",
                run_after=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
            ),
        ]
    )
    await session.flush()

    claimed = (await session.execute(CLAIM_ONE_JOB, {"worker_id": "worker-1"})).mappings().one()

    assert claimed["payload"] == {"issue": 42}
    assert claimed["status"] == "running"
    assert claimed["locked_by"] == "worker-1"
    assert claimed["locked_at"] is not None
    # The second job is deferred by `run_after`, so nothing is left to claim.
    assert (await session.execute(CLAIM_ONE_JOB, {"worker_id": "worker-2"})).mappings().all() == []


async def test_two_workers_claim_disjoint_jobs_without_blocking(
    migrated: AsyncEngine, session: AsyncSession
) -> None:
    """`FOR UPDATE SKIP LOCKED` — the reason these tests need a real Postgres and not SQLite.

    Each claim runs on its own connection with its own transaction left open, so the second worker
    meets a row the first has locked and not yet committed. That is the only arrangement in which
    `SKIP LOCKED` does anything: on one connection the `status = 'pending'` predicate alone hides
    the claimed row, and the statement passes with the locking clause deleted.

    `lock_timeout` is what makes the failure visible. Without `SKIP LOCKED` the second worker waits
    for the first worker's transaction, which this test never commits, so the assertion would hang
    forever instead of failing; with it, the wait becomes an error after a second.
    """
    session.add_all(
        [
            Job(kind="create_session", payload={"issue": 42}, status="pending"),
            Job(kind="create_session", payload={"issue": 43}, status="pending"),
        ]
    )
    await session.commit()

    async def claim(connection: AsyncConnection, worker_id: str) -> int:
        await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
        return (
            (await connection.execute(CLAIM_ONE_JOB, {"worker_id": worker_id}))
            .mappings()
            .one()["id"]
        )

    async with migrated.connect() as first, migrated.connect() as second:
        # Neither transaction is committed: both locks are still held when the other worker claims.
        claimed = [await claim(first, "worker-1"), await claim(second, "worker-2")]

    assert len(set(claimed)) == 2


async def test_the_ledger_holds_one_row_per_day(session: AsyncSession) -> None:
    day = datetime.date(2026, 8, 8)
    session.add(
        AcuLedger(day=day, acus=Decimal("61.375"), synced_at=datetime.datetime.now(datetime.UTC))
    )
    await session.flush()

    session.add(
        AcuLedger(day=day, acus=Decimal("62.5"), synced_at=datetime.datetime.now(datetime.UTC))
    )
    with pytest.raises(IntegrityError) as raised:
        await session.flush()

    assert "pk_acu_ledger" in str(raised.value)


async def test_acus_keep_three_decimal_places(session: AsyncSession) -> None:
    """ACUs are `numeric`, not a float: the budget guard adds them up and compares to a ceiling."""
    session.add(a_remediation(acus_consumed=Decimal("12.345")))
    await session.flush()
    session.expunge_all()

    assert (await session.execute(select(Remediation.acus_consumed))).scalar_one() == Decimal(
        "12.345"
    )


async def test_session_scope_commits_and_leaves_the_row_readable(
    migrated: AsyncEngine, process_engine: None
) -> None:
    """`session_scope` is one unit of work, and `expire_on_commit=False` keeps the object usable."""
    async with db.session_scope() as session:
        remediation = a_remediation()
        session.add(remediation)

    # Reading an attribute after the commit must not emit a lazy SELECT: there is no session and no
    # greenlet left to run one in, and under asyncio that is a `MissingGreenlet`, not a slow query.
    assert remediation.state == "QUEUED"

    async with db.create_session_factory(migrated)() as verify:
        assert (await verify.execute(select(Remediation.issue_number))).scalar_one() == 42

    await db.dispose_engine()


async def test_session_scope_rolls_back_when_the_unit_of_work_fails(
    migrated: AsyncEngine, process_engine: None
) -> None:
    with pytest.raises(RuntimeError):
        async with db.session_scope() as session:
            session.add(a_remediation())
            await session.flush()
            raise RuntimeError("the handler failed after writing")

    async with db.create_session_factory(migrated)() as verify:
        assert (await verify.execute(select(Remediation.id))).scalars().all() == []

    await db.dispose_engine()


async def test_dispose_engine_lets_the_next_call_build_a_fresh_one(process_engine: None) -> None:
    first = db.get_engine()
    assert db.get_engine() is first

    await db.dispose_engine()

    assert db.get_engine() is not first
    await db.dispose_engine()
