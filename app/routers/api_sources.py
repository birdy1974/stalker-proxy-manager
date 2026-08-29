"""
Input Source API (GUI tab 2): server-side filtered/paginated/sorted listings of
the SOURCES stored in the DB (never live portal calls - workflow steps 8/11).

  live    /api/sources/live     filters: portal_id, genre_id, q, enabled
  vod     /api/sources/vod      same + detail endpoint for the info popup
  series  /api/sources/series   same + seasons endpoint
  local   /api/sources/local*   directory CRUD + scan + file listing
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select

from ..config import MEDIA_ROOT
from ..database import get_db, spawn
from ..services.permissions import describe_access, permission_hint
from ..services.playlist_sync import (SYNC_KINDS, add_sources,
                                        sync_sources)
from ..models import (
    LiveGenre, LiveSource, LocalFile, LocalPlaylist, LocalSource, Portal,
    SerieEpisode, SerieGenre, SerieSeason, SerieSource, VodGenre, VodSource,
)
from ..security import require_admin
from ..services import item_info
from ..services.db_logging import db_log
from ..services.local_files import fill_local_durations, missing_duration_ids

router = APIRouter(prefix="/api/sources", tags=["sources"], dependencies=[Depends(require_admin)])

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".mpg", ".mpeg", ".wmv",
              ".flv", ".mov", ".webm", ".m4v", ".vob", ".3gp", ".wav", ".mp3"}


def _sort(model, sort: str, direction: str):
    col = {"name": model.original_name, "id": model.id,
           "portal": model.portal_id}.get(sort, model.id)
    return col.desc() if direction == "desc" else col.asc()


async def _page(db, stmt, model, page: int, per_page: int, sort: str, direction: str):
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(_sort(model, sort, direction))
                             .offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return total, rows


def _live_item(r: LiveSource, genre_names, portal_names) -> dict:
    return {"id": r.id, "name": r.original_name, "number": r.number,
            "portal_id": r.portal_id, "portal": portal_names.get(r.portal_id, "?"),
            "genre_id": r.live_genre_id, "genre": genre_names.get(r.live_genre_id, ""),
            "logo": r.logo_original, "cmd": r.cmd, "enabled": r.enabled}


def _vod_item(r: VodSource, genre_names, portal_names) -> dict:
    return {"id": r.id, "name": r.original_name, "position": r.position,
            "portal_id": r.portal_id, "portal": portal_names.get(r.portal_id, "?"),
            "genre_id": r.vod_genre_id, "genre": genre_names.get(r.vod_genre_id, ""),
            "poster": r.poster, "year": r.year, "rating": r.rating, "enabled": r.enabled}


def _serie_item(r: SerieSource, genre_names, portal_names) -> dict:
    return {"id": r.id, "name": r.original_name,
            "portal_id": r.portal_id, "portal": portal_names.get(r.portal_id, "?"),
            "genre_id": r.serie_genre_id, "genre": genre_names.get(r.serie_genre_id, ""),
            "poster": r.poster, "year": r.year, "rating": r.rating,
            "seasons_fetched": r.seasons_fetched, "enabled": r.enabled}


async def _names(db):
    portals = {p.id: p.name for p in (await db.execute(select(Portal))).scalars().all()}
    lg = {g.id: g.name for g in (await db.execute(select(LiveGenre))).scalars().all()}
    vg = {g.id: g.name for g in (await db.execute(select(VodGenre))).scalars().all()}
    sg = {g.id: g.name for g in (await db.execute(select(SerieGenre))).scalars().all()}
    return portals, lg, vg, sg


def _filters(model, portal_id, genre_fk, genre_id, q, enabled):
    stmt = select(model)
    if portal_id:
        stmt = stmt.where(model.portal_id == portal_id)
    if genre_id:
        stmt = stmt.where(genre_fk == genre_id)
    if q:
        stmt = stmt.where(model.original_name.ilike(f"%{q}%"))   # case-insensitive per spec
    if enabled in ("true", "false"):
        stmt = stmt.where(model.enabled.is_(enabled == "true"))
    return stmt


@router.get("/live")
async def live(db=Depends(get_db), portal_id: int = 0, genre_id: int = 0, q: str = "",
               enabled: str = "", page: int = 1, per_page: int = 25,
               sort: str = "name", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    stmt = _filters(LiveSource, portal_id, LiveSource.live_genre_id, genre_id, q, enabled)
    total, rows = await _page(db, stmt, LiveSource, page, per_page, sort, direction)
    portals, lg, _, _ = await _names(db)
    return {"total": total, "page": page, "per_page": per_page,
            "items": [_live_item(r, lg, portals) for r in rows]}


@router.get("/vod")
async def vod(db=Depends(get_db), portal_id: int = 0, genre_id: int = 0, q: str = "",
              enabled: str = "", page: int = 1, per_page: int = 25,
              sort: str = "name", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    stmt = _filters(VodSource, portal_id, VodSource.vod_genre_id, genre_id, q, enabled)
    total, rows = await _page(db, stmt, VodSource, page, per_page, sort, direction)
    portals, _, vg, _ = await _names(db)
    return {"total": total, "page": page, "per_page": per_page,
            "items": [_vod_item(r, vg, portals) for r in rows]}


@router.get("/series")
async def series(db=Depends(get_db), portal_id: int = 0, genre_id: int = 0, q: str = "",
                 enabled: str = "", page: int = 1, per_page: int = 25,
                 sort: str = "name", direction: str = "asc"):
    per_page = min(max(per_page, 5), 500)
    stmt = _filters(SerieSource, portal_id, SerieSource.serie_genre_id, genre_id, q, enabled)
    total, rows = await _page(db, stmt, SerieSource, page, per_page, sort, direction)
    portals, _, _, sg = await _names(db)
    return {"total": total, "page": page, "per_page": per_page,
            "items": [_serie_item(r, sg, portals) for r in rows]}


@router.get("/vod/{vid}")
async def vod_detail(vid: int, db=Depends(get_db)):
    """Info for the detail popup (TMDB enrichment lands in Phase 3)."""
    r = await db.get(VodSource, vid)
    if not r:
        raise HTTPException(404, "vod not found")
    portals, _, vg, _ = await _names(db)
    d = _vod_item(r, vg, portals)
    d.update({"description": r.description, "director": r.director, "actors": r.actors,
              "duration": r.duration, "added": r.added, "cmd": r.cmd})
    return {"item": d}


@router.get("/series/{sid}")
async def series_detail(sid: int, db=Depends(get_db)):
    r = await db.get(SerieSource, sid)
    if not r:
        raise HTTPException(404, "serie not found")
    portals, _, _, sg = await _names(db)
    d = _serie_item(r, sg, portals)
    seasons = (await db.execute(select(SerieSeason).where(SerieSeason.serie_source_id == sid)
                                .order_by(SerieSeason.season_number))).scalars().all()
    out_seasons = []
    for sn in seasons:
        cnt = await db.scalar(select(func.count()).select_from(SerieEpisode)
                              .where(SerieEpisode.serie_season_id == sn.id))
        out_seasons.append({"id": sn.id, "season_number": sn.season_number, "name": sn.name,
                            "enabled": sn.enabled, "episode_count": cnt,
                            "episodes_fetched": sn.episodes_fetched})
    d.update({"description": r.description, "seasons": out_seasons})
    return {"item": d}


@router.get("/media-info")
async def sources_media_info(kind: str = "", id: int = 0, db=Depends(get_db)):  # noqa: A002
    """Lazy probe + TMDB for a SOURCE item (kept off the fast detail payload
    so the popup opens instantly; GUI fetches this right after)."""
    if kind == "vod":
        r = await db.get(VodSource, id)
        if not r:
            raise HTTPException(404, "vod not found")
        url = await item_info.playable_url(db, r.cmd or "", r.portal_id, "vod")
        probe = await item_info.probe_target(url, is_url=True) if url else \
            {"error": "no playable URL in cmd"}
        return {"probe": probe, "tmdb": await item_info.enrich(r.original_name, r.year, "vod")}
    if kind == "series":
        r = await db.get(SerieSource, id)
        if not r:
            raise HTTPException(404, "series not found")
        ep = (await db.execute(select(SerieEpisode)
                               .join(SerieSeason, SerieEpisode.serie_season_id == SerieSeason.id)
                               .where(SerieSeason.serie_source_id == id)
                               .order_by(SerieSeason.season_number,
                                         SerieEpisode.episode_number).limit(1))).scalars().first()
        url = await item_info.playable_url(db, ep.cmd or "", r.portal_id, "series") if ep else None
        probe = await item_info.probe_target(url, is_url=True) if url else \
            {"error": "seasons/episodes not fetched yet (enable the series, then Fetch)"
             if not ep else "no playable URL in cmd"}
        return {"probe": probe, "tmdb": await item_info.enrich(r.original_name, r.year, "series")}
    if kind == "live":
        r = await db.get(LiveSource, id)
        if not r:
            raise HTTPException(404, "channel not found")
        url = await item_info.playable_url(db, r.cmd or "", r.portal_id, "live")
        probe = await item_info.probe_target(url, is_url=True) if url else \
            {"error": "no playable URL in cmd"}
        return {"probe": probe, "tmdb": None}
    raise HTTPException(400, "kind must be live|vod|series")


# ------------------------------------------------------------------ toggles
@router.post("/toggle")
async def toggle(payload: dict, db=Depends(get_db)):
    """Bulk/single enable toggle: {kind, ids, enabled}"""
    model = {"live": LiveSource, "vod": VodSource, "series": SerieSource}.get(payload.get("kind"))
    if model is None:
        raise HTTPException(400, "kind must be live|vod|series")
    ids = payload.get("ids", [])
    enabled = bool(payload.get("enabled"))
    rows = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
    for r in rows:
        r.enabled = enabled
    # mirror the switch into the output playlist (vod/series; live channels are
    # curated custom channels and are added explicitly from the Playlist tab)
    synced = {}
    if payload.get("kind") in SYNC_KINDS:
        synced = await sync_sources(db, payload["kind"], [r.id for r in rows], enabled)
    await db.commit()
    await db_log("INFO", "sources",
                 f"{payload['kind']}: {len(rows)} items -> enabled={enabled}"
                 + (f" (playlist: +{synced.get('created', 0)} new, "
                    f"{synced.get('enabled', 0)} re-enabled, "
                    f"{synced.get('disabled', 0)} switched off)" if synced else ""))
    return {"ok": True, "count": len(rows), "playlist": synced}


@router.post("/series/seasons/toggle")
async def toggle_seasons(payload: dict, db=Depends(get_db)):
    ids = payload.get("ids", [])
    rows = (await db.execute(select(SerieSeason).where(SerieSeason.id.in_(ids)))).scalars().all()
    for r in rows:
        r.enabled = bool(payload.get("enabled"))
    await db.commit()
    return {"ok": True, "count": len(rows)}


# ------------------------------------------------------------------ local
@router.get("/local/dirs")
async def local_dirs(db=Depends(get_db), q: str = "", page: int = 1, per_page: int = 25,
                     sort: str = "directory", direction: str = "asc"):
    stmt = select(LocalSource)
    if q:
        stmt = stmt.where(LocalSource.directory.ilike(f"%{q}%"))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    col = LocalSource.directory
    stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    out = []
    for r in rows:
        n = await db.scalar(select(func.count()).select_from(LocalFile)
                            .where(LocalFile.local_source_id == r.id))
        out.append({"id": r.id, "directory": r.directory, "enabled": r.enabled,
                    "recursive": r.recursive, "file_count": n,
                    "last_scan": r.last_scan.isoformat() if r.last_scan else None})
    return {"total": total or 0, "page": page, "per_page": per_page, "items": out}


@router.post("/local/dirs")
async def add_local_dir(payload: dict, db=Depends(get_db)):
    directory = (payload.get("directory") or "").strip()
    if not directory:
        raise HTTPException(400, "directory required")
    if (await db.execute(select(LocalSource).where(LocalSource.directory == directory))).scalar_one_or_none():
        raise HTTPException(409, "directory already registered")
    row = LocalSource(directory=directory, enabled=True,
                      recursive=bool(payload.get("recursive", True)))
    db.add(row)
    await db.commit()
    await db_log("INFO", "local", f"local directory added: {directory}")
    return {"id": row.id}


@router.delete("/local/dirs/{did}")
async def del_local_dir(did: int, db=Depends(get_db)):
    row = await db.get(LocalSource, did)
    if not row:
        raise HTTPException(404, "not found")
    await db.delete(row)                     # cascades files + playlist rows
    await db.commit()
    await db_log("WARNING", "local", f"local directory removed: {row.directory}")
    return {"ok": True}


@router.post("/local/dirs/toggle")
async def toggle_local_dirs(payload: dict, db=Depends(get_db)):
    enabled = bool(payload.get("enabled"))
    rows = (await db.execute(select(LocalSource).where(
        LocalSource.id.in_(payload.get("ids", []))))).scalars().all()
    for r in rows:
        r.enabled = enabled
    # local files inherit the switch: mirror the whole directory into the Local
    # playlist in one go (bulk, single transaction)
    synced = {}
    if rows:
        file_ids = (await db.execute(select(LocalFile.id).where(
            LocalFile.local_source_id.in_([r.id for r in rows])))).scalars().all()
        synced = await sync_sources(db, "local", file_ids, enabled)
    await db.commit()
    return {"ok": True, "count": len(rows), "playlist": synced}


@router.post("/local/files/toggle")
async def toggle_local_files(payload: dict, db=Depends(get_db)):
    """Enable/disable individual scanned files (also syncs the Local playlist)."""
    enabled = bool(payload.get("enabled"))
    rows = (await db.execute(select(LocalFile).where(
        LocalFile.id.in_(payload.get("ids", []))))).scalars().all()
    for r in rows:
        r.enabled = enabled
    synced = await sync_sources(db, "local", [r.id for r in rows], enabled)
    await db.commit()
    return {"ok": True, "count": len(rows), "playlist": synced}


@router.post("/local/scan")
async def scan_local(payload: dict, db=Depends(get_db)):
    """Scan enabled directories for video files -> local_files table."""
    ids = payload.get("ids") or []
    stmt = select(LocalSource).where(LocalSource.enabled.is_(True))
    if ids:
        stmt = stmt.where(LocalSource.id.in_(ids))
    dirs = (await db.execute(stmt)).scalars().all()
    total_new, total_seen, total_skipped = 0, 0, 0
    added_to_playlist = 0
    for d in dirs:
        base = Path(d.directory)
        if not base.is_absolute():
            base = MEDIA_ROOT / d.directory
        try:
            is_dir = base.is_dir()        # raises PermissionError, not False
        except PermissionError:
            await db_log("ERROR", "local", permission_hint(base))
            continue
        if not is_dir:
            await db_log("ERROR", "local", f"directory not accessible: {base}")
            continue
        # os.walk() swallows unreadable sub-directories by default; collect them
        # instead so the log and the GUI say why a scan came back empty
        # (almost always: the mount is owned by another uid -> PUID/PGID).
        skipped: list[str] = []

        def _walk_error(err: OSError) -> None:
            skipped.append(str(getattr(err, "filename", base)))

        try:
            walker = (os.walk(base, onerror=_walk_error) if d.recursive
                      else [(str(base), [], os.listdir(base))])
        except PermissionError:
            await db_log("ERROR", "local", permission_hint(base))
            continue
        n_new = 0
        for root, _, files in walker:
            for fn in files:
                if Path(fn).suffix.lower() not in VIDEO_EXTS:
                    continue
                rel = os.path.relpath(os.path.join(root, fn), base)
                total_seen += 1
                exists = await db.scalar(select(func.count()).select_from(LocalFile).where(
                    LocalFile.local_source_id == d.id, LocalFile.relative_path == rel))
                if exists:
                    continue
                try:
                    st = os.stat(os.path.join(root, fn))
                except OSError as e:                      # unreadable file: skip, keep scanning
                    skipped.append(f"{os.path.join(root, fn)} ({e.strerror})")
                    continue
                db.add(LocalFile(local_source_id=d.id, relative_path=rel, filename=fn,
                                 size_bytes=st.st_size,
                                 mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()))
                n_new += 1
        if skipped:
            total_skipped += len(skipped)
            await db_log("ERROR", "local",
                         f"{len(skipped)} path(s) skipped while scanning {base}, "
                         f"first: {skipped[0]} - {permission_hint(base)}")
        d.last_scan = datetime.now(timezone.utc)
        await db.commit()
        total_new += n_new
        await db_log("INFO", "local", f"scanned {base}: {n_new} new video files")
    # newly found files belong in the Local playlist straight away (bulk insert:
    # only rows that do not exist yet are created, nothing is switched off)
    if dirs:
        file_ids = (await db.execute(select(LocalFile.id).where(
            LocalFile.local_source_id.in_([d.id for d in dirs])))).scalars().all()
        add = await add_sources(db, "local", file_ids)
        await db.commit()
        added_to_playlist = add["added"]
        if add["added"]:
            await db_log("INFO", "local",
                         f"{add['added']} file(s) added to the Local playlist")
        need = await missing_duration_ids([d.id for d in dirs])
        if need:
            spawn(fill_local_durations(need), name="local-durations")
    return {"ok": True, "new": total_new, "seen": total_seen, "skipped": total_skipped,
            "playlist_added": added_to_playlist}


@router.get("/local/files")
async def local_files(db=Depends(get_db), source_id: int = 0, q: str = "",
                      page: int = 1, per_page: int = 25, sort: str = "filename",
                      direction: str = "asc"):
    stmt = select(LocalFile)
    if source_id:
        stmt = stmt.where(LocalFile.local_source_id == source_id)
    if q:
        stmt = stmt.where(or_(LocalFile.filename.ilike(f"%{q}%"),
                              LocalFile.relative_path.ilike(f"%{q}%")))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    col = LocalFile.filename if sort != "path" else LocalFile.relative_path
    stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    dirnames = {d.id: d.directory for d in (await db.execute(select(LocalSource))).scalars().all()}
    return {"total": total or 0, "page": page, "per_page": per_page, "items": [
        {"id": r.id, "filename": r.filename, "relative_path": r.relative_path,
         "directory": dirnames.get(r.local_source_id, ""), "size_bytes": r.size_bytes,
         "enabled": r.enabled} for r in rows]}


@router.get("/local/browse")
async def browse(path: str = ""):
    """Directory browser for the Add-dir popup (server-side, stays inside MEDIA_ROOT)."""
    base = Path(path) if path else MEDIA_ROOT
    if not base.is_absolute():
        base = MEDIA_ROOT / path
    base = base.resolve()
    if not str(base).startswith(str(MEDIA_ROOT.resolve())) and base != Path("/"):
        raise HTTPException(403, "outside media root")
    try:
        # NB: Path.is_dir()/exists() raise PermissionError (not False) when a
        # parent directory is not searchable - the same PUID/PGID situation.
        is_dir = base.is_dir()
    except PermissionError:
        raise HTTPException(403, permission_hint(base))
    if not is_dir:
        raise HTTPException(404, "directory not found")
    try:
        items = sorted([{"name": e.name, "path": str(e)}
                        for e in base.iterdir() if e.is_dir()], key=lambda x: x["name"].lower())
    except PermissionError:
        # host mount owned by another uid -> clean 403 with the fix, not a 500
        raise HTTPException(403, permission_hint(base))
    return {"path": str(base), "parent": str(base.parent), "dirs": items}
