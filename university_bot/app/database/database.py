"""Async database engine, session factory, and helpers."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import get_settings
from app.database.models.base import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, echo=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    """Close all pooled connections and release the engine (shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def create_tables() -> None:
    """Create tables directly. Used for dev/Phase 1; Alembic takes over later."""
    from app.database.models import (  # noqa: F401
        assignment,
        course,
        eclass_account,
        group,
        lesson,
        notification,
        user,
    )

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
