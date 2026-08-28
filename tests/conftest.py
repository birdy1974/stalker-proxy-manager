"""
Test environment. Must run before anything imports `app.*`: app.config reads
SPM_DATA_DIR / SPM_DATABASE_URL at import time and builds the engine from them.
"""

from __future__ import annotations

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
    """Fresh tables per test; make sure no log row is left in the queue."""
    from app import models
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.drop_all)
        await conn.run_sync(models.Base.metadata.create_all)
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
