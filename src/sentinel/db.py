"""The async engine and session factory the whole process shares.

`Settings.database_url` is already constrained to the `postgresql+asyncpg` scheme, so the engine
here is always the asyncio one. The module holds no models; `sentinel.models` holds no connection.

Callers take a session from `session_scope()`, which is one unit of work: it commits on a clean
exit and rolls back on an exception. The webhook path depends on that being a single transaction —
the state change, the `remediation_event` and the enqueued job are written together or not at all.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinel.config import Settings, get_settings


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build an engine for `settings`, defaulting to this process's configuration.

    Tests pass their own settings; nothing else needs to.
    """
    settings = get_settings() if settings is None else settings
    return create_async_engine(
        settings.database_url.get_secret_value(),
        # A pooled connection can outlive the server it was opened against — a Postgres restart, a
        # failover, an idle timeout on a proxy. One round trip on checkout turns the resulting
        # `ConnectionDoesNotExistError`, raised in the middle of a handler, into a fresh connection.
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to `engine`."""
    return async_sessionmaker(
        engine,
        # With the default, committing expires every loaded attribute, and the next attribute
        # access emits a lazy SELECT. Under asyncio that is not a slow read but a `MissingGreenlet`
        # raised from ordinary attribute access, typically after the handler has already returned.
        expire_on_commit=False,
    )


@cache
def get_engine() -> AsyncEngine:
    """This process's engine, created on first use and pooled from then on."""
    return create_engine()


@cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """This process's session factory."""
    return create_session_factory(get_engine())


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any exception."""
    async with get_session_factory()() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        await session.commit()


async def dispose_engine() -> None:
    """Close the pooled connections, on shutdown.

    The caches are cleared too, so a process that keeps running builds a fresh engine rather than
    handing out one whose pool is closed.
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
