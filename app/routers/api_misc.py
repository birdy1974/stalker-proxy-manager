"""
Dashboard / streams / logs / settings / EPG sources / export-import API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from ..config import FALLBACK_STRATEGY, FETCH_PAGE_BUDGET, OUTPUT_BASE_URL
from ..database import get_db
from ..models import (
    EpgSource, FFmpegTemplate, LiveGenre, LivePlaylist, LiveSource, LocalFile,
    LocalPlaylist, LocalSource, Log, Portal, SerieGenre, SeriePlaylist,
    SerieSource, Setting, User, VodGenre, VodPlaylist, VodSource,
)
from ..security import require_admin
from ..services.db_logging import db_log
from ..services import api_stats
from ..services.fetch_jobs import list_jobs
from ..services.stream_manager import MANAGER

router = APIRouter(prefix="/api", tags=["misc"], dependencies=[Depends(require_admin)])


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
async def dashboard(db=Depends(get_db)):
    async def cnt(model, *where):
        return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    stats = {
        "portals": await cnt(Portal),
        "portals_enabled": await cnt(Portal, Portal.enabled.is_(True)),
        "live_available": await cnt(LiveSource),
        "live_enabled": await cnt(LiveSource, LiveSource.enabled.is_(True)),
        "vod_available": await cnt(VodSource),
        "vod_enabled": await cnt(VodSource, VodSource.enabled.is_(True)),
        "series_available": await cnt(SerieSource),
        "series_enabled": await cnt(SerieSource, SerieSource.enabled.is_(True)),
        "playlist_items": (await cnt(LivePlaylist) + await cnt(VodPlaylist)
                           + await cnt(SeriePlaylist) + await cnt(LocalPlaylist)),
        "ffmpeg_templates": await cnt(FFmpegTemplate, FFmpegTemplate.enabled.is_(True)),
        "users": await cnt(User),
        "local_files": await cnt(LocalFile),
    }
    streams = MANAGER.list()
    # who is using the API right now, per user (drives the "connections" card)
    per_user: dict[str, int] = {}
    for st in streams:
        per_user[st["user_name"] or "-"] = per_user.get(st["user_name"] or "-", 0) + 1
    api = api_stats.snapshot()
    api["streams_active"] = len(streams)
    from ..portal.pool import POOL
    api["portal_sessions"] = POOL.stats()
    api["streams_per_user"] = [{"user": k, "streams": v}
                               for k, v in sorted(per_user.items(), key=lambda kv: -kv[1])]
    return {"stats": stats, "streams": streams, "jobs": list_jobs()[:5], "api": api}


@router.get("/streams")
async def streams():
    return {"items": MANAGER.list()}


@router.post("/streams/{sid}/kill")
async def kill_stream(sid: str):
    return {"ok": await MANAGER.kill(sid)}


@router.post("/streams/kill-all")
async def kill_all_streams():
    return {"killed": await MANAGER.kill_all()}


# ------------------------------------------------------------------ logs
@router.get("/logs")
async def logs(db=Depends(get_db), level: str = "", module: str = "", q: str = "",
               since_minutes: int = 0, page: int = 1, per_page: int = 50):
    stmt = select(Log)
    if level:
        stmt = stmt.where(Log.level == level.upper())
    if module:
        stmt = stmt.where(Log.module == module)
    if q:
        stmt = stmt.where(Log.message.ilike(f"%{q}%"))
    if since_minutes:
        stmt = stmt.where(Log.ts >= datetime.now(timezone.utc)
                          - timedelta(minutes=since_minutes))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(Log.id.desc())
                             .offset((page - 1) * per_page).limit(min(per_page, 200)))).scalars().all()
    modules = sorted(r[0] for r in (await db.execute(select(Log.module).distinct())).all())
    return {"total": total or 0, "page": page, "per_page": per_page, "modules": modules,
            "items": [{"id": r.id, "ts": r.ts.isoformat() if r.ts else "", "level": r.level,
                       "module": r.module, "message": r.message} for r in rows]}


# ------------------------------------------------------------------ settings
DEFAULT_SETTINGS = {
    "playlist_url_format": "{base}/play/{type}/{id}.ts?u={u}&p={p}",
    # Seed from env so a first boot honours docker-compose; later GUI edits win.
    "fallback_strategy": FALLBACK_STRATEGY,     # macs_first | portal_first
    "epg_refresh_hours": 24,
    "logo_country": "netherlands",
    "tmdb_api_key": "",
    "fetch_page_budget": FETCH_PAGE_BUDGET,
    "output_base_url": OUTPUT_BASE_URL,
}


@router.get("/settings")
async def get_settings(db=Depends(get_db)):
    stored = {r.key: r.value for r in (await db.execute(select(Setting))).scalars().all()}
    out = dict(DEFAULT_SETTINGS)
    for k, raw in stored.items():
        try:
            out[k] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            out[k] = raw
    return {"settings": out}


@router.post("/settings")
async def set_settings(payload: dict, db=Depends(get_db)):
    for k, v in payload.items():
        row = await db.get(Setting, k)
        if row is None:
            row = Setting(key=k)
            db.add(row)
        row.value = json.dumps(v)
    await db.commit()
    await db_log("INFO", "settings", f"settings updated: {sorted(payload.keys())}")
    return {"ok": True}


# ------------------------------------------------------------------ epg sources
@router.get("/epg-sources")
async def epg_sources(db=Depends(get_db)):
    rows = (await db.execute(select(EpgSource).order_by(EpgSource.id))).scalars().all()
    return {"items": [{"id": r.id, "url": r.url, "enabled": r.enabled,
                       "last_fetch": r.last_fetch.isoformat() if r.last_fetch else None,
                       "status": r.status, "channel_count": r.channel_count} for r in rows]}


@router.post("/epg-sources")
async def add_epg(payload: dict, db=Depends(get_db)):
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    db.add(EpgSource(url=url, enabled=True))
    await db.commit()
    return {"ok": True}


@router.post("/epg-sources/toggle")
async def toggle_epg(payload: dict, db=Depends(get_db)):
    rows = (await db.execute(select(EpgSource).where(
        EpgSource.id.in_(payload.get("ids", []))))).scalars().all()
    for r in rows:
        r.enabled = bool(payload.get("enabled"))
    await db.commit()
    return {"ok": True}


@router.delete("/epg-sources/{eid}")
async def del_epg(eid: int, db=Depends(get_db)):
    r = await db.get(EpgSource, eid)
    if r:
        await db.delete(r)
        await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ export / import
@router.get("/export")
async def export_config(section: str = "all", db=Depends(get_db)):
    """Versioned JSON backup. Sections: portals, ffmpeg, users, settings, epg, all."""
    data: dict = {"app": "stalker-proxy-manager", "version": 1, "section": section}

    async def dump(model, fields):
        rows = (await db.execute(select(model))).scalars().all()
        return [{f: _jsonable(getattr(r, f)) for f in fields} for r in rows]

    if section in ("all", "portals"):
        from ..models import MacAddress
        data["portals"] = await dump(Portal, ["name", "base_url", "enabled", "proxy_url",
                                              "tls_insecure", "identity_mode", "stb_timezone"])
        macs = (await db.execute(select(MacAddress, Portal)
                                 .join(Portal, Portal.id == MacAddress.portal_id))).all()
        # sn / device_id travel with the MAC on purpose: they are the serial this
        # portal enrolled, so a backup that drops them would hand the panel a new
        # device the next time it is restored.
        data["macs"] = [{"portal": p.name, "mac": m.mac, "order": m.order,
                         "sn": m.sn, "device_id": m.device_id}
                        for m, p in macs]
        data["live_genres"] = await dump(LiveGenre, ["portal_id", "genre_portal_id", "name", "enabled"])
        data["vod_genres"] = await dump(VodGenre, ["portal_id", "genre_portal_id", "name", "enabled"])
        data["serie_genres"] = await dump(SerieGenre, ["portal_id", "genre_portal_id", "name", "enabled"])
    if section in ("all", "ffmpeg"):
        data["ffmpeg_templates"] = await dump(
            FFmpegTemplate, [c for c in FFmpegTemplate.__table__.columns.keys() if c != "id"])
    if section in ("all", "users"):
        data["users"] = await dump(User, ["name", "password", "m3u_enabled", "xtream_enabled",
                                          "expire_date", "max_connections", "enabled", "groups_json"])
    if section in ("all", "settings"):
        data["settings"] = {r.key: json.loads(r.value or "null")
                            for r in (await db.execute(select(Setting))).scalars().all()}
    if section in ("all", "epg"):
        data["epg_sources"] = await dump(EpgSource, ["url", "enabled"])
    if section in ("all", "live_playlist"):
        data["live_playlist"] = await dump(
            LivePlaylist, ["custom_name", "group_name", "number", "epg_id", "logo",
                           "ffmpeg_template_id", "enabled", "order"])
    return JSONResponse(data, headers={"Content-Disposition":
                                       f'attachment; filename="spm-{section}.json"'})


def _jsonable(v):
    return v.isoformat() if isinstance(v, datetime) else v


@router.post("/import")
async def import_config(payload: dict, db=Depends(get_db)):
    """
    Import a previously exported JSON (merge-by-name semantics).
    SoD: duplicates are skipped; nothing is deleted by an import.
    """
    mode = payload.get("mode", "merge")
    data = payload.get("data", {})
    applied = {"skipped": [], "imported": 0}

    for t in data.get("ffmpeg_templates", []):
        name = t.get("name")
        exists = (await db.execute(select(FFmpegTemplate).where(FFmpegTemplate.name == name))
                  ).scalar_one_or_none()
        if exists and mode == "merge":
            applied["skipped"].append(f"ffmpeg:{name}")
            continue
        row = exists or FFmpegTemplate(name=name)
        for k, v in t.items():
            if hasattr(row, k) and k != "id":
                setattr(row, k, v)
        if not exists:
            db.add(row)
        applied["imported"] += 1

    for u in data.get("users", []):
        exists = (await db.execute(select(User).where(User.name == u.get("name")))).scalar_one_or_none()
        if exists:
            applied["skipped"].append(f"user:{u.get('name')}")
            continue
        db.add(User(**{k: v for k, v in u.items() if k != "id"}))
        applied["imported"] += 1

    for p in data.get("portals", []):
        exists = (await db.execute(select(Portal).where(Portal.name == p.get("name")))).scalar_one_or_none()
        if exists:
            applied["skipped"].append(f"portal:{p.get('name')}")
            continue
        db.add(Portal(**p))
        applied["imported"] += 1

    for e in data.get("epg_sources", []):
        url = (e.get("url") or "").strip()
        if not url:
            continue
        exists = await db.scalar(select(EpgSource).where(EpgSource.url == url))
        if exists:
            applied["skipped"].append(f"epg:{url}")
            continue
        db.add(EpgSource(url=url, enabled=bool(e.get("enabled", True))))
        applied["imported"] += 1

    for k, v in (data.get("settings") or {}).items():
        row = await db.get(Setting, k)
        if row is None:
            row = Setting(key=k)
            db.add(row)
        row.value = json.dumps(v)

    await db.commit()
    await db_log("INFO", "import", f"config import: {applied['imported']} imported, "
                                   f"{len(applied['skipped'])} skipped ({mode})")
    return applied
