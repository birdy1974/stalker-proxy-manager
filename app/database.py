"""
Async database plumbing (SQLAlchemy 2.x).

Production runs Postgres (asyncpg); development falls back to SQLite so the app
can run for GUI-mockup testing without a database container. All model code is
engine-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Coroutine

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


# ---------------------------------------------------------------------------
# Cancellation-safe database work
# ---------------------------------------------------------------------------
# A player that switches channel, or gives up on a slow /playlist.m3u or
# /xmltv.php, makes uvicorn/Starlette cancel the WHOLE ASGI request task - and
# anyio then *re-delivers* that cancellation on every event-loop turn until the
# task is gone (anyio/_backends/_asyncio.py: CancelScope._deliver_cancellation).
#
# Handing a pooled connection back to SQLAlchemy is a chain of awaits (rollback
# -> reset -> checkin), so it gets aborted halfway and the pool logs
#
#   ERROR [sqlalchemy.pool] Exception terminating connection <AdaptedConnection ...>
#   asyncio.exceptions.CancelledError: Cancelled via cancel scope ... by
#   <Task pending name='Task-158' coro=<RequestResponseCycle.run_asgi() ...>
#
# and drops the connection instead of reusing it. On a busy box that is
# connection churn under a pool of 15, plus a wall of scary ERROR lines - and
# the teardown writes themselves (active_streams rows, log rows) silently never
# land, leaving ghost streams on the dashboard until the next restart.
#
# Everything below exists to keep such work outside the dying request's cancel
# scope. Note that a plain `try/except CancelledError` does NOT work here.
_detached: set[asyncio.Task] = set()


def _consume_result(task: asyncio.Task) -> None:
    """Retrieve a detached task's outcome so asyncio stays quiet about it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("detached %s failed: %r", task.get_name(), exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Start a task that outlives its creator (a strong reference is kept, so it
    is neither garbage-collected mid-flight nor orphaned from shutdown)."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _detached.add(task)
    task.add_done_callback(_detached.discard)
    return task


@contextmanager
def _cancel_shield():
    """
    Stop the surrounding cancel scope from delivering cancellations in here.

    anyio keeps calling ``task.cancel()`` every loop turn, so a retry inside a
    plain ``except CancelledError`` is cancelled again immediately. Entering a
    *shielded* scope removes this task from the parent scope's task set; anyio
    restarts cancellation in the parent once we leave, so the CancelledError
    still propagates afterwards - just not while we are cleaning up.
    """
    try:
        from anyio import CancelScope
    except ImportError:                      # pragma: no cover - ships with FastAPI
        yield
        return
    with CancelScope(shield=True):
        yield


async def run_uncancelled(coro: Coroutine[Any, Any, Any], *, timeout: float = 15.0,
                          what: str = "database cleanup") -> Any:
    """
    Await `coro` in a way that survives the calling task being cancelled.

    The coroutine is handed to a fresh task (outside the caller's cancel scope)
    and awaited under a shield, so a client disconnect cannot abort it halfway
    and cannot interrupt the connection it returns to the pool. A bare
    ``Task.cancel()`` (uvicorn's forced shutdown) gets past anyio's shield, so
    in that case the work is deliberately left running detached rather than
    killed - we only stop waiting for it.
    """
    task = spawn(coro)
    with _cancel_shield():
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            log.warning("%s did not finish within %.0fs; leaving it running",
                        what, timeout)
            return None
        except asyncio.CancelledError:
            # We are being torn down. Do NOT swallow this - the request task has
            # to unwind or uvicorn/anyio will wait for it forever. The work
            # itself is detached and keeps running; just make sure its result is
            # retrieved so asyncio does not log "Task exception was never
            # retrieved" for a failure nobody will ever await.
            task.add_done_callback(_consume_result)
            raise


class _DisconnectSafeSession(AsyncSession):
    """
    AsyncSession that returns its connection to the pool cleanly even when the
    task owning it has been cancelled.

    `async with SessionLocal()` appears in ~35 places (auth, M3U/XMLTV
    generation, stream bookkeeping) and any of them can be abandoned mid-flight
    by a client that walks away. Fixing it here covers all of them - including
    FastAPI's `get_db` dependency - instead of wrapping every call site.
    """

    async def close(self) -> None:
        try:
            await run_uncancelled(super().close(), what="session close")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Reaching here means the connection was already invalidated (a
            # query was interrupted by the disconnect), so there is nothing
            # left to hand back - the pool already discarded it. Closing is
            # cleanup, so this must never mask the caller's real exception.
            log.debug("session close after cancellation had nothing to release",
                      exc_info=True)


SessionLocal = async_sessionmaker(engine, expire_on_commit=False,
                                  class_=_DisconnectSafeSession)


class _ClientDisconnectFilter(logging.Filter):
    """
    Downgrade one specific, benign pool message.

    If a client disconnects *while a query is in flight*, SQLAlchemy cannot
    know the connection is still sane, so it invalidates and terminates it from
    inside the now-cancelled request task. The terminate is itself interrupted
    and the pool logs `Exception terminating connection` plus a full
    CancelledError traceback at ERROR. Discarding that connection is the right
    thing; the wall of tracebacks is not. Only this exact case (pool logger +
    CancelledError as the exception) is rewritten - every other error, and
    every non-pool CancelledError, is untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("sqlalchemy.pool"):
            return True
        exc = record.exc_info[1] if record.exc_info else None
        if not isinstance(exc, asyncio.CancelledError):
            return True
        record.levelno, record.levelname = logging.INFO, "INFO"
        record.exc_info, record.exc_text = None, None
        args = record.args or ()
        if len(args) == 1:            # "Exception terminating connection %r"
            record.msg = ("Discarding connection %r: client disconnected "
                          "mid-query (expected - the pool opens a fresh one)")
        else:                         # "Exception during reset or similar"
            record.msg = ("Connection reset interrupted by a client disconnect "
                          "(expected - the pool opens a fresh one)")
            record.args = ()
        return True


def _quiet_expected_pool_noise() -> None:
    """Attach the filter to the handlers that actually emit pool records."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _ClientDisconnectFilter) for f in handler.filters):
            handler.addFilter(_ClientDisconnectFilter())


_quiet_expected_pool_noise()


async def get_db() -> AsyncSession:  # FastAPI dependency
    # Session teardown runs inside the request task - the very task that gets
    # cancelled when a client leaves mid-response; _DisconnectSafeSession makes
    # the connection hand-back immune to that.
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all tables. Safe to call on every startup (IF NOT EXISTS semantics)."""
    from . import models  # noqa: F401  (register metadata)

    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
    log.info("Database schema ensured (%d tables)", len(models.Base.metadata.tables))


# Columns introduced after the first release. `create_all` creates tables but
# never alters existing ones, so an upgraded container would otherwise fail
# with "no such column" on the next SELECT. (name, sql-type, default) - the
# default is dialect-specific for booleans, hence the tiny branch below.
_NEW_COLUMNS: dict[str, dict[str, tuple[str, str]]] = {
    "ffmpeg_templates": {
        "low_power": ("BOOLEAN", "1"),
        "rc_mode": ("VARCHAR(10)", "'VBR'"),
        "async_depth": ("VARCHAR(4)", "'4'"),
        "is_builtin": ("BOOLEAN", "0"),
    },
    "local_files": {
        "duration_s": ("FLOAT", "NULL"),
    },
    "serie_sources": {
        "raw_series": ("TEXT", "NULL"),
    },
}


def _add_missing_columns(sync_conn) -> None:
    """Idempotent ALTER TABLEs for columns added after first release."""
    from sqlalchemy import inspect, text

    is_sqlite = sync_conn.dialect.name == "sqlite"
    insp = inspect(sync_conn)
    for table, columns in _NEW_COLUMNS.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, (typ, default) in columns.items():
            if name in existing:
                continue
            if typ == "BOOLEAN" and not is_sqlite:
                default = "TRUE"          # postgres rejects DEFAULT 1 on boolean
            sync_conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN {name} {typ} DEFAULT {default}"))
            log.info("schema: added %s.%s (%s default %s)", table, name, typ, default)
