"""Areas: named playback profiles (FFmpeg overlay on the one playlist)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from ..database import get_db
from ..models import (
    Area, AreaItemTemplate, FFmpegTemplate, LivePlaylist, LocalPlaylist,
    SeriePlaylist, User, VodPlaylist,
)
from ..security import require_admin
from ..services.db_logging import db_log
from ..services.playback import AREA_KINDS, KIND_DEFAULT_COL

router = APIRouter(prefix="/api/areas", tags=["areas"], dependencies=[Depends(require_admin)])

_PL = {
    "live": (LivePlaylist, LivePlaylist.custom_name),
    "vod": (VodPlaylist, VodPlaylist.custom_name),
    "series": (SeriePlaylist, SeriePlaylist.custom_name),
    "local": (LocalPlaylist, LocalPlaylist.custom_name),
}
_FIELDS = ("name", "enabled", "notes", *KIND_DEFAULT_COL.values())


def _tpl_int(value):
    if value in (None, "", 0, "0"):
        return None
    return int(value)


def _apply(area: Area, payload: dict) -> None:
    if "name" in payload:
        area.name = (payload.get("name") or "").strip()
    if "enabled" in payload:
        area.enabled = bool(payload["enabled"])
    if "notes" in payload:
        area.notes = (payload.get("notes") or "").strip() or None
    for col in KIND_DEFAULT_COL.values():
        if col in payload:
            setattr(area, col, _tpl_int(payload.get(col)))


def _row(a: Area, user_count: int = 0, tpls: dict[int, str] | None = None) -> dict:
    tpls = tpls or {}

    def named(tid):
        return tpls.get(tid) if tid else None

    return {
        "id": a.id, "name": a.name, "enabled": a.enabled, "notes": a.notes or "",
        "user_count": user_count,
        "ffmpeg_template_live_id": a.ffmpeg_template_live_id,
        "ffmpeg_template_vod_id": a.ffmpeg_template_vod_id,
        "ffmpeg_template_series_id": a.ffmpeg_template_series_id,
        "ffmpeg_template_local_id": a.ffmpeg_template_local_id,
        "ffmpeg_template_live": named(a.ffmpeg_template_live_id),
        "ffmpeg_template_vod": named(a.ffmpeg_template_vod_id),
        "ffmpeg_template_series": named(a.ffmpeg_template_series_id),
        "ffmpeg_template_local": named(a.ffmpeg_template_local_id),
    }


async def _tpls(db) -> dict[int, str]:
    return {t.id: t.name for t in (await db.execute(select(FFmpegTemplate))).scalars().all()}


@router.get("")
async def list_areas(db=Depends(get_db)):
    areas = (await db.execute(select(Area).order_by(Area.name))).scalars().all()
    tpls = await _tpls(db)
    counts: dict[int, int] = {}
    if areas:
        for aid, n in (await db.execute(
                select(User.area_id, func.count()).where(
                    User.area_id.in_([a.id for a in areas])).group_by(User.area_id))).all():
            counts[aid] = n
    templates = [{"id": i, "name": n} for i, n in sorted(tpls.items(), key=lambda kv: kv[1].lower())]
    return {"items": [_row(a, counts.get(a.id, 0), tpls) for a in areas],
            "templates": templates}


@router.post("")
async def create_area(payload: dict, db=Depends(get_db)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if (await db.execute(select(Area).where(Area.name == name))).scalar_one_or_none():
        raise HTTPException(409, "area exists")
    a = Area(name=name, enabled=True)
    _apply(a, payload)
    if not a.name:
        raise HTTPException(400, "name required")
    db.add(a)
    await db.commit()
    await db.refresh(a)
    await db_log("INFO", "areas", f"area '{a.name}' created")
    return {"id": a.id, "item": _row(a, 0, await _tpls(db))}


@router.put("/{aid}")
async def update_area(aid: int, payload: dict, db=Depends(get_db)):
    a = await db.get(Area, aid)
    if not a:
        raise HTTPException(404, "area not found")
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        other = (await db.execute(select(Area).where(Area.name == name, Area.id != aid))
                 ).scalar_one_or_none()
        if other:
            raise HTTPException(409, "area exists")
    _apply(a, payload)
    await db.commit()
    return {"ok": True}


@router.delete("/{aid}")
async def delete_area(aid: int, db=Depends(get_db)):
    a = await db.get(Area, aid)
    if not a:
        raise HTTPException(404, "area not found")
    name = a.name
    await db.delete(a)
    await db.commit()
    await db_log("WARNING", "areas", f"area '{name}' deleted")
    return {"ok": True}


@router.post("/{aid}/duplicate")
async def duplicate_area(aid: int, db=Depends(get_db)):
    src = await db.get(Area, aid)
    if not src:
        raise HTTPException(404, "area not found")
    base = f"{src.name} copy"
    name, n = base, 2
    while (await db.execute(select(Area).where(Area.name == name))).scalar_one_or_none():
        name = f"{base} {n}"
        n += 1
    dst = Area(name=name, enabled=src.enabled, notes=src.notes,
               ffmpeg_template_live_id=src.ffmpeg_template_live_id,
               ffmpeg_template_vod_id=src.ffmpeg_template_vod_id,
               ffmpeg_template_series_id=src.ffmpeg_template_series_id,
               ffmpeg_template_local_id=src.ffmpeg_template_local_id)
    db.add(dst)
    await db.flush()
    rows = (await db.execute(select(AreaItemTemplate).where(
        AreaItemTemplate.area_id == src.id))).scalars().all()
    for r in rows:
        db.add(AreaItemTemplate(area_id=dst.id, kind=r.kind, playlist_id=r.playlist_id,
                                ffmpeg_template_id=r.ffmpeg_template_id))
    await db.commit()
    await db.refresh(dst)
    await db_log("INFO", "areas", f"area '{src.name}' duplicated as '{dst.name}'")
    return {"id": dst.id, "item": _row(dst, 0, await _tpls(db))}


@router.get("/{aid}/items")
async def list_exceptions(aid: int, db=Depends(get_db)):
    a = await db.get(Area, aid)
    if not a:
        raise HTTPException(404, "area not found")
    rows = (await db.execute(select(AreaItemTemplate).where(
        AreaItemTemplate.area_id == aid).order_by(AreaItemTemplate.kind,
                                                  AreaItemTemplate.id))).scalars().all()
    tpls = await _tpls(db)
    names: dict[tuple[str, int], str] = {}
    for kind, (model, col) in _PL.items():
        ids = [r.playlist_id for r in rows if r.kind == kind]
        if not ids:
            continue
        for pid, name in (await db.execute(select(model.id, col).where(model.id.in_(ids)))).all():
            names[(kind, pid)] = name or f"#{pid}"
    items = []
    for r in rows:
        items.append({
            "id": r.id, "kind": r.kind, "playlist_id": r.playlist_id,
            "name": names.get((r.kind, r.playlist_id), f"#{r.playlist_id} (missing)"),
            "missing": (r.kind, r.playlist_id) not in names,
            "ffmpeg_template_id": r.ffmpeg_template_id,
            "template": tpls.get(r.ffmpeg_template_id, ""),
        })
    return {"items": items}


@router.post("/{aid}/items")
async def upsert_exceptions(aid: int, payload: dict, db=Depends(get_db)):
    """Add/replace exceptions. Body: {kind, playlist_id, ffmpeg_template_id}
    or bulk {items: [{kind, playlist_id}, ...], ffmpeg_template_id}."""
    a = await db.get(Area, aid)
    if not a:
        raise HTTPException(404, "area not found")
    tid = _tpl_int(payload.get("ffmpeg_template_id"))
    if not tid:
        raise HTTPException(400, "ffmpeg_template_id required")
    tpl = await db.get(FFmpegTemplate, tid)
    if not tpl:
        raise HTTPException(404, "template not found")
    specs = payload.get("items")
    if specs is None:
        specs = [{"kind": payload.get("kind"), "playlist_id": payload.get("playlist_id")}]
    n = 0
    for spec in specs:
        kind = (spec.get("kind") or "").strip()
        if kind not in AREA_KINDS:
            raise HTTPException(400, "kind must be live|vod|series|local")
        pid = int(spec.get("playlist_id") or 0)
        if not pid:
            raise HTTPException(400, "playlist_id required")
        existing = (await db.execute(select(AreaItemTemplate).where(
            AreaItemTemplate.area_id == aid, AreaItemTemplate.kind == kind,
            AreaItemTemplate.playlist_id == pid))).scalar_one_or_none()
        if existing:
            existing.ffmpeg_template_id = tid
        else:
            db.add(AreaItemTemplate(area_id=aid, kind=kind, playlist_id=pid,
                                    ffmpeg_template_id=tid))
        n += 1
    await db.commit()
    return {"ok": True, "count": n}


@router.delete("/{aid}/items/{eid}")
async def delete_exception(aid: int, eid: int, db=Depends(get_db)):
    row = await db.get(AreaItemTemplate, eid)
    if not row or row.area_id != aid:
        raise HTTPException(404, "exception not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.post("/{aid}/items/bulk-delete")
async def bulk_delete_exceptions(aid: int, payload: dict, db=Depends(get_db)):
    a = await db.get(Area, aid)
    if not a:
        raise HTTPException(404, "area not found")
    ids = [int(x) for x in payload.get("ids", [])]
    rows = (await db.execute(select(AreaItemTemplate).where(
        AreaItemTemplate.area_id == aid, AreaItemTemplate.id.in_(ids)))).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    return {"ok": True, "count": len(rows)}
