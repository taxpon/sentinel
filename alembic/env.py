"""Alembic environment.

Two things here are not the generated default.

**The URL comes from `DATABASE_URL`, not from `sentinel.config.Settings`.** Settings also requires
the Devin and GitHub credentials, and applying a migration needs a database, not an API token — a
deploy step should not fail because a token has been rotated out of the environment.

**A caller may supply its own connection** in `config.attributes["connection"]`. Tests use this to
run the migrations on the connection they already hold, inside the event loop they already have,
instead of starting a second engine.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from sentinel.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Alembic needs the database to migrate; see .env.example."
        )
    return url


def run_migrations_offline() -> None:
    """Emit the SQL to stdout instead of running it, for review or for a DBA to apply."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # `compare_server_default` is off by default, and with it off a column whose default has
        # drifted from its migration is a change that `alembic check` and `--autogenerate` do not
        # see at all. `job.run_after DEFAULT now()` is the difference between a job that is
        # immediately claimable and one that is never claimed, so it has to be seen.
        #
        # `compare_type` has been on by default since Alembic 1.12. Naming it pins the comparison
        # rather than inheriting whatever the default becomes next.
        #
        # The drift test in tests/test_models.py configures its own context and must carry the same
        # two options; it does not reach this function.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
