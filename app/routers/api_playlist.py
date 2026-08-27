"""
Playlist Builder API (GUI tab 3).

Live channels are fully custom rows with an ORDERED fallback chain
(live_playlist_sources); vod/serie/local rows reference one source plus an
optional fallback chain (Q1=C). Fuzzy matching (spec: name-as-you-type, not
necessarily a perfect match) always runs against ENABLED SOURCE items - never
against the playlist itself (Phase-1 fix G1).
"""

from __future__ import annotations

import difflib
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from ..database import get_db
from ..models import (
    FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource, LocalFile,
    LocalPlaylist, LocalSource, Portal, SerieEpisode, SerieGenre, SeriePlaylist,
    SeriePlaylistSeason, SeriePlaylistSource, SerieSeason, SerieSource,
    VodGenre, VodPlaylist, VodPlaylistSource, VodSource,
)
from ..security import require_admin
from ..services.db_logging import db_log

router = APIRouter(prefix="/api/playlist", tags=["playlist"], dependencies=[Depends(require_admin)])


def norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum() or c.isspace()).strip()


def fuzzy(query: str, candidates: list[tuple[int, str]], limit: int = 50) -> list[tuple[int, float]]:
    """difflib-based fuzzy match, case-insensitive; returns sorted (id, score)."""
    q = norm(query)
    if not q:
        return [(cid, 0.0) for cid, _ in candidates[:limit]]
    scored = []
    for cid, name in candidates:
        n = norm(name)
        if q in n:
            scored.append((cid, 1.0 + len(q) / max(len(n), 1)))
        else:
            r = difflib.SequenceMatcher(None, q, n).ratio()
            if r >= 0.45:                     # "close enough" per spec
                scored.append((cid, r))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


# ----------------------------------------------------------------- suggest
@router.get("/suggest")
async def suggest(q: str = "", kind: str = "live", db=Depends(get_db)):
    """Fuzzy candidate list for the Add-channel popup and the fallback editor.
    Empty query -> all enabled sources (spec: show all when filter is empty)."""
    model = {"live": LiveSource, "vod": VodSource, "series": SerieSource}.get(kind)
    if model is None:
        raise HTTPException(400, "kind must be live|vod|series")
    rows = (await db.execute(select(model, Portal).join(Portal, Portal.id == model.portal_id)
                             .where(model.enabled.is_(True)))).all()
    cand = [(r[0].id, r[0].original_name) for r in rows]
    by_id = {r[0].id: r for r in rows}
    matches = [cid for cid, _ in fuzzy(q, cand, 60)] if q else [c[0] for c in cand][:60]
    out = []
    for cid in matches:
        src, portal = by_id[cid]
        out.append({"id": src.id, "name": src.original_name, "portal": portal.name,
                    "portal_id": portal.id,
                    "logo": getattr(src, "logo_original", None) or getattr(src, "poster", None),
                    "cmd": src.cmd})
    return {"items": out, "query": q, "kind": kind}


# ----------------------------------------------------------------- live CRUD
async def _chain(db, playlist_id: int, link_model, src_model, fk_name: str):
    rows = (await db.execute(select(link_model).where(link_model.live_playlist_id == playlist_id)
                             .order_by(link_model.priority))).scalars().all()
    out = []
    for r in rows:
        src = await db.get(src_model, getattr(r, fk_name))
        if not src:
            continue
        portal = await db.get(Portal, src.portal_id)
        out.append({"link_id": r.id, "source_id": src.id, "priority": r.priority,
                    "name": src.original_name, "portal": portal.name if portal else "?"})
    return out


@router.get("/live")
async def live_list(db=Depends(get_db), q: str = "", group: str = "", portal_id: int = 0,
                    page: int = 1, per_page: int = 25, sort: str = "order", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    stmt = select(LivePlaylist)
    if q:
        stmt = stmt.where(LivePlaylist.custom_name.ilike(f"%{q}%"))
    if group:
        stmt = stmt.where(LivePlaylist.group_name == group)
    # portal filter: keep items that have a chain row whose source is on that portal
    if portal_id:
        sub = (select(LivePlaylistSource.live_playlist_id)
               .join(LiveSource, LiveSource.id == LivePlaylistSource.live_source_id)
               .where(LiveSource.portal_id == portal_id))
        stmt = stmt.where(LivePlaylist.id.in_(sub))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    col = {"order": LivePlaylist.order, "name": LivePlaylist.custom_name,
           "group": LivePlaylist.group_name, "id": LivePlaylist.id}.get(sort, LivePlaylist.order)
    stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    groups = [g[0] for g in (await db.execute(
        select(LivePlaylist.group_name).distinct().order_by(LivePlaylist.group_name))).all() if g[0]]
    tpls = {t.id: t.name for t in (await db.execute(select(FFmpegTemplate))).scalars().all()}
    items = []
    for r in rows:
        chain = await _chain(db, r.id, LivePlaylistSource, LiveSource, "live_source_id")
        items.append({"id": r.id, "custom_name": r.custom_name, "group_name": r.group_name,
                      "epg_id": r.epg_id, "logo": r.logo, "number": r.number,
                      "ffmpeg_template_id": r.ffmpeg_template_id,
                      "template": tpls.get(r.ffmpeg_template_id or 0, ""),
                      "enabled": r.enabled, "order": r.order, "chain": chain})
    return {"total": total or 0, "page": page, "per_page": per_page,
            "items": items, "groups": groups}


def _apply_live_payload(r: LivePlaylist, payload: dict):
    for f in ("custom_name", "group_name", "epg_id", "logo", "number",
              "ffmpeg_template_id", "enabled"):
        if f in payload:
            setattr(r, f, payload[f])


@router.post("/live")
async def live_create(payload: dict, db=Depends(get_db)):
    name = (payload.get("custom_name") or "").strip()
    if not name:
        raise HTTPException(400, "custom_name required")
    max_order = await db.scalar(select(func.max(LivePlaylist.order))) or 0
    r = LivePlaylist(custom_name=name, order=max_order + 1)
    _apply_live_payload(r, payload)
    db.add(r)
    await db.flush()
    for i, sid in enumerate(payload.get("source_ids", []), 1):
        db.add(LivePlaylistSource(live_playlist_id=r.id, live_source_id=int(sid), priority=i))
    await db.commit()
    await db_log("INFO", "playlist", f"custom channel '{name}' created "
                                     f"({len(payload.get('source_ids', []))} fallback sources)")
    return {"id": r.id}


@router.put("/live/{pid}")
async def live_update(pid: int, payload: dict, db=Depends(get_db)):
    r = await db.get(LivePlaylist, pid)
    if not r:
        raise HTTPException(404, "not found")
    _apply_live_payload(r, payload)
    if "source_ids" in payload:
        existing = {x.live_source_id: x for x in r.sources}
        wanted = [int(x) for x in payload["source_ids"]]
        for sid, link in existing.items():
            if sid not in wanted:
                await db.delete(link)
        for i, sid in enumerate(wanted, 1):
            if sid in existing:
                existing[sid].priority = i
            else:
                db.add(LivePlaylistSource(live_playlist_id=r.id, live_source_id=sid, priority=i))
    await db.commit()
    return {"ok": True}


@router.delete("/live/{pid}")
async def live_delete(pid: int, db=Depends(get_db)):
    r = await db.get(LivePlaylist, pid)
    if not r:
        raise HTTPException(404, "not found")
    await db.delete(r)
    await db.commit()
    return {"ok": True}


@router.post("/live/order")
async def live_order(payload: dict, db=Depends(get_db)):
    """Drag&drop result: [{id, order}, ...] (only valid when sorted by order - G6)."""
    for row in payload.get("items", []):
        r = await db.get(LivePlaylist, int(row["id"]))
        if r:
            r.order = int(row["order"])
    await db.commit()
    return {"ok": True}


@router.post("/live/bulk")
async def live_bulk(payload: dict, db=Depends(get_db)):
    """Assign group and/or ffmpeg template to MANY channels in one go (spec)."""
    ids = [int(x) for x in payload.get("ids", [])]
    rows = (await db.execute(select(LivePlaylist).where(LivePlaylist.id.in_(ids)))).scalars().all()
    for r in rows:
        if "group_name" in payload:
            r.group_name = payload["group_name"]
        if "ffmpeg_template_id" in payload:
            r.ffmpeg_template_id = payload["ffmpeg_template_id"] or None
        if "enabled" in payload:
            r.enabled = bool(payload["enabled"])
    await db.commit()
    return {"ok": True, "count": len(rows)}


# ----------------------------------------------------------------- vod / series / local
async def _map_chain(db, model_pl, link_model, src_model, link_fk, src_fk, pl_id):
    rows = (await db.execute(select(link_model).where(src_fk == pl_id)
                             .order_by(link_model.priority))).scalars().all()
    out = []
    for r in rows:
        src = await db.get(src_model, r.vod_source_id if hasattr(r, "vod_source_id") else r.serie_source_id)
        portal = await db.get(Portal, src.portal_id) if src else None
        out.append({"link_id": r.id, "source_id": src.id if src else 0, "priority": r.priority,
                    "name": src.original_name if src else "?", "portal": portal.name if portal else "?"})
    return out


@router.post("/add-from-source")
async def add_from_source(payload: dict, db=Depends(get_db)):
    """Add a source item to the playlist (kind: vod|series|localfile)."""
    kind = payload.get("kind")
    sid = int(payload.get("source_id", 0))
    if kind == "vod":
        src = await db.get(VodSource, sid)
        if not src:
            raise HTTPException(404, "source not found")
        if (await db.execute(select(VodPlaylist).where(VodPlaylist.vod_source_id == sid))).scalar_one_or_none():
            return {"ok": True, "exists": True}
        genre_row = await db.get(VodGenre, src.vod_genre_id) if src.vod_genre_id else None
        r = VodPlaylist(vod_source_id=sid, custom_name=src.original_name,
                        group_name=genre_row.name if genre_row else "VOD",
                        poster=src.poster, year=src.year, rating=src.rating,
                        overview=src.description,
                        order=(await db.scalar(select(func.max(VodPlaylist.order))) or 0) + 1)
        db.add(r)
        await db.flush()
        db.add(VodPlaylistSource(vod_playlist_id=r.id, vod_source_id=sid, priority=1))
        await db.commit()
        return {"ok": True, "id": r.id}
    if kind == "series":
        src = await db.get(SerieSource, sid)
        if not src:
            raise HTTPException(404, "source not found")
        if (await db.execute(select(SeriePlaylist).where(SeriePlaylist.serie_source_id == sid))).scalar_one_or_none():
            return {"ok": True, "exists": True}
        genre_row = await db.get(SerieGenre, src.serie_genre_id) if src.serie_genre_id else None
        r = SeriePlaylist(serie_source_id=sid, custom_name=src.original_name,
                          group_name=genre_row.name if genre_row else "Series",
                          poster=src.poster, year=src.year, rating=src.rating,
                          overview=src.description,
                          order=(await db.scalar(select(func.max(SeriePlaylist.order))) or 0) + 1)
        db.add(r)
        await db.flush()
        db.add(SeriePlaylistSource(serie_playlist_id=r.id, serie_source_id=sid, priority=1))
        seasons = (await db.execute(select(SerieSeason).where(
            SerieSeason.serie_source_id == sid))).scalars().all()
        for sn in seasons:
            db.add(SeriePlaylistSeason(serie_playlist_id=r.id, serie_season_id=sn.id,
                                       enabled=sn.enabled))
        await db.commit()
        return {"ok": True, "id": r.id}
    if kind == "localfile":
        lf = await db.get(LocalFile, sid)
        if not lf:
            raise HTTPException(404, "file not found")
        if (await db.execute(select(LocalPlaylist).where(LocalPlaylist.local_file_id == sid))).scalar_one_or_none():
            return {"ok": True, "exists": True}
        r = LocalPlaylist(local_file_id=sid, custom_name=lf.filename,
                          order=(await db.scalar(select(func.max(LocalPlaylist.order))) or 0) + 1)
        db.add(r)
        await db.commit()
        return {"ok": True, "id": r.id}
    raise HTTPException(400, "kind must be vod|series|localfile")


async def _pl_list(db, model, src_model, src_fk_name, q, group, filters, page, per_page,
                   sort, direction):
    stmt = select(model)
    if q:
        stmt = stmt.where(model.custom_name.ilike(f"%{q}%"))
    if group:
        stmt = stmt.where(model.group_name == group)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    col = {"order": model.order, "name": model.custom_name,
           "group": model.group_name}.get(sort, model.order)
    stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    tpls = {t.id: t.name for t in (await db.execute(select(FFmpegTemplate))).scalars().all()}
    groups = [g[0] for g in (await db.execute(
        select(model.group_name).distinct().order_by(model.group_name))).all() if g[0]]
    return total or 0, rows, groups, tpls


@router.get("/vod")
async def vod_pl(db=Depends(get_db), q: str = "", group: str = "", page: int = 1,
                 per_page: int = 25, sort: str = "order", direction: str = "asc",
                 selected: str = ""):
    per_page = min(max(per_page, 5), 500)
    total, rows, groups, tpls = await _pl_list(db, VodPlaylist, VodSource, "vod_source_id",
                                               q, group, {}, page, per_page, sort, direction)
    items = []
    for r in rows:
        chain = await _map_chain(db, VodPlaylist, VodPlaylistSource, VodSource,
                                 VodPlaylistSource, VodPlaylistSource.vod_playlist_id, r.id)
        items.append({"id": r.id, "custom_name": r.custom_name, "group_name": r.group_name,
                      "poster": r.poster, "year": r.year, "enabled": r.enabled,
                      "order": r.order, "ffmpeg_template_id": r.ffmpeg_template_id,
                      "template": tpls.get(r.ffmpeg_template_id or 0, ""), "chain": chain})
    return {"total": total, "page": page, "per_page": per_page, "items": items, "groups": groups}


@router.put("/vod/{pid}")
async def vod_update(pid: int, payload: dict, db=Depends(get_db)):
    r = await db.get(VodPlaylist, pid)
    if not r:
        raise HTTPException(404, "not found")
    for f in ("custom_name", "group_name", "ffmpeg_template_id", "enabled", "order"):
        if f in payload:
            setattr(r, f, payload[f])
    if payload.get("tmdb"):      # Phase-3 hook: TMDB merge
        for f in ("tmdb_id", "overview", "poster", "rating", "year"):
            if f in payload["tmdb"]:
                setattr(r, f, payload["tmdb"][f])
    await db.commit()
    return {"ok": True}


@router.delete("/vod/{pid}")
async def vod_delete(pid: int, db=Depends(get_db)):
    r = await db.get(VodPlaylist, pid)
    if r:
        await db.delete(r)
        await db.commit()
    return {"ok": True}


@router.get("/series")
async def series_pl(db=Depends(get_db), q: str = "", group: str = "", page: int = 1,
                    per_page: int = 25, sort: str = "order", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    total, rows, groups, tpls = await _pl_list(db, SeriePlaylist, SerieSource, "serie_source_id",
                                               q, group, {}, page, per_page, sort, direction)
    items = []
    for r in rows:
        seasons = (await db.execute(
            select(SeriePlaylistSeason, SerieSeason)
            .join(SerieSeason, SerieSeason.id == SeriePlaylistSeason.serie_season_id)
            .where(SeriePlaylistSeason.serie_playlist_id == r.id)
            .order_by(SerieSeason.season_number))).all()
        n_eps = 0
        for pls, sn in seasons:
            if pls.enabled:
                n_eps += await db.scalar(select(func.count()).select_from(SerieEpisode).where(
                    SerieEpisode.serie_season_id == sn.id)) or 0
        items.append({"id": r.id, "custom_name": r.custom_name, "group_name": r.group_name,
                      "poster": r.poster, "year": r.year, "enabled": r.enabled,
                      "order": r.order, "ffmpeg_template_id": r.ffmpeg_template_id,
                      "template": tpls.get(r.ffmpeg_template_id or 0, ""),
                      "seasons": [{"link_id": pls.id, "season_id": sn.id,
                                   "season_number": sn.season_number,
                                   "name": sn.name, "enabled": pls.enabled}
                                  for pls, sn in seasons],
                      "enabled_episode_count": n_eps})
    return {"total": total, "page": page, "per_page": per_page, "items": items, "groups": groups}


@router.put("/series/{pid}")
async def series_update(pid: int, payload: dict, db=Depends(get_db)):
    r = await db.get(SeriePlaylist, pid)
    if not r:
        raise HTTPException(404, "not found")
    for f in ("custom_name", "group_name", "ffmpeg_template_id", "enabled", "order"):
        if f in payload:
            setattr(r, f, payload[f])
    if "seasons" in payload:      # [{link_id, enabled}]
        for srow in payload["seasons"]:
            link = await db.get(SeriePlaylistSeason, int(srow["link_id"]))
            if link and link.serie_playlist_id == pid:
                link.enabled = bool(srow["enabled"])
    await db.commit()
    return {"ok": True}


@router.delete("/series/{pid}")
async def series_delete(pid: int, db=Depends(get_db)):
    r = await db.get(SeriePlaylist, pid)
    if r:
        await db.delete(r)
        await db.commit()
    return {"ok": True}


@router.get("/local")
async def local_pl(db=Depends(get_db), q: str = "", group: str = "", page: int = 1,
                   per_page: int = 25, sort: str = "order", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    stmt = select(LocalPlaylist)
    if q:
        stmt = stmt.where(LocalPlaylist.custom_name.ilike(f"%{q}%"))
    if group:
        stmt = stmt.where(LocalPlaylist.group_name == group)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    col = {"order": LocalPlaylist.order, "name": LocalPlaylist.custom_name}.get(sort, LocalPlaylist.order)
    stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    tpls = {t.id: t.name for t in (await db.execute(select(FFmpegTemplate))).scalars().all()}
    items = []
    for r in rows:
        lf = await db.get(LocalFile, r.local_file_id)
        ls = await db.get(LocalSource, lf.local_source_id) if lf else None
        items.append({"id": r.id, "custom_name": r.custom_name,
                      "group_name": r.group_name, "enabled": r.enabled, "order": r.order,
                      "ffmpeg_template_id": r.ffmpeg_template_id,
                      "template": tpls.get(r.ffmpeg_template_id or 0, ""),
                      "file": (f"{ls.directory}/{lf.relative_path}" if ls and lf else "?")})
    return {"total": total or 0, "page": page, "per_page": per_page, "items": items}


@router.put("/local/{pid}")
async def local_update(pid: int, payload: dict, db=Depends(get_db)):
    r = await db.get(LocalPlaylist, pid)
    if not r:
        raise HTTPException(404, "not found")
    for f in ("custom_name", "group_name", "ffmpeg_template_id", "enabled", "order"):
        if f in payload:
            setattr(r, f, payload[f])
    await db.commit()
    return {"ok": True}


@router.delete("/local/{pid}")
async def local_delete(pid: int, db=Depends(get_db)):
    r = await db.get(LocalPlaylist, pid)
    if r:
        await db.delete(r)
        await db.commit()
    return {"ok": True}
