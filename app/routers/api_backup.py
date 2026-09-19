"""Admin-only backup inspection, additive restore, and explicit data deletion."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from ..database import get_db
from ..security import require_admin
from ..services import backup
from ..services.branding import refresh as refresh_favicon

router = APIRouter(prefix="/api/backup", tags=["backup"], dependencies=[Depends(require_admin)])
_lock = asyncio.Lock()


@asynccontextmanager
async def checked(db):
    try:
        yield
    except (ValueError, TypeError, KeyError) as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from None
    except (IntegrityError, StatementError):
        await db.rollback()
        # Database exceptions can include credentials from the inserted row.
        raise HTTPException(409, "Backup conflicts with database constraints. Nothing was restored; check identities and references.") from None


def keys(payload):
    value = payload.get("setting_keys")
    if value is not None and (not isinstance(value, list) or not value or
                              any(not isinstance(k, str) or not k for k in value)):
        raise ValueError("Select at least one setting key.")
    return value


async def snapshot(db):
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    elif db.bind.dialect.name == "sqlite":
        # sqlite's legacy transaction mode does not begin a transaction for
        # SELECT by itself. Hold a consistent read snapshot across all tables.
        await db.execute(text("BEGIN"))


@router.get("/catalog")
async def catalog(db=Depends(get_db)):
    tables = []
    for name, table in backup.TABLES.items():
        tables.append({"name": name, "rows": await db.scalar(select(func.count()).select_from(table)),
                       "columns": list(table.c.keys()), "runtime_only": name == "active_streams"})
    settings = backup.TABLES["settings"]
    return {"tables": sorted(tables, key=lambda t: t["name"]),
            "setting_keys": list((await db.execute(select(settings.c.key).order_by(settings.c.key))).scalars()),
            "notes": backup.NOTES}


@router.post("/export")
async def export(payload: dict, db=Depends(get_db)):
    async with checked(db):
        await snapshot(db)
        data = await backup.export(db, payload.get("tables", list(backup.TABLES)), keys(payload))
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="spm-backup-v2.json"',
                                           "Cache-Control": "no-store"})


@router.post("/preview")
async def preview(payload: dict, db=Depends(get_db)):
    async with _lock, checked(db):
        # Execute the exact restore inside a rolled-back savepoint, including
        # constraint checks. This changes no stored values, even on failure.
        async with db.begin_nested() as transaction:
            result = await backup.restore(db, payload.get("data"), payload.get("tables"), keys(payload))
            await transaction.rollback()
        return result


@router.post("/restore")
async def restore(payload: dict, db=Depends(get_db)):
    if payload.get("confirm_add_only") is not True:
        raise HTTPException(400, "Confirm that restore only adds missing information and leaves existing values unchanged.")
    async with _lock, checked(db):
        result = await backup.restore(db, payload.get("data"), payload.get("tables"), keys(payload))
        await db.commit()
    await refresh_favicon()
    return result


def delete_selection(payload):
    if payload.get("all") is True:
        return list(backup.TABLES), None
    return backup.table_names(payload.get("tables")), keys(payload)


async def delete_plan(db, names, setting_keys):
    order, filters, nulls = backup.deletion_filters(names, setting_keys)
    affected = []
    for table in order:
        count = 0
        if table.name in filters:
            count = await db.scalar(select(func.count()).select_from(table).where(filters[table.name]))
        cleared = 0
        if table.name in nulls:
            condition = or_(*nulls[table.name])
            if table.name in filters:
                condition &= ~filters[table.name]
            cleared = await db.scalar(select(func.count()).select_from(table).where(condition))
        if count or cleared or table.name in names:
            affected.append({"name": table.name, "deleted": count, "references_cleared": cleared,
                             "selected": table.name in names})
    return order, filters, {"tables": affected, "deleted": sum(t["deleted"] for t in affected)}


@router.post("/delete-preview")
async def preview_delete(payload: dict, db=Depends(get_db)):
    async with checked(db):
        names, setting_keys = delete_selection(payload)
        _, _, result = await delete_plan(db, names, setting_keys)
        return result


@router.post("/delete")
async def delete_data(payload: dict, db=Depends(get_db)):
    expected = "DELETE EVERYTHING" if payload.get("all") is True else "DELETE SELECTED"
    if payload.get("confirmation") != expected or payload.get("confirm_delete") is not True:
        raise HTTPException(400, f"Confirm deletion and type {expected} exactly.")
    from ..services.fetch_jobs import list_jobs
    from ..services.stream_manager import MANAGER
    if MANAGER.list() or any(j["status"] in ("queued", "running") for j in list_jobs()):
        raise HTTPException(409, "Stop active streams and wait for queued/running fetch jobs before deleting data.")
    from ..services.db_logging import flush_logs
    await flush_logs()
    async with _lock, checked(db):
        names, setting_keys = delete_selection(payload)
        order, filters, result = await delete_plan(db, names, setting_keys)
        # Children go first, leaving the parent subqueries intact until used.
        for table in reversed(order):
            if table.name in filters:
                await db.execute(table.delete().where(filters[table.name]))
        await db.commit()
    from ..portal.pool import POOL
    await POOL.close_all()
    await refresh_favicon()
    return result
