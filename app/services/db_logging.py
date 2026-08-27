"""
Dual logging: every message goes to stdout (visible in Portainer / docker logs)
AND into the `logs` table for the dashboard messages pane.

Usage: await db_log("INFO", "portal", "portal X resolved to ...")
Retrieval is paginated/filterable (GUI messages pane) and a retention cleanup
runs at startup (keeps the newest N rows, default 5000).
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from ..database import SessionLocal
from ..models import Log

pylog = logging.getLogger("spm")
RETENTION_ROWS = 5000


async def db_log(level: str, module: str, message: str) -> None:
    level = level.upper()
    # stdout first (Portainer), always
    getattr(pylog, {"DEBUG": "debug", "INFO": "info", "WARNING": "warning", "ERROR": "error"}.get(level, "info"))(
        "[%s] %s", module, message)
    try:
        async with SessionLocal() as s:
            s.add(Log(level=level, module=module[:40], message=message[:4000]))
            await s.commit()
    except Exception:  # noqa: BLE001 - logging must never crash the app
        pylog.exception("failed to persist log row")


async def cleanup_logs(keep: int = RETENTION_ROWS) -> None:
    """Delete all but the newest `keep` log rows (bounded table)."""
    try:
        async with SessionLocal() as s:
            sub = select(Log.id).order_by(Log.id.desc()).limit(keep)
            ids = [r[0] for r in (await s.execute(sub)).all()]
            if ids:
                await s.execute(delete(Log).where(Log.id.not_in(ids)))
                await s.commit()
    except Exception:  # noqa: BLE001
        pylog.exception("log retention cleanup failed")
