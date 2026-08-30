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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.database import engine  # noqa: E402
from app.services.db_logging import flush_logs  # noqa: E402


@pytest.fixture(autouse=True)
async def _schema_and_flush():
    """Fresh tables per test - quietly, so the NEXT test is not punished for them.

    `drop_all` needs SQLite's exclusive lock, so it fails with `database is
    locked` if anything still holds a shared one. Two things legitimately do, at
    the moment a test ends:

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

    await flush_logs()
    last: Exception | None = None
    for attempt in range(6):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
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


@pytest.fixture
async def pool_errors():
    """Collect ERROR records from SQLAlchemy's pool logger (the noise in the
    bug report comes from there, so the tests watch exactly that logger)."""
    import logging

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
    await POOL.close_all()
    POOL.hits = POOL.misses = 0
    yield
    await POOL.close_all()
    POOL.hits = POOL.misses = 0
