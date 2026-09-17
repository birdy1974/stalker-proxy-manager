"""
Test environment. Must run before anything imports `app.*`: app.config reads
SPM_DATA_DIR / SPM_DATABASE_URL at import time and builds the engine from them.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

_DATA = tempfile.mkdtemp(prefix="spm-tests-")
os.environ["SPM_DATA_DIR"] = _DATA
os.environ["SPM_DATABASE_URL"] = f"sqlite+aiosqlite:///{pathlib.Path(_DATA, 'spm.db').as_posix()}"
os.environ["SPM_MOCK_PORTAL"] = "0"
os.environ["SPM_ADMIN_PASSWORD"] = "test-admin"
os.environ["SPM_SKIP_LOGIN"] = "1"   # the API tests call admin endpoints directly
# >>> redirect-guard: the experiment stays OFF suite-wide; tests/test_redirect_guard.py
# re-enables it per test. Delete these lines with app/services/redirect_guard.py.
os.environ["SPM_REDIRECT_VALIDATE"] = "0"
os.environ["SPM_REOPEN_DEMOTE"] = "0"
# <<< redirect-guard

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.database import engine  # noqa: E402
from app.services.db_logging import flush_logs  # noqa: E402


#: Fingerprint of the schema as `create_all` left it. Comparing it per test is
#: what lets the common case skip DDL entirely - see `_reset_sync`.
_SCHEMA: dict[str, tuple | None] = {"fingerprint": None}


def _sqlite_fingerprint(sync_conn) -> tuple:
    """Every table/index/view and its DDL, as one comparable value."""
    rows = sync_conn.exec_driver_sql(
        "SELECT type, name, tbl_name, ifnull(sql, '') FROM sqlite_master").fetchall()
    return tuple(sorted(tuple(r) for r in rows))


def _reset_sync(sync_conn) -> bool:
    """Clear the database for the next test. False = the DDL must be rebuilt.

    Rebuilding 31 tables costs ~58 ms per test - ~40 s of the suite - while
    deleting their rows costs ~6 ms and leaves every test with exactly the same
    empty database (SQLite reuses row ids after a delete, so ids still start at
    1). The schema is only rebuilt when a test actually changed it: the
    fingerprint below is what the migration tests (which drop columns and invent
    legacy tables on purpose) trip, and only those few tests pay for it.
    """
    from app import models

    if _sqlite_fingerprint(sync_conn) != _SCHEMA["fingerprint"]:
        return False
    for table in reversed(models.Base.metadata.sorted_tables):
        sync_conn.execute(table.delete())
    # AUTOINCREMENT tables keep their high-water mark in sqlite_sequence; a
    # fresh schema started these at 1, and so must we.
    if sync_conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        sync_conn.exec_driver_sql("DELETE FROM sqlite_sequence")
    return True


@pytest.fixture(autouse=True)
async def _schema_and_flush():
    """Fresh database per test - cheaply, so the suite stays worth running.

    The empty database is produced by deleting rows (fast path) whenever the
    previous test left the schema alone; `drop_all` + `create_all` still runs
    for the first test in a process and for any test that changed the schema.

    The rebuild path also needs SQLite's exclusive lock, so it fails with
    `database is locked` if anything still holds a shared one. Two things
    legitimately do, at the moment a test ends:

    * log rows queued for the writer task, which commits in its OWN session a
      moment after the test that produced them is over -> drain the queue first;
    * a connection checked out and then abandoned, which is exactly what
      `test_get_db_survives_an_abandoned_request` is written to create. It is not
      returned to the pool, so `dispose()` cannot reclaim it; only the garbage
      collector terminating it releases the lock -> collect, yield to the
      aiosqlite thread, and retry the DDL a few times while that happens.

    Before this, one intentional leak turned into a dozen `ERROR at setup` entries
    in whatever files happened to run after it - a suite whose failures depend on
    file order teaches you nothing about your change.
    """
    import gc

    from app import models
    from app.services.playlist_gen import clear_m3u_cache

    await flush_logs()
    clear_m3u_cache()                  # the schema reset zeros ids; cached M3Us must die
    last: Exception | None = None
    for attempt in range(6):
        try:
            if engine.dialect.name == "sqlite" and _SCHEMA["fingerprint"] is not None:
                async with engine.begin() as conn:
                    cleared = await conn.run_sync(_reset_sync)
                if cleared:
                    break
            async with engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
                if engine.dialect.name == "sqlite":
                    _SCHEMA["fingerprint"] = await conn.run_sync(_sqlite_fingerprint)
            break
        except Exception as exc:  # noqa: BLE001 - only "locked" is retryable
            text = str(exc).lower()
            if "locked" not in text:
                raise
            last = exc
            await engine.dispose()
            gc.collect()
            await asyncio.sleep(0.05 * (attempt + 1))
    else:
        raise AssertionError(
            "schema reset kept losing the SQLite file lock; a test is leaking a "
            f"session instead of closing it: {last}")
    yield
    await flush_logs()
    clear_m3u_cache()


@pytest.fixture
async def pool_errors():
    """Collect ERROR records from SQLAlchemy's pool logger (the noise in the
    bug report comes from there, so the tests watch exactly that logger)."""
    import gc
    import logging

    # Drain cyclic garbage BEFORE attaching the handler: a pooled connection
    # an earlier test abandoned mid-cancellation would otherwise be collected
    # (and warn) at whatever later test first trips a GC threshold - landing
    # in this test's records and flaking it. Draining here attributes every
    # warning to the test whose garbage actually produced it.
    gc.collect()

    records: list[str] = []

    class _Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("sqlalchemy.pool")
    handler, old_level = _Catch(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture(autouse=True)
async def _reset_portal_pool():
    """Reset the shared portal session pool around every test.

    The pool is a process-wide singleton, so without this a fake client
    installed by one test would be handed to the next one.
    """
    from app.portal.pool import POOL
    from app.services.stream_manager import MANAGER, _RouteHealth
    await POOL.close_all()
    POOL.hits = POOL.misses = 0
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()          # who held the lease, not just until when
    MANAGER.route_health = _RouteHealth()
    yield
    await POOL.close_all()
    POOL.hits = POOL.misses = 0
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.route_health = _RouteHealth()
