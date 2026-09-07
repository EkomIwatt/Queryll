"""Async engine and session plumbing.

The worker holds a small pool: it processes one document at a time and its long waits are
on the Voyage API, not on Postgres. Progress updates commit in their own short transactions
so that Instance 2 — polled by the browser every two seconds — can actually see them while
the run is still in flight.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Build the asyncpg engine.

    Note what is deliberately *not* here: `pgvector.asyncpg.register_vector`. The SQLAlchemy
    `Vector` type already renders a vector to its text form on the way out and parses it on
    the way back, so installing the driver-level codec as well makes asyncpg receive a string
    where it expects a sequence of floats, and every chunk insert fails at the driver. One
    conversion, in one place.
    """
    return create_async_engine(
        database_url,
        echo=echo,
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction. Commits on clean exit, rolls back on any exception."""
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def check_connectivity(engine: AsyncEngine) -> None:
    """Fail fast at startup rather than on the first claimed job."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
