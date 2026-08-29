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
    FFmpegTemplate, LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource,
    LocalFile, LocalPlaylist, LocalSource, Portal, SerieEpisode, SerieGenre,
    SeriePlaylist,
    SeriePlaylistSeason, SeriePlaylistSource, SerieSeason, SerieSource,
    VodGenre, VodPlaylist, VodPlaylistSource, VodSource,
)
from ..security import require_admin
from ..services import item_info
from ..services.db_logging import db_log
from ..services.playlist_sync import ADD_KINDS, add_sources
from ..services.titles import best_title

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


# ---------------------------------------------------------------- tv-logos (Phase 3)
@router.post("/live/auto-logos")
async def auto_logos_endpoint():
    """Fuzzy-match every live playlist channel against the tv-logo/tv-logos
    repo index and write matched logo URLs (admin-triggered, single pass)."""
    from ..services import logos as logos_svc
    try:
        return await logos_svc.auto_logos()
    except Exception as exc:  # noqa: BLE001 - report to GUI instead of 500
        return {"ok": False, "error": str(exc)}


@router.post("/logos/refresh-index")
async def refresh_logo_index():
    from ..services import logos as logos_svc
    try:
        return await logos_svc.refresh_index()
    except Exception as exc:  # noqa: BLE001 - report to GUI instead of 500
        return {"ok": False, "error": str(exc)}


@router.get("/logos/suggest")
async def logo_suggest(name: str = ""):
    """Single-logo suggestion for the custom-channel editor."""
    from ..services import logos as logos_svc
    url = await logos_svc.suggest_logo(name)
    return {"name": name, "logo": url}


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
        # explicit select: lazy r.sources would trigger sync IO -> 500 (async session)
        existing = {x.live_source_id: x for x in (await db.execute(
            select(LivePlaylistSource).where(
                LivePlaylistSource.live_playlist_id == pid))).scalars().all()}
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
    if payload.get("delete"):                     # "remove from playlist" in bulk
        for r in rows:
            await db.delete(r)
        await db.commit()
        return {"ok": True, "count": len(rows), "deleted": len(rows)}
    for r in rows:
        if "group_name" in payload:
            r.group_name = payload["group_name"]
        if "ffmpeg_template_id" in payload:
            r.ffmpeg_template_id = payload["ffmpeg_template_id"] or None
        if "enabled" in payload:
            r.enabled = bool(payload["enabled"])
    await db.commit()
    return {"ok": True, "count": len(rows)}


async def _bulk_assign(db, model, payload) -> int:
    """Same semantics as /api/playlist/live/bulk for vod/series/local rows."""
    ids = [int(x) for x in payload.get("ids", [])]
    rows = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
    if payload.get("delete"):                     # "remove from playlist" in bulk
        for r in rows:
            await db.delete(r)
        await db.commit()
        return len(rows)
    for r in rows:
        if "group_name" in payload:
            r.group_name = payload["group_name"]
        if "ffmpeg_template_id" in payload:
            r.ffmpeg_template_id = payload["ffmpeg_template_id"] or None
        if "enabled" in payload:
            r.enabled = bool(payload["enabled"])
    await db.commit()
    return len(rows)


@router.post("/vod/bulk")
async def vod_bulk(payload: dict, db=Depends(get_db)):
    n = await _bulk_assign(db, VodPlaylist, payload)
    return {"ok": True, "count": n, **({"deleted": n} if payload.get("delete") else {})}


@router.post("/series/bulk")
async def series_bulk(payload: dict, db=Depends(get_db)):
    n = await _bulk_assign(db, SeriePlaylist, payload)
    return {"ok": True, "count": n, **({"deleted": n} if payload.get("delete") else {})}


@router.post("/local/bulk")
async def local_bulk(payload: dict, db=Depends(get_db)):
    n = await _bulk_assign(db, LocalPlaylist, payload)
    return {"ok": True, "count": n, **({"deleted": n} if payload.get("delete") else {})}


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
        r = VodPlaylist(vod_source_id=sid, custom_name=best_title(src.original_name),
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


@router.get("/candidates")
async def candidates(kind: str = "vod", q: str = "", group: str = "",
                     only_missing: bool = True, enabled_only: bool = True,
                     page: int = 1, per_page: int = 25, db=Depends(get_db)):
    """
    Sources that can be pushed into the playlist, with an `in_playlist` flag.

    Backs the Playlist Builder's "Add from sources" dialog: filter by name and
    group, hide what is already in the playlist, select the whole (filtered)
    set and add it with ONE /add-sources call - group access instead of 1 by 1.
    """
    per_page = min(max(per_page, 5), 500)
    if kind == "live":
        src, genre, fk = LiveSource, LiveGenre, LiveSource.live_genre_id
        name_col = LiveSource.original_name
        used_model, used_col = LivePlaylistSource, LivePlaylistSource.live_source_id
        used_extra = (LivePlaylistSource.priority == 1,)
    elif kind == "vod":
        src, genre, fk = VodSource, VodGenre, VodSource.vod_genre_id
        name_col = VodSource.original_name
        used_model, used_col = VodPlaylist, VodPlaylist.vod_source_id
        used_extra = ()
    elif kind == "series":
        src, genre, fk = SerieSource, SerieGenre, SerieSource.serie_genre_id
        name_col = SerieSource.original_name
        used_model, used_col = SeriePlaylist, SeriePlaylist.serie_source_id
        used_extra = ()
    elif kind == "local":
        src, genre, fk = LocalFile, LocalSource, LocalFile.local_source_id
        name_col = LocalFile.filename
        used_model, used_col = LocalPlaylist, LocalPlaylist.local_file_id
        used_extra = ()
    else:
        raise HTTPException(400, f"kind must be one of {', '.join(ADD_KINDS)}")

    # label column = genre name (portal kinds) or the scanned directory (local)
    label_col = LocalSource.directory if kind == "local" else genre.name
    stmt = select(src, label_col).outerjoin(genre, genre.id == fk)
    if q:
        stmt = stmt.where(name_col.ilike(f"%{q}%"))
    if group:
        stmt = stmt.where(label_col == group)
    if enabled_only and hasattr(src, "enabled"):
        stmt = stmt.where(src.enabled.is_(True))
    if only_missing:
        stmt = stmt.where(~src.id.in_(select(used_col).where(*used_extra) if used_extra
                                      else select(used_col)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(name_col).offset(
        (page - 1) * per_page).limit(per_page))).all()

    items = [{"id": r.id,
              "name": (r.original_name if hasattr(r, "original_name") else r.filename),
              "group": gname or getattr(r, "genre", None) or getattr(r, "directory", None) or "",
              "poster": getattr(r, "poster", None), "year": getattr(r, "year", None),
              "enabled": getattr(r, "enabled", True), "in_playlist": False}
             for r, gname in rows]
    if not only_missing and items:                 # flag what is already there
        have = {x[0] for x in (await db.execute(
            select(used_col).where(used_col.in_([i["id"] for i in items]), *used_extra))).all()}
        for i in items:
            i["in_playlist"] = i["id"] in have
    groups = [g[0] for g in (await db.execute(
        select(label_col).distinct().order_by(label_col))).all() if g[0]]
    return {"total": total or 0, "page": page, "per_page": per_page,
            "items": items, "groups": groups}


@router.post("/add-sources")
async def add_sources_bulk(payload: dict, db=Depends(get_db)):
    """
    Add MANY sources to the playlist in ONE transaction.

    "Group access instead of one by one": the Playlist Builder dialog selects a
    whole genre/group (or an arbitrary selection) and pushes it in a single
    request - one transaction, one commit, instead of one round trip per item.
    Existing rows are never touched and are reported as `existed`.
    """
    kind = payload.get("kind")
    ids = [int(x) for x in payload.get("ids", []) if str(x).strip()]
    if kind not in ADD_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(ADD_KINDS)}")
    if not ids:
        raise HTTPException(400, "no source ids given")
    group = (payload.get("group_name") or "").strip() or None
    try:
        res = await add_sources(db, kind, ids, group)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - report to the GUI, keep the session clean
        await db.rollback()
        raise HTTPException(400, f"bulk add failed: {exc}") from exc
    await db_log("INFO", "playlist",
                 f"added {res['added']} {kind} item(s) to the playlist"
                 + (f" ({res['existed']} already there)" if res.get("existed") else ""))
    return {"ok": True, **res}


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
    dirty = False
    for r in rows:
        chain = await _map_chain(db, VodPlaylist, VodPlaylistSource, VodSource,
                                 VodPlaylistSource, VodPlaylistSource.vod_playlist_id, r.id)
        src_title = chain[0]["name"] if chain else None
        title = best_title(r.custom_name, src_title)
        if title != (r.custom_name or "") and title != "?":
            r.custom_name = title
            dirty = True
        items.append({"id": r.id, "custom_name": title, "group_name": r.group_name,
                      "poster": r.poster, "year": r.year, "rating": r.rating,
                      "overview": r.overview, "enabled": r.enabled,
                      "order": r.order, "ffmpeg_template_id": r.ffmpeg_template_id,
                      "template": tpls.get(r.ffmpeg_template_id or 0, ""), "chain": chain})
    if dirty:
        await db.commit()
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


@router.post("/series/sync-seasons")
async def sync_series_seasons(db=Depends(get_db)):
    """
    Re-link seasons for every playlist-series. Runs automatically after each
    portal fetch and at boot; this is the manual "I just fetched, show me the
    seasons" button.
    """
    from ..services.playlist_sync import sync_season_links
    added = await sync_season_links(db)
    return {"ok": True, "added": added}


@router.get("/series")
async def series_pl(db=Depends(get_db), q: str = "", group: str = "", page: int = 1,
                    per_page: int = 25, sort: str = "order", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    total, rows, groups, tpls = await _pl_list(db, SeriePlaylist, SerieSource, "serie_source_id",
                                               q, group, {}, page, per_page, sort, direction)
    row_ids = [r.id for r in rows]

    # Read-repair for the whole page at once: seasons fetched AFTER the item was
    # added to the playlist still need a link row. This loop used to run one
    # SELECT per season per row plus one COUNT per enabled season, which came to
    # 8.2 queries per item (204 for a 25-row page).
    from ..services.playlist_sync import sync_season_links
    if row_ids:
        await sync_season_links(db, row_ids)

    # ...then two queries for the page, whatever its size.
    links: dict[int, list] = {}
    if row_ids:
        for pid, link_id, season_id, s_num, s_name, enabled in (await db.execute(
                select(SeriePlaylistSeason.serie_playlist_id, SeriePlaylistSeason.id,
                       SerieSeason.id, SerieSeason.season_number, SerieSeason.name,
                       SeriePlaylistSeason.enabled)
                .join(SerieSeason, SerieSeason.id == SeriePlaylistSeason.serie_season_id)
                .where(SeriePlaylistSeason.serie_playlist_id.in_(row_ids))
                .order_by(SeriePlaylistSeason.serie_playlist_id,
                          SerieSeason.season_number))).all():
            links.setdefault(pid, []).append((link_id, season_id, s_num, s_name, enabled))

    counts: dict[int, int] = {}
    enabled_ids = [v[1] for group in links.values() for v in group if v[4]]
    if enabled_ids:
        for season_id, n in (await db.execute(
                select(SerieEpisode.serie_season_id, func.count())
                .where(SerieEpisode.serie_season_id.in_(enabled_ids))
                .group_by(SerieEpisode.serie_season_id))).all():
            counts[season_id] = n

    items = []
    for r in rows:
        seasons = links.get(r.id, [])
        n_eps = sum(counts.get(v[1], 0) for v in seasons if v[4])
        items.append({"id": r.id, "custom_name": r.custom_name, "group_name": r.group_name,
                      "poster": r.poster, "year": r.year, "rating": r.rating,
                      "overview": r.overview, "enabled": r.enabled,
                      "order": r.order, "ffmpeg_template_id": r.ffmpeg_template_id,
                      "template": tpls.get(r.ffmpeg_template_id or 0, ""),
                        "seasons": [{"link_id": v[0], "season_id": v[1],
                                     "season_number": v[2], "name": v[3],
                                     "enabled": v[4]} for v in seasons],
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
                      "file": (f"{ls.directory}/{lf.relative_path}" if ls and lf else "?"),
                      "size_bytes": lf.size_bytes if lf else 0})
    groups = [g[0] for g in (await db.execute(
        select(LocalPlaylist.group_name).distinct().order_by(LocalPlaylist.group_name))).all() if g[0]]
    return {"total": total or 0, "page": page, "per_page": per_page, "items": items, "groups": groups}


# ------------------------------------------------- detail popup enrichment
async def _source_cmd_and_portal(db, kind: str, pid: int):
    """Return (cmd, portal_id, is_url) for the item's PRIMARY source."""
    if kind == "live":
        link = (await db.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == pid)
            .order_by(LivePlaylistSource.priority))).scalars().first()
        src = await db.get(LiveSource, link.live_source_id) if link else None
        return (src.cmd if src else None), (src.portal_id if src else None), True
    if kind == "vod":
        link = (await db.execute(select(VodPlaylistSource).where(
            VodPlaylistSource.vod_playlist_id == pid)
            .order_by(VodPlaylistSource.priority))).scalars().first()
        src = await db.get(VodSource, link.vod_source_id) if link else None
        return (src.cmd if src else None), (src.portal_id if src else None), True
    if kind == "series":
        # first enabled season's first episode of the playlist's source
        pl = await db.get(SeriePlaylist, pid)
        eps = []
        if pl:
            season_links = (await db.execute(
                select(SeriePlaylistSeason).where(
                    SeriePlaylistSeason.serie_playlist_id == pid,
                    SeriePlaylistSeason.enabled.is_(True)))).scalars().all()
            for sl in season_links:
                eps = (await db.execute(select(SerieEpisode).where(
                    SerieEpisode.serie_season_id == sl.serie_season_id)
                    .order_by(SerieEpisode.episode_number).limit(1))).scalars().all()
                if eps:
                    break
        if not eps or not eps[0].cmd:
            return None, None, True
        season = await db.get(SerieSeason, eps[0].serie_season_id)
        ssrc = await db.get(SerieSource, season.serie_source_id) if season else None
        return eps[0].cmd, (ssrc.portal_id if ssrc else None), True
    if kind == "local":
        r = await db.get(LocalPlaylist, pid)
        lf = await db.get(LocalFile, r.local_file_id) if r else None
        ls = await db.get(LocalSource, lf.local_source_id) if lf else None
        path = item_info.local_file_path(ls.directory, lf.relative_path) if ls and lf else None
        return path, None, False
    return None, None, True


@router.get("/info")
async def playlist_item_info(kind: str = "", id: int = 0, db=Depends(get_db)):  # noqa: A002
    """Probe + TMDB info for a playlist item (detail popup enrichment)."""
    if kind not in ("live", "vod", "series", "local"):
        raise HTTPException(400, "kind must be live|vod|series|local")
    models = {"live": LivePlaylist, "vod": VodPlaylist, "series": SeriePlaylist,
              "local": LocalPlaylist}
    row = await db.get(models[kind], id)
    if not row:
        raise HTTPException(404, "item not found")
    cmd, portal_id, is_url = await _source_cmd_and_portal(db, kind, id)
    probe = {"error": "no usable source stream"}
    if cmd and is_url:
        url = await item_info.playable_url(db, cmd, portal_id, kind)
        if url:
            probe = await item_info.probe_target(url, is_url=True)
    elif cmd:
        probe = await item_info.probe_target(cmd, is_url=False)
    tmdb = await item_info.enrich(row.custom_name, getattr(row, "year", None),
                                  "vod" if kind == "vod" else ("series" if kind == "series" else kind))
    return {"probe": probe, "tmdb": tmdb, "source_cmd": cmd}


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
