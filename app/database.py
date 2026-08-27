"""
Async database plumbing (SQLAlchemy 2.x).

Production runs Postgres (asyncpg); development falls back to SQLite so the app
can run for GUI-mockup testing without a database container. All model code is
engine-agnostic.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from .config import DATABASE_URL, log

# pool settings differ per engine (sqlite has no pool_pre_ping concept issues)
_engine_kwargs: dict = {"echo": False, "future": True}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs.update(pool_size=5, max_overflow=10, pool_pre_ping=True)

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

# Enable WAL + foreign keys on SQLite (big perf + correctness win for dev).
if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragma(dbapi_conn, _):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncSession:  # FastAPI dependency
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables. Safe to call on every startup (IF NOT EXISTS semantics)."""
    from . import models  # noqa: F401  (register metadata)

    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    log.info("Database schema ensured (%d tables)", len(models.Base.metadata.tables))
