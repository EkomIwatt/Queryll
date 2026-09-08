"""Async engine, session factory and the `get_db` dependency.

There is no `create_all` here and no Alembic. `db/init.sql` on `main` is Contract 2 and
is the single source of truth for the schema; this module only connects to it.
"""

from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
            # Render's free tier and Neon both drop idle connections; recycle before
            # the server does so a pooled connection is never handed out dead.
            pool_recycle=1800,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Rolls back on an exception, always closes."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
