"""Tests for the database write's deadlock retry.

Postgres kills one side of a deadlock and raises ``40001``. The SQL
standard says the application should retry, so the write helper does.
These tests check that the retry happens, that it is bounded, and that a
non-deadlock error is *not* retried — retrying a genuine failure would
just delay the error the caller needs to see.
"""

from __future__ import annotations

import pytest

from app.db import Database, _is_deadlock


class _FakeSession:
    """A session whose ``execute`` raises whatever the factory says next."""

    def __init__(self, factory: _FakeSessionFactory) -> None:
        self._factory = factory
        self.rollbacks = 0
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, statement) -> None:
        if self._factory.failures:
            raise self._factory.failures.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeSessionFactory:
    """A session factory that fails a fixed number of times."""

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.attempts = 0
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        self.attempts += 1
        session = _FakeSession(self)
        self.sessions.append(session)
        return session


class _Deadlock(Exception):
    """An exception that looks like a Postgres deadlock."""

    sqlstate = "40001"


class _Other(Exception):
    """An exception that is not a deadlock."""


def _database(
    failures: list[BaseException], retries: int = 3
) -> tuple[Database, _FakeSessionFactory]:
    """A database whose sessions fail as instructed."""
    database = Database()
    database._session_factory = _FakeSessionFactory(failures)  # type: ignore[assignment]
    database._settings.db_deadlock_retries = retries  # type: ignore[union-attr]
    return database, database._session_factory  # type: ignore[return-value]


def test_a_deadlock_is_recognised() -> None:
    assert _is_deadlock(_Deadlock()) is True
    assert _is_deadlock(_Other()) is False


@pytest.mark.asyncio
async def test_a_deadlock_is_retried() -> None:
    """The write succeeds on the second attempt."""
    database, factory = _database([_Deadlock()])

    async def work(session) -> str:
        await session.execute("INSERT ...")
        return "done"

    assert await database.write(work) == "done"
    assert factory.attempts == 2


@pytest.mark.asyncio
async def test_a_deadlock_is_retried_a_few_times_then_raised() -> None:
    """A deadlock that never clears is raised, not retried forever."""
    database, factory = _database([_Deadlock(), _Deadlock(), _Deadlock(), _Deadlock()])

    async def work(session) -> None:
        await session.execute("INSERT ...")

    with pytest.raises(_Deadlock):
        await database.write(work)
    # One initial attempt plus three retries.
    assert factory.attempts == 4


@pytest.mark.asyncio
async def test_another_error_is_not_retried() -> None:
    """A genuine failure is raised at once."""
    database, factory = _database([_Other()])

    async def work(session) -> None:
        await session.execute("INSERT ...")

    with pytest.raises(_Other):
        await database.write(work)
    assert factory.attempts == 1


@pytest.mark.asyncio
async def test_the_retry_rolls_the_failed_transaction_back() -> None:
    """The failed attempt is rolled back; the successful one is committed."""
    database, factory = _database([_Deadlock()])

    async def work(session) -> None:
        await session.execute("INSERT ...")

    await database.write(work)
    # The first attempt failed and was rolled back; the second succeeded.
    assert factory.sessions[0].rollbacks == 1
    assert factory.sessions[1].rollbacks == 0
    assert factory.sessions[1].commits == 1


@pytest.mark.asyncio
async def test_the_successful_attempt_is_committed() -> None:
    """The work is committed once, on the attempt that succeeded."""
    database, factory = _database([_Deadlock()])

    async def work(session) -> str:
        await session.execute("INSERT ...")
        return "done"

    await database.write(work)
    assert sum(session.commits for session in factory.sessions) == 1
