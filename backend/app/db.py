"""Database access layer (async SQLAlchemy over asyncpg)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


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
        """Yield a session, rolling back on error."""
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise


database = Database()
