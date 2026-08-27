"""
EPG API (Phase 3): EPG source CRUD + refresh + auto-match controls.
Backend for the Settings page EPG section.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from ..database import get_db
from ..models import EpgChannel, EpgProgramme, EpgSource, LivePlaylist
from ..security import require_admin
from ..services import epg as epg_svc
from ..services.db_logging import db_log

router = APIRouter(prefix="/api/epg", tags=["epg"], dependencies=[Depends(require_admin)])

_refresh_task: asyncio.Task | None = None


class SourceIn(BaseModel):
    url: str


class ToggleIn(BaseModel):
    enabled: bool


class AssignIn(BaseModel):
    epg_id: str | None


def _src_json(r: EpgSource) -> dict:
    return {"id": r.id, "url": r.url, "enabled": r.enabled,
            "last_fetch": r.last_fetch.isoformat() if r.last_fetch else None,
            "status": r.status, "channel_count": r.channel_count}


@router.get("")
async def overview(db=Depends(get_db)):
    sources = (await db.execute(select(EpgSource).order_by(EpgSource.id))).scalars().all()
    n_channels = await db.scalar(select(func.count()).select_from(EpgChannel)) or 0
    n_prog = await db.scalar(select(func.count()).select_from(EpgProgramme)) or 0
    n_live = await db.scalar(select(func.count()).select_from(LivePlaylist)) or 0
    n_matched = await db.scalar(select(func.count()).select_from(LivePlaylist).where(
        LivePlaylist.epg_id.isnot(None), LivePlaylist.epg_id != "")) or 0
    return {"sources": [_src_json(r) for r in sources],
            "epg_channels": n_channels, "programmes": n_prog,
            "playlist_channels": n_live, "matched": n_matched,
            "refresh_running": bool(_refresh_task and not _refresh_task.done())}


@router.post("/sources")
async def add_source(body: SourceIn, db=Depends(get_db)):
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    exists = await db.scalar(select(EpgSource).where(EpgSource.url == url))
    if exists:
        raise HTTPException(409, "source already exists")
    row = EpgSource(url=url, enabled=True)
    db.add(row)
    await db.commit()
    await db_log("INFO", "epg", f"EPG source added: {url}")
    return {"id": row.id}


@router.delete("/sources/{src_id}")
async def delete_source(src_id: int, db=Depends(get_db)):
    row = await db.get(EpgSource, src_id)
    if not row:
        raise HTTPException(404, "not found")
    (await db.execute(EpgChannel.__table__.delete().where(EpgChannel.epg_source_id == src_id)))
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.patch("/sources/{src_id}")
async def toggle_source(src_id: int, body: ToggleIn, db=Depends(get_db)):
    row = await db.get(EpgSource, src_id)
    if not row:
        raise HTTPException(404, "not found")
    row.enabled = body.enabled
    await db.commit()
    return {"ok": True, "enabled": row.enabled}


@router.post("/sources/{src_id}/refresh")
async def refresh_source_endpoint(src_id: int, db=Depends(get_db)):
    row = await db.get(EpgSource, src_id)
    if not row:
        raise HTTPException(404, "not found")

    async def _run():
        await epg_svc.refresh_source(src_id)

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@router.post("/refresh")
async def refresh_all_endpoint():
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        return {"ok": False, "error": "refresh already running"}
    _refresh_task = asyncio.create_task(epg_svc.refresh_all())
    return {"ok": True, "started": True}


@router.post("/match")
async def match_endpoint():
    n = await epg_svc.match_epg_to_playlist()
    return {"ok": True, "matched": n}


@router.get("/channels")
async def list_epg_channels(q: str = "", page: int = 1, per_page: int = 50, db=Depends(get_db)):
    stmt = select(EpgChannel).order_by(EpgChannel.name)
    if q:
        stmt = stmt.where(EpgChannel.name.ilike(f"%{q}%"))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return {"total": total or 0,
            "rows": [{"tvg_id": r.tvg_id, "name": r.name, "icon": r.icon,
                      "source_id": r.epg_source_id} for r in rows]}


@router.patch("/channels/assign/{live_id}")
async def manual_assign(live_id: int, body: AssignIn, db=Depends(get_db)):
    """Manual override: assign (or clear) an EPG tvg-id for a playlist channel."""
    row = await db.get(LivePlaylist, live_id)
    if not row:
        raise HTTPException(404, "not found")
    row.epg_id = (body.epg_id or "").strip() or None
    await db.commit()
    return {"ok": True, "epg_id": row.epg_id}
