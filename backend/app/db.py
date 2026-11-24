"""Database access layer (async SQLAlchemy over asyncpg)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

#: What a unit of work returns.
T = TypeVar("T")


class Database:
    """Owns the async engine and session factory for the process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._session_factory

    async def connect(self) -> None:
        """Create the engine and session factory, and ensure the schema exists."""
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            self._settings.database_url,
            pool_size=self._settings.db_pool_max_size,
            max_overflow=0,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        await self._create_schema()
        logger.info("database engine created")

    async def _create_schema(self) -> None:
        """Create any missing tables.

        The schema is intentionally tiny and owned by us, so creating it at
        startup is the simplest reproducible way to get from a fresh Postgres
        volume to a working platform.
        """
        from .models import Base

        try:
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("database schema ensured")
        except Exception as exc:  # startup must not hard-fail on a DB hiccup
            logger.warning("could not create schema: %s", exc)

    async def disconnect(self) -> None:
        """Dispose the engine."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("database engine disposed")

    async def ping(self) -> bool:
        """Return True when the database answers a trivial query."""
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # health check must never raise
            logger.warning("database ping failed: %s", exc)
            return False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, rolling back on error.

        This is the **read** path, and it is cheap: a read cannot deadlock,
        so there is nothing to retry. Writes go through :meth:`write`,
        which can.
        """
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def write(self, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
        """Run a unit of work in a transaction, retrying a deadlock.

        **Deadlocks are retried.** Postgres detects a deadlock when two
        transactions each hold a lock the other wants, and it kills one of
        them. With several writers — the sync service persisting the
        decision history while an operator approves a recommendation —
        that happens regularly under load, and the error it raises is
        ``40001``, which the SQL standard says the application should
        retry.

        Retrying is therefore not papering over the problem: it is
        following the standard's own instruction. The **whole** unit of
        work is retried, not just the failing statement, because Postgres
        leaves an aborted transaction unusable — every further statement
        in it fails until it is rolled back. Retrying only the statement
        would therefore fail for a different reason and hide the real one.

        The work is rolled back before the retry, so it starts from a
        clean state, and the retries are bounded. A deadlock that
        survives them is raised to the caller, which logs it and carries
        on — the platform keeps serving.
        """
        attempts = self._settings.db_deadlock_retries + 1
        for attempt in range(attempts):
            try:
                async with self.session_factory() as session:
                    try:
                        result = await work(session)
                        await session.commit()
                        return result
                    except Exception:
                        await session.rollback()
                        raise
            except Exception as exc:
                if not _is_deadlock(exc) or attempt == attempts - 1:
                    raise
                # Back off a little, so the retried transaction does not
                # immediately collide with the same other transaction.
                await asyncio.sleep(0.05 * (attempt + 1))
                logger.warning(
                    "database deadlock; retrying",
                    extra={"attempt": attempt + 1},
                )
        raise RuntimeError("the retry loop ended without a result")


database = Database()


def _is_deadlock(exc: BaseException) -> bool:
    """Whether an exception is a Postgres deadlock or a lock timeout.

    Both are ``40001``: "the transaction was aborted to save the whole
    database from a deadlock, or the lock wait was too long". Both are
    transient, and the SQL standard says the application should retry
    them.
    """
    # asyncpg raises ``asyncpg.exceptions.DeadlockDetectedError`` for a
    # deadlock and ``asyncpg.exceptions.LockNotAvailableError`` for a
    # lock timeout. Neither is imported here so the module keeps working
    # without asyncpg, so the check is on the class name and the SQLSTATE.
    if getattr(exc, "sqlstate", None) == "40001":
        return True
    if getattr(getattr(exc, "orig", None), "sqlstate", None) == "40001":
        return True
    name = type(exc).__name__.lower()
    if "deadlock" in name or "locknotavailable" in name:
        return True
    return "deadlock detected" in str(exc).lower()
