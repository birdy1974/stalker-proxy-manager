"""
Dual logging: every message goes to stdout (visible in Portainer / docker logs)
AND into the `logs` table for the dashboard messages pane.

Usage: await db_log("INFO", "portal", "portal X resolved to ...")
Retrieval is paginated/filterable (GUI messages pane) and a retention cleanup
runs at startup (keeps the newest N rows, default 5000).

The DB half runs on a dedicated writer task, not in the caller's task:

  * Stream teardown (client changed channel / player gave up) happens while the
    ASGI request task is being cancelled, and a session closed under
    cancellation makes SQLAlchemy log "Exception terminating connection" and
    drop the pooled connection - and the row never lands either. Handing the
    row to a queue costs the caller nothing and cannot be interrupted.
  * One INSERT + COMMIT per log line, inline in the byte pump, also meant a
    round trip to Postgres for every "fallback step" line. The writer batches.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete, select

from ..database import SessionLocal, run_uncancelled, spawn
from ..models import Log

pylog = logging.getLogger("spm")
RETENTION_ROWS = 5000

_QUEUE_MAX = 2000          # bounded: a wedged database must not eat all memory
_BATCH_MAX = 50            # rows per INSERT/COMMIT
_writer: asyncio.Task | None = None
_queue: asyncio.Queue | None = None
_dropped = 0


def _q() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    return _queue


async def _write_batch(batch: list[tuple[str, str, str]]) -> None:
    try:
        async with SessionLocal() as s:
            s.add_all(Log(level=lvl, module=mod[:40], message=msg[:4000])
                      for lvl, mod, msg in batch)
            await s.commit()
    except Exception:  # noqa: BLE001 - logging must never crash the app
        pylog.exception("failed to persist %d log row(s)", len(batch))


async def _writer_loop() -> None:
    q = _q()
    while True:
        batch = [await q.get()]
        try:                                   # cheap opportunistic batching
            while len(batch) < _BATCH_MAX:
                batch.append(q.get_nowait())
        except asyncio.QueueEmpty:
            pass
        try:
            # shielded: a shutdown cancel must not abort the commit halfway
            await run_uncancelled(_write_batch(batch), what="log batch commit")
        finally:
            for _ in batch:
                q.task_done()


def _enqueue(level: str, module: str, message: str) -> None:
    """Never blocks, never raises, never touches the caller's DB connection."""
    global _writer
    q = _q()
    if _writer is None or _writer.done():
        _writer = spawn(_writer_loop(), name="spm-log-writer")
    try:
        q.put_nowait((level, module, message))
    except asyncio.QueueFull:
        global _dropped
        _dropped += 1
        if _dropped == 1 or _dropped % 200 == 0:
            pylog.warning("log queue full (%d queued): dropped %d log row(s) so far "
                          "- the database is not keeping up", q.qsize(), _dropped)


async def db_log(level: str, module: str, message: str) -> None:
    level = level.upper()
    # stdout first (Portainer), always
    getattr(pylog, {"DEBUG": "debug", "INFO": "info", "WARNING": "warning",
                    "ERROR": "error"}.get(level, "info"))("[%s] %s", module, message)
    _enqueue(level, module, message)


async def flush_logs(timeout: float = 10.0) -> None:
    """Wait until every queued row is committed (shutdown, tests, exports)."""
    if _queue is None or _queue.empty():
        return
    try:
        await asyncio.wait_for(_queue.join(), timeout)
    except asyncio.TimeoutError:
        pylog.warning("log queue still has %d row(s) after %.0fs", _queue.qsize(), timeout)
    except Exception:  # noqa: BLE001 - flushing must never break shutdown
        pylog.exception("flushing the log queue failed")


async def stop_log_writer() -> None:
    global _writer
    await flush_logs()
    if _writer is not None and not _writer.done():
        _writer.cancel()
    _writer = None


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
