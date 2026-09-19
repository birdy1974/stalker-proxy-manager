"""
Dashboard / streams / logs / settings / EPG sources / export-import API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from ..config import FALLBACK_STRATEGY, FETCH_PAGE_BUDGET, OUTPUT_BASE_URL, TMDB_API_KEY
from ..database import get_db
from ..models import (
    Area, AreaItemTemplate, EpgSource, Enigma2Profile, FFmpegTemplate, LiveGenre,
    LivePlaylist, LivePlaylistSource, LiveSource, LocalFile, LocalPlaylist,
    LocalSource, Log, Portal, SerieEpisode, SerieGenre, SeriePlaylist,
    SeriePlaylistSeason, SeriePlaylistSource, SerieSeason, SerieSource, Setting,
    User, VodGenre, VodPlaylist, VodPlaylistSource, VodSource,
)
from ..services.branding import DEFAULT_ID as DEFAULT_FAVICON, refresh as refresh_favicon
from ..services.playback import KIND_DEFAULT_COL
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
        "areas": await cnt(Area),
        "users": await cnt(User),
        "enigma2_profiles": await cnt(Enigma2Profile),
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
    "epg_portal_enabled": True,
    "logo_country": "netherlands",
    "tmdb_api_key": TMDB_API_KEY,              # initial env seed; stored GUI value wins
    "fetch_page_budget": FETCH_PAGE_BUDGET,
    "output_base_url": OUTPUT_BASE_URL,
    # VLC honours this per local-file entry; 0 omits the directive.
    "vlc_local_network_caching_ms": 500,
    # Background multi-MAC status/expiry sweep (minutes). 0 = paused.
    # Only portals with ≥2 MACs are visited when mac_health_multi_only is true.
    "mac_health_minutes": 60,
    "mac_health_multi_only": True,
    # Browser tab icon: id of a built-in (services/branding.py) or "custom" for
    # an uploaded picture. Picked in Settings, rides along in a settings backup.
    "favicon": DEFAULT_FAVICON,
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
    if "epg_refresh_hours" in payload:
        value = payload["epg_refresh_hours"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not 0 <= value <= 168:
            raise HTTPException(400, "EPG refresh hours must be a whole number from 0 (paused) to 168")
    if "epg_portal_enabled" in payload and not isinstance(payload["epg_portal_enabled"], bool):
        raise HTTPException(400, "Portal EPG enabled must be true or false")
    for k, v in payload.items():
        row = await db.get(Setting, k)
        if row is None:
            row = Setting(key=k)
            db.add(row)
        row.value = json.dumps(v)
    await db.commit()
    # Settings such as VLC's local-file cache directive alter generated M3Us;
    # do not serve the previous value for the normal two-minute cache window.
    from ..services.playlist_gen import clear_m3u_cache
    clear_m3u_cache()
    # The tab icon is normally changed from its own picker, but it is a settings
    # row like any other (and a restored backup writes it here): re-stamp the
    # cache-busting fingerprint so open tabs pick the new picture up.
    if "favicon" in payload:
        await refresh_favicon()
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
    from .api_epg import queue_cached_refresh
    queue_cached_refresh()
    return {"ok": True}


@router.delete("/epg-sources/{eid}")
async def del_epg(eid: int, db=Depends(get_db)):
    from .api_epg import delete_source
    if await db.get(EpgSource, eid):
        return await delete_source(eid, db)
    return {"ok": True}


# ------------------------------------------------------------------ export / import
async def _dump_areas(db) -> tuple[list[dict], list[dict], dict[int, str]]:
    """Playback areas + their per-item template exceptions, template picks
    written as template NAMES so a restore re-binds onto another id space."""
    tpls = {t.id: t.name for t in
            (await db.execute(select(FFmpegTemplate))).scalars().all()}
    areas = (await db.execute(select(Area))).scalars().all()
    area_names = {a.id: a.name for a in areas}

    def _tpl(tid):
        return tpls.get(tid) if tid else None

    areas_out = [{
        "name": a.name, "enabled": a.enabled, "notes": a.notes,
        "ffmpeg_template_live": _tpl(a.ffmpeg_template_live_id),
        "ffmpeg_template_vod": _tpl(a.ffmpeg_template_vod_id),
        "ffmpeg_template_series": _tpl(a.ffmpeg_template_series_id),
        "ffmpeg_template_local": _tpl(a.ffmpeg_template_local_id),
    } for a in areas]
    exceptions = (await db.execute(select(AreaItemTemplate))).scalars().all()
    exceptions_out = [{
        "area": area_names.get(r.area_id), "kind": r.kind,
        "playlist_id": r.playlist_id,
        "ffmpeg_template": _tpl(r.ffmpeg_template_id),
    } for r in exceptions if area_names.get(r.area_id)]
    return areas_out, exceptions_out, area_names


async def _dump_enigma2(db) -> list[dict]:
    """Enigma2 receiver profiles, in the same name-reference style as the
    rest of the backup (the SPM user by name, for the credentials and group
    filters the box's bouquets carry). Runtime state - last build/push,
    counters - belongs to the machine that built it, not to the backup.

    `token` and the receiver passwords ride along deliberately: the token is
    what a box's existing install URL points at, and a backup that dropped
    them would hand back a profile the receiver cannot talk to."""
    users = {u.id: u.name for u in (await db.execute(select(User))).scalars().all()}
    out = []
    for p in (await db.execute(select(Enigma2Profile))).scalars().all():
        out.append({
            "name": p.name, "enabled": p.enabled,
            "user": users.get(p.user_id) if p.user_id else None,
            "token": p.token,
            "host": p.host, "web_port": p.web_port, "use_https": p.use_https,
            "owif_auth": p.owif_auth, "owif_user": p.owif_user,
            "owif_pass": p.owif_pass,
            "transport": p.transport, "ftp_port": p.ftp_port,
            "ssh_port": p.ssh_port, "login": p.login, "password": p.password,
            "bouquet_prefix": p.bouquet_prefix,
            "player_live": p.player_live, "player_vod": p.player_vod,
            "player_series": p.player_series,
            "container_mode": p.container_mode, "container_live": p.container_live,
            "container_vod": p.container_vod, "container_series": p.container_series,
            "delivery_mode": p.delivery_mode,
            "include_live": p.include_live, "include_vod": p.include_vod,
            "include_series": p.include_series, "include_local": p.include_local,
            "groups_json": json.loads(p.groups_json) if p.groups_json else None,
            "layout": p.layout, "max_entries": p.max_entries,
        })
    return out


async def _dump_sources(db) -> dict:
    """Sources of every kind, with the NAME references (portal, genre,
    directory) a restore onto a different id space needs to re-bind.

    Runtime/health state is deliberately left out (MAC `status`/`last_scan`
    and friends belong to the machine that checked them, not to the backup).
    """
    portals = {p.id: p.name for p in (await db.execute(select(Portal))).scalars().all()}

    def _genre(rows, gid):
        return rows.get(gid)

    live_genres = {g.id: (g.name, g.genre_portal_id)
                   for g in (await db.execute(select(LiveGenre))).scalars().all()}
    vod_genres = {g.id: (g.name, g.genre_portal_id)
                  for g in (await db.execute(select(VodGenre))).scalars().all()}
    serie_genres = {g.id: (g.name, g.genre_portal_id)
                    for g in (await db.execute(select(SerieGenre))).scalars().all()}

    live = []
    for r in (await db.execute(select(LiveSource))).scalars().all():
        g = _genre(live_genres, r.live_genre_id)
        live.append({
            "portal": portals.get(r.portal_id),
            "genre": g[0] if g else None, "genre_portal_id": g[1] if g else None,
            "portal_channel_id": r.portal_channel_id, "number": r.number,
            "original_name": r.original_name, "cmd": r.cmd,
            "logo_original": r.logo_original, "epg_original": r.epg_original,
            "tv_archive": r.tv_archive, "censored": r.censored,
            "link_flags": r.link_flags, "xtream_url": r.xtream_url,
            "enabled": r.enabled,
        })
    vod = []
    for r in (await db.execute(select(VodSource))).scalars().all():
        g = _genre(vod_genres, r.vod_genre_id)
        vod.append({
            "portal": portals.get(r.portal_id),
            "genre": g[0] if g else None, "genre_portal_id": g[1] if g else None,
            "portal_item_id": r.portal_item_id, "original_name": r.original_name,
            "cmd": r.cmd, "position": r.position, "poster": r.poster,
            "year": r.year, "description": r.description, "genre_text": r.genre,
            "director": r.director, "actors": r.actors, "rating": r.rating,
            "duration": r.duration, "added": r.added, "link_flags": r.link_flags,
            "xtream_url": r.xtream_url, "media_cmd": r.media_cmd,
            "enabled": r.enabled,
        })
    serie = []
    seasons = {}
    for s in (await db.execute(select(SerieSeason))).scalars().all():
        seasons.setdefault(s.serie_source_id, []).append(s)
    for s in seasons.values():
        s.sort(key=lambda x: x.season_number)
    episodes = {}
    for e in (await db.execute(select(SerieEpisode))).scalars().all():
        episodes.setdefault(e.serie_season_id, []).append(e)
    for r in (await db.execute(select(SerieSource))).scalars().all():
        g = _genre(serie_genres, r.serie_genre_id)
        seasons_out = []
        for s in seasons.get(r.id, []):
            seasons_out.append({
                "season_number": s.season_number, "portal_season_id": s.portal_season_id,
                "name": s.name, "enabled": s.enabled, "episodes_fetched": s.episodes_fetched,
                "episodes": [{
                    "episode_number": e.episode_number, "portal_item_id": e.portal_item_id,
                    "name": e.name, "cmd": e.cmd, "duration": e.duration,
                    "link_flags": e.link_flags, "media_cmd": e.media_cmd,
                    "series_param": e.series_param,
                } for e in sorted(episodes.get(s.id, []),
                                  key=lambda x: x.episode_number)],
            })
        serie.append({
            "portal": portals.get(r.portal_id),
            "genre": g[0] if g else None, "genre_portal_id": g[1] if g else None,
            "portal_item_id": r.portal_item_id, "original_name": r.original_name,
            "poster": r.poster, "year": r.year, "description": r.description,
            "rating": r.rating, "category_name": r.category_name,
            "raw_series": r.raw_series, "enabled": r.enabled,
            "seasons_fetched": r.seasons_fetched, "seasons": seasons_out,
        })
    local = []
    files = {}
    for f in (await db.execute(select(LocalFile))).scalars().all():
        files.setdefault(f.local_source_id, []).append(f)
    for s in (await db.execute(select(LocalSource))).scalars().all():
        local.append({
            "directory": s.directory, "enabled": s.enabled, "recursive": s.recursive,
            "files": [{
                "relative_path": f.relative_path, "filename": f.filename,
                "size_bytes": f.size_bytes, "mtime": f.mtime,
                "duration_s": f.duration_s, "media_probe": f.media_probe,
                "enabled": f.enabled,
            } for f in sorted(files.get(s.id, []), key=lambda x: x.relative_path)],
        })
    return {"live": live, "vod": vod, "series": serie, "local": local}


async def _dump_playlists(db) -> dict:
    """Every playlist kind with its fallback chain, as NAME references a
    restore re-binds (template by name, sources by portal + portal item id,
    local files by directory + path)."""
    tpls = {t.id: t.name for t in (await db.execute(select(FFmpegTemplate))).scalars().all()}
    portals = {p.id: p.name for p in (await db.execute(select(Portal))).scalars().all()}

    def _tpl(tid):
        return tpls.get(tid)

    live_src = {s.id: s for s in (await db.execute(select(LiveSource))).scalars().all()}
    live_links = {}
    for l in (await db.execute(select(LivePlaylistSource))).scalars().all():
        live_links.setdefault(l.live_playlist_id, []).append(l)
    live = []
    for r in (await db.execute(select(LivePlaylist).order_by(LivePlaylist.order))).scalars().all():
        live.append({
            "custom_name": r.custom_name, "group_name": r.group_name,
            "number": r.number, "lock_number": r.lock_number,
            "epg_id": r.epg_id, "logo": r.logo,
            "ffmpeg_template": _tpl(r.ffmpeg_template_id),
            "enabled": r.enabled, "order": r.order,
            "sources": [{
                "portal": portals.get(live_src[l.live_source_id].portal_id)
                if l.live_source_id in live_src else None,
                "portal_channel_id": live_src[l.live_source_id].portal_channel_id
                if l.live_source_id in live_src else None,
                "original_name": live_src[l.live_source_id].original_name
                if l.live_source_id in live_src else None,
                "priority": l.priority,
            } for l in sorted(live_links.get(r.id, []), key=lambda x: x.priority)],
        })

    vod_src = {s.id: s for s in (await db.execute(select(VodSource))).scalars().all()}
    vod_links = {}
    for l in (await db.execute(select(VodPlaylistSource))).scalars().all():
        vod_links.setdefault(l.vod_playlist_id, []).append(l)

    def _vod_ref(s):
        if s is None:
            return None
        return {"portal": portals.get(s.portal_id),
                "portal_item_id": s.portal_item_id, "original_name": s.original_name}

    vod = []
    for r in (await db.execute(select(VodPlaylist).order_by(VodPlaylist.order))).scalars().all():
        prim = vod_src.get(r.vod_source_id)
        vod.append({
            "custom_name": r.custom_name, "group_name": r.group_name, "logo": r.logo,
            "tmdb_id": r.tmdb_id, "overview": r.overview, "poster": r.poster,
            "rating": r.rating, "year": r.year,
            "ffmpeg_template": _tpl(r.ffmpeg_template_id),
            "enabled": r.enabled, "order": r.order,
            "primary": _vod_ref(prim),
            "sources": [{**_vod_ref(vod_src.get(l.vod_source_id)), "priority": l.priority}
                        for l in sorted(vod_links.get(r.id, []), key=lambda x: x.priority)],
        })

    serie_src = {s.id: s for s in (await db.execute(select(SerieSource))).scalars().all()}
    serie_links = {}
    for l in (await db.execute(select(SeriePlaylistSource))).scalars().all():
        serie_links.setdefault(l.serie_playlist_id, []).append(l)
    season_links = {}
    for s in (await db.execute(select(SeriePlaylistSeason))).scalars().all():
        season_links.setdefault(s.serie_playlist_id, []).append(s)
    season_meta = {s.id: s for s in (await db.execute(select(SerieSeason))).scalars().all()}

    def _serie_ref(s):
        if s is None:
            return None
        return {"portal": portals.get(s.portal_id),
                "portal_item_id": s.portal_item_id, "original_name": s.original_name}

    series = []
    for r in (await db.execute(select(SeriePlaylist).order_by(SeriePlaylist.order))).scalars().all():
        prim = serie_src.get(r.serie_source_id)
        seasons_out = []
        for sl in season_links.get(r.id, []):
            sm = season_meta.get(sl.serie_season_id)
            if sm is None:
                continue
            # the pick names its serie: the same season number can exist on
            # every fallback linked to this playlist
            ss = serie_src.get(sm.serie_source_id)
            seasons_out.append({
                "portal": portals.get(ss.portal_id) if ss else None,
                "portal_item_id": ss.portal_item_id if ss else None,
                "season_number": sm.season_number,
                "enabled": sl.enabled,
            })
        seasons_out.sort(key=lambda x: x["season_number"])
        series.append({
            "custom_name": r.custom_name, "group_name": r.group_name, "logo": r.logo,
            "tmdb_id": r.tmdb_id, "overview": r.overview, "poster": r.poster,
            "rating": r.rating, "year": r.year,
            "ffmpeg_template": _tpl(r.ffmpeg_template_id),
            "enabled": r.enabled, "order": r.order,
            "primary": _serie_ref(prim),
            "sources": [{**_serie_ref(serie_src.get(l.serie_source_id)), "priority": l.priority}
                        for l in sorted(serie_links.get(r.id, []), key=lambda x: x.priority)],
            "seasons": seasons_out,
        })

    local_files = {f.id: f for f in (await db.execute(select(LocalFile))).scalars().all()}
    local_dirs = {s.id: s.directory
                  for s in (await db.execute(select(LocalSource))).scalars().all()}
    local = []
    for r in (await db.execute(select(LocalPlaylist).order_by(LocalPlaylist.order))).scalars().all():
        f = local_files.get(r.local_file_id)
        local.append({
            "custom_name": r.custom_name, "group_name": r.group_name,
            "ffmpeg_template": _tpl(r.ffmpeg_template_id),
            "enabled": r.enabled, "order": r.order,
            "file": None if f is None else {
                "directory": local_dirs.get(f.local_source_id),
                "relative_path": f.relative_path, "filename": f.filename,
            },
        })
    return {"live": live, "vod": vod, "series": series, "local": local}


@router.get("/export")
async def export_config(section: str = "all", db=Depends(get_db)):
    """Versioned JSON backup. Sections: portals, ffmpeg, users, areas,
    settings, epg, sources, playlists, enigma2, all.

    `sources` (live/vod/series/local files) and `playlists` (all four kinds
    with their fallback chains) are the operator's curated catalog: they carry
    the `enabled` flags, custom names, logos, epg ids, template assignments and
    source priorities, as NAME references a restore can re-bind on another
    install.
    """
    if section not in {"all", "portals", "ffmpeg", "users", "areas", "settings", "epg", "sources", "playlists", "enigma2"}:
        raise HTTPException(400, "Unknown backup section.")
    data: dict = {"app": "stalker-proxy-manager", "version": 1, "section": section}

    async def dump(model, fields):
        rows = (await db.execute(select(model))).scalars().all()
        return [{f: _jsonable(getattr(r, f)) for f in fields} for r in rows]

    if section in ("all", "portals"):
        from ..models import MacAddress
        # portal_version / modules ride along: they are what gates a fetch job,
        # so a restored install that lost them would re-gate on a fresh probe of
        # a panel that may be unreachable from the machine that restored it
        # `xtream` rides along *unmasked*, deliberately: it is a harvested Xtream
        # login, and a backup that restored `****` would hand back a portal that
        # cannot authenticate - a silent dead bridge. That makes this export a
        # secret-bearing file; it is admin-only and it already carries user rows.
        data["portals"] = await dump(Portal, ["name", "base_url", "enabled", "proxy_url",
                                              "tls_insecure", "identity_mode", "stb_timezone",
                                              "direct_links", "portal_version", "modules",
                                              "xtream", "xtream_at", "xtream_adopted"])
        macs = (await db.execute(select(MacAddress, Portal)
                                 .join(Portal, Portal.id == MacAddress.portal_id))).all()
        # sn / device_id travel with the MAC on purpose: they are the serial this
        # portal enrolled, so a backup that drops them would hand the panel a new
        # device the next time it is restored.
        # `password` rides along like the other secrets in this file: without
        # it a MAC restored onto a new install is a serial the panel has never
        # enrolled, and every stream through it fails.
        data["macs"] = [{"portal": p.name, "mac": m.mac, "order": m.order,
                         "sn": m.sn, "device_id": m.device_id,
                         "password": m.password}
                        for m, p in macs]
        data["live_genres"] = await dump(LiveGenre, ["portal_id", "genre_portal_id", "name", "enabled"])
        data["vod_genres"] = await dump(VodGenre, ["portal_id", "genre_portal_id", "name", "enabled"])
        data["serie_genres"] = await dump(SerieGenre, ["portal_id", "genre_portal_id", "name", "enabled"])
    if section in ("all", "ffmpeg"):
        data["ffmpeg_templates"] = await dump(
            FFmpegTemplate, [c for c in FFmpegTemplate.__table__.columns.keys() if c != "id"])
    if section in ("all", "areas"):
        # areas + their per-item template exceptions (templates by name);
        # the users section carries the same arrays, so `all` keeps one copy
        data["areas"], data["area_item_templates"], _ = await _dump_areas(db)
    if section in ("all", "users"):
        areas_out, exceptions_out, area_names = await _dump_areas(db)
        data["areas"] = areas_out
        data["area_item_templates"] = exceptions_out
        users = (await db.execute(select(User))).scalars().all()
        data["users"] = [{
            "name": u.name, "password": u.password,
            "m3u_enabled": u.m3u_enabled, "xtream_enabled": u.xtream_enabled,
            "expire_date": u.expire_date, "max_connections": u.max_connections,
            "enabled": u.enabled, "groups_json": u.groups_json,
            "area": area_names.get(u.area_id) if u.area_id else None,
        } for u in users]
    if section in ("all", "settings"):
        data["settings"] = {r.key: json.loads(r.value or "null")
                            for r in (await db.execute(select(Setting))).scalars().all()}
    if section in ("all", "epg"):
        data["epg_sources"] = await dump(EpgSource, ["url", "enabled"])
    if section in ("all", "sources"):
        src = await _dump_sources(db)
        # the genre rows, name-keyed (the id-keyed copies in the `portals`
        # section reference an id space that does not survive a restore onto
        # another install) - genres gate what a fetch pulls, so they travel
        # with the sources they filter
        pnames = {p.id: p.name
                  for p in (await db.execute(select(Portal))).scalars().all()}
        for key, model in (("live_genres", LiveGenre), ("vod_genres", VodGenre),
                           ("serie_genres", SerieGenre)):
            src[key + "_by_name"] = [{
                "portal": pnames.get(g.portal_id),
                "genre_portal_id": g.genre_portal_id, "name": g.name,
                "enabled": g.enabled,
            } for g in (await db.execute(select(model))).scalars().all()]
        data["sources"] = src
    if section in ("all", "playlists"):
        data["playlists"] = await _dump_playlists(db)
    if section in ("all", "enigma2"):
        data["enigma2_profiles"] = await _dump_enigma2(db)
    return JSONResponse(data, headers={"Content-Disposition":
                                       f'attachment; filename="spm-{section}.json"'})


def _jsonable(v):
    return v.isoformat() if isinstance(v, datetime) else v


@router.post("/import")
async def import_config(payload: dict, db=Depends(get_db)):
    """Legacy v1 importer. Add missing identities; never overwrite existing rows.

    The Settings GUI uses the complete v2 backup API for new exports. This
    endpoint remains available for older section-based backup files.
    """
    mode = payload.get("mode", "merge")
    if mode not in ("merge", "add_only"):
        raise HTTPException(400, "Only additive restore is supported.")
    mode = "merge"
    data = payload.get("data", {})
    if not isinstance(data, dict) or not data or not any(k in data for k in (
            "ffmpeg_templates", "areas", "area_item_templates", "users", "portals",
            "macs", "sources", "playlists", "settings", "epg_sources",
            "enigma2_profiles", "live_playlist", "live_genres", "vod_genres", "serie_genres")):
        raise HTTPException(400, "Not a recognized legacy backup.")
    applied = {"skipped": [], "imported": 0, "updated": 0}

    def _bump(res):
        if res == "existing":
            applied["skipped"].append("existing catalog row")
        else:
            applied["imported"] += 1

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

    tpl_by_name = {t.name: t.id for t in
                   (await db.execute(select(FFmpegTemplate))).scalars().all()}

    def _tpl_id(name):
        return tpl_by_name.get(name) if name else None

    for a in data.get("areas", []):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        exists = (await db.execute(select(Area).where(Area.name == name))).scalar_one_or_none()
        if exists and mode == "merge":
            applied["skipped"].append(f"area:{name}")
            continue
        row = exists or Area(name=name)
        row.enabled = bool(a.get("enabled", True))
        if "notes" in a:
            row.notes = a.get("notes") or None
        for kind, col in KIND_DEFAULT_COL.items():
            key = f"ffmpeg_template_{kind}"
            if key in a:
                setattr(row, col, _tpl_id(a.get(key)))
            elif col in a:
                setattr(row, col, a.get(col) if a.get(col) in tpl_by_name.values() else None)
        if not exists:
            db.add(row)
        applied["imported"] += 1
    await db.flush()

    area_by_name = {r.name: r.id for r in
                    (await db.execute(select(Area))).scalars().all()}
    known_user = {c.name for c in User.__table__.columns} - {"id"}
    for u in data.get("users", []):
        exists = (await db.execute(select(User).where(User.name == u.get("name")))).scalar_one_or_none()
        if exists:
            applied["skipped"].append(f"user:{u.get('name')}")
            continue
        udata = {k: v for k, v in u.items() if k in known_user}
        area_name = u.get("area")
        if area_name:
            udata["area_id"] = area_by_name.get(area_name)
        elif udata.get("area_id") not in set(area_by_name.values()):
            udata["area_id"] = None
        db.add(User(**udata))
        applied["imported"] += 1

    for ex in data.get("area_item_templates", []):
        aid = area_by_name.get(ex.get("area"))
        tid = _tpl_id(ex.get("ffmpeg_template"))
        kind = (ex.get("kind") or "").strip()
        pid = int(ex.get("playlist_id") or 0)
        if not aid or not tid or kind not in KIND_DEFAULT_COL or not pid:
            continue
        exists = (await db.execute(select(AreaItemTemplate).where(
            AreaItemTemplate.area_id == aid, AreaItemTemplate.kind == kind,
            AreaItemTemplate.playlist_id == pid))).scalar_one_or_none()
        if exists and mode == "merge":
            applied["skipped"].append(f"area-item:{ex.get('area')}:{kind}:{pid}")
            continue
        if exists:
            exists.ffmpeg_template_id = tid
        else:
            db.add(AreaItemTemplate(area_id=aid, kind=kind, playlist_id=pid,
                                    ffmpeg_template_id=tid))
        applied["imported"] += 1

    # ---- enigma2 receiver profiles: merged by name (unique). The SPM user is
    #      re-bound by name; a backup written on another install's id space
    #      would point the box at the wrong credentials otherwise. The pull
    #      token rides along so an existing install URL keeps working; a
    #      hand-edited file without one gets a fresh token.
    from ..services import enigma2_bouquets as e2bq
    user_by_name = {u.name: u.id for u in
                    (await db.execute(select(User))).scalars().all()}
    for p in data.get("enigma2_profiles", []):
        name = (p.get("name") or "").strip()
        if not name:
            continue
        exists = (await db.execute(select(Enigma2Profile).where(
            Enigma2Profile.name == name))).scalar_one_or_none()
        if exists:
            applied["skipped"].append(f"enigma2:{name}")
            continue
        prof = Enigma2Profile(name=name)
        for f in ("enabled", "host", "web_port", "use_https", "owif_auth",
                  "owif_user", "owif_pass", "transport", "ftp_port",
                  "ssh_port", "login", "password", "bouquet_prefix",
                  "player_live", "player_vod", "player_series",
                  "container_mode", "container_live", "container_vod",
                  "container_series", "delivery_mode", "include_live",
                  "include_vod", "include_series", "include_local",
                  "layout", "max_entries"):
            if f in p:
                setattr(prof, f, p[f])
        if "groups_json" in p and p["groups_json"] is not None:
            prof.groups_json = json.dumps(p["groups_json"])
        uname = (p.get("user") or "").strip()
        if uname:
            uid = user_by_name.get(uname)
            if uid is not None:
                prof.user_id = uid
            else:
                applied["skipped"].append(f"enigma2-user:{uname}")
        prof.token = (p.get("token") or "").strip() or e2bq.new_token()
        db.add(prof)
        applied["imported"] += 1

    known = {c.name for c in Portal.__table__.columns} - {"id"}
    for p in data.get("portals", []):
        exists = (await db.execute(select(Portal).where(Portal.name == p.get("name")))).scalar_one_or_none()
        if exists:
            applied["skipped"].append(f"portal:{p.get('name')}")
            continue
        # filtered, not splatted: a backup written by a *newer* image carries
        # columns this one does not have, and `Portal(**p)` answers that with a
        # TypeError inside an import that the user cannot inspect
        db.add(Portal(**{k: v for k, v in p.items() if k in known}))
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

    # ---- MACs (exported in the `portals` section): merged by (portal, mac).
    #      Restoring the MACs - serial, device id and password included - is
    #      what makes a moved install present the same identity to the panel;
    #      an existing row keeps its local health state (merge, like portals).
    from ..models import MacAddress
    portal_by_name = {p.name: p.id for p in
                      (await db.execute(select(Portal))).scalars().all()}
    for m in data.get("macs", []):
        pid = portal_by_name.get((m.get("portal") or "").strip())
        mac = (m.get("mac") or "").strip().upper()
        if not pid or not mac:
            continue
        exists = (await db.execute(select(MacAddress).where(
            MacAddress.portal_id == pid,
            MacAddress.mac == mac))).scalar_one_or_none()
        if exists:
            applied["skipped"].append(f"mac:{mac}")
            continue
        db.add(MacAddress(portal_id=pid, mac=mac,
                          password=m.get("password"),
                          order=int(m.get("order") or 0),
                          sn=m.get("sn"), device_id=m.get("device_id")))
        applied["imported"] += 1

    # ---- sources: additive restore (existing identities are unchanged) ----
    src = data.get("sources") or {}
    if not isinstance(src, dict):
        src = {}

    # ---- genres: identity is (portal, genre_portal_id). The name-keyed rows
    #      nested in the `sources` section are portable; the id-keyed copies
    #      in the `portals` section only work on the install they were written
    #      on, which is exactly the case where their portal ids are still valid.
    for key, model in (("live_genres", LiveGenre), ("vod_genres", VodGenre),
                       ("serie_genres", SerieGenre)):
        entries: list[tuple[int, str, dict]] = []
        for g in src.get(key + "_by_name") or []:
            pid = portal_by_name.get((g.get("portal") or "").strip())
            gid = str(g.get("genre_portal_id") or "").strip()
            if pid and gid:
                entries.append((pid, gid, g))
        for g in data.get(key) or []:
            pid = g.get("portal_id")
            gid = str(g.get("genre_portal_id") or "").strip()
            if isinstance(pid, int) and gid:
                entries.append((pid, gid, g))
        seen: set[tuple[int, str]] = set()
        for pid, gid, g in entries:
            if (pid, gid) in seen:
                continue
            seen.add((pid, gid))
            exists = (await db.execute(select(model).where(
                model.portal_id == pid,
                model.genre_portal_id == gid))).scalar_one_or_none()
            if exists:
                applied["skipped"].append(f"genre:{(g.get('name') or gid)[:40]}")
                continue
            db.add(model(portal_id=pid, genre_portal_id=gid,
                         name=(g.get("name") or "").strip() or None,
                         enabled=bool(g.get("enabled", True))))
            applied["imported"] += 1

    async def _restore_row(model, ident: dict, values: dict):
        """Insert only; the identity-matched local row always wins."""
        exists = (await db.execute(select(model).where(*[
            getattr(model, k) == v for k, v in ident.items()]))).scalar_one_or_none()
        if exists is not None:
            return "existing"
        db.add(model(**{**ident, **values}))
        return "inserted"

    async def _genre_id(model, pid, e):
        """bind a source row to a genre: by portal id (the stable identity),
        then by name (what a hand-edited file carries). The genre phase above
        already imported this file's genre rows, so a backed-up genre resolves."""
        gid = str(e.get("genre_portal_id") or "").strip()
        if pid and gid:
            row = (await db.execute(select(model).where(
                model.portal_id == pid,
                model.genre_portal_id == gid))).scalar_one_or_none()
            if row is not None:
                return row.id
        gname = (e.get("genre") or "").strip()
        if pid and gname:
            row = (await db.execute(select(model).where(
                model.portal_id == pid,
                model.name == gname))).scalar_one_or_none()
            if row is not None:
                return row.id
        return None

    for e in src.get("live", []):
        pid = portal_by_name.get((e.get("portal") or "").strip())
        cid = str(e.get("portal_channel_id") or "").strip()
        label = e.get("original_name") or cid or "?"
        if not pid or not cid:
            applied["skipped"].append(f"live:{label[:60]} (portal missing)")
            continue
        _bump(await _restore_row(LiveSource,
                                 {"portal_id": pid, "portal_channel_id": cid},
                                 {
                                     "number": e.get("number"),
                                     "original_name": e.get("original_name"),
                                     "cmd": e.get("cmd"),
                                     "logo_original": e.get("logo_original"),
                                     "epg_original": e.get("epg_original"),
                                     "tv_archive": e.get("tv_archive"),
                                     "censored": e.get("censored"),
                                     "link_flags": e.get("link_flags"),
                                     "xtream_url": e.get("xtream_url"),
                                     "enabled": bool(e.get("enabled", True)),
                                     "live_genre_id": await _genre_id(LiveGenre, pid, e),
                                 }))
        await db.flush()

    for e in src.get("vod", []):
        pid = portal_by_name.get((e.get("portal") or "").strip())
        iid = str(e.get("portal_item_id") or "").strip()
        label = e.get("original_name") or iid or "?"
        if not pid or not iid:
            applied["skipped"].append(f"vod:{label[:60]} (portal missing)")
            continue
        _bump(await _restore_row(VodSource,
                                 {"portal_id": pid, "portal_item_id": iid},
                                 {
                                     "original_name": e.get("original_name"),
                                     "cmd": e.get("cmd"),
                                     "position": e.get("position"),
                                     "poster": e.get("poster"),
                                     "year": e.get("year"),
                                     "description": e.get("description"),
                                     "genre": e.get("genre_text"),
                                     "director": e.get("director"),
                                     "actors": e.get("actors"),
                                     "rating": e.get("rating"),
                                     "duration": e.get("duration"),
                                     "added": e.get("added"),
                                     "link_flags": e.get("link_flags"),
                                     "xtream_url": e.get("xtream_url"),
                                     "media_cmd": e.get("media_cmd"),
                                     "enabled": bool(e.get("enabled", True)),
                                     "vod_genre_id": await _genre_id(VodGenre, pid, e),
                                 }))
        await db.flush()

    for e in src.get("series", []):
        pid = portal_by_name.get((e.get("portal") or "").strip())
        iid = str(e.get("portal_item_id") or "").strip()
        label = e.get("original_name") or iid or "?"
        if not pid or not iid:
            applied["skipped"].append(f"series:{label[:60]} (portal missing)")
            continue
        _bump(await _restore_row(SerieSource,
                                 {"portal_id": pid, "portal_item_id": iid},
                                 {
                                     "original_name": e.get("original_name"),
                                     "poster": e.get("poster"),
                                     "year": e.get("year"),
                                     "description": e.get("description"),
                                     "rating": e.get("rating"),
                                     "category_name": e.get("category_name"),
                                     "raw_series": e.get("raw_series"),
                                     "enabled": bool(e.get("enabled", True)),
                                     "seasons_fetched": bool(e.get("seasons_fetched", False)),
                                     "serie_genre_id": await _genre_id(SerieGenre, pid, e),
                                 }))
        await db.flush()
        srow = (await db.execute(select(SerieSource).where(
            SerieSource.portal_id == pid,
            SerieSource.portal_item_id == iid))).scalar_one()
        for s in e.get("seasons") or []:
            sn = s.get("season_number")
            if not isinstance(sn, int):
                continue
            _bump(await _restore_row(SerieSeason,
                                     {"serie_source_id": srow.id,
                                      "season_number": sn},
                                     {
                                         "portal_season_id": s.get("portal_season_id"),
                                         "name": s.get("name"),
                                         "enabled": bool(s.get("enabled", True)),
                                         "episodes_fetched": bool(s.get("episodes_fetched", False)),
                                     }))
            await db.flush()
            sseason = (await db.execute(select(SerieSeason).where(
                SerieSeason.serie_source_id == srow.id,
                SerieSeason.season_number == sn))).scalar_one()
            for ep in s.get("episodes") or []:
                en = ep.get("episode_number")
                if not isinstance(en, int):
                    continue
                _bump(await _restore_row(SerieEpisode,
                                         {"serie_season_id": sseason.id,
                                          "episode_number": en},
                                         {
                                             "portal_item_id": ep.get("portal_item_id"),
                                             "name": ep.get("name"),
                                             "cmd": ep.get("cmd"),
                                             "duration": ep.get("duration"),
                                             "link_flags": ep.get("link_flags"),
                                             "media_cmd": ep.get("media_cmd"),
                                             "series_param": ep.get("series_param"),
                                         }))
                await db.flush()

    for e in src.get("local", []):
        d = (e.get("directory") or "").strip()
        if not d:
            continue
        _bump(await _restore_row(LocalSource, {"directory": d},
                                 {"enabled": bool(e.get("enabled", True)),
                                  "recursive": bool(e.get("recursive", False))}))
        await db.flush()
        drow = (await db.execute(select(LocalSource).where(
            LocalSource.directory == d))).scalar_one()
        for f in e.get("files") or []:
            rp = (f.get("relative_path") or "").strip()
            if not rp:
                continue
            _bump(await _restore_row(LocalFile,
                                     {"local_source_id": drow.id,
                                      "relative_path": rp},
                                     {
                                         "filename": f.get("filename") or rp.rsplit("/", 1)[-1],
                                         "size_bytes": f.get("size_bytes"),
                                         "mtime": f.get("mtime"),
                                         "duration_s": f.get("duration_s"),
                                         "media_probe": f.get("media_probe"),
                                         "enabled": bool(f.get("enabled", True)),
                                     }))
            await db.flush()

    # ---- playlists: add missing rows/links, keep existing fields unchanged.
    #      Rows bind by custom_name (first match). A new vod/serie/local row
    #      whose required primary source cannot be resolved is skipped (noted).
    async def _first(q):
        return (await db.execute(q.limit(1))).scalars().first()

    async def _resolve_live_source(e):
        pid = portal_by_name.get((e.get("portal") or "").strip())
        cid = str(e.get("portal_channel_id") or "").strip()
        if not pid or not cid:
            return None
        return (await db.execute(select(LiveSource).where(
            LiveSource.portal_id == pid,
            LiveSource.portal_channel_id == cid))).scalar_one_or_none()

    async def _resolve_vod_source(ref):
        if not isinstance(ref, dict):
            return None
        pid = portal_by_name.get((ref.get("portal") or "").strip())
        iid = str(ref.get("portal_item_id") or "").strip()
        if not pid or not iid:
            return None
        return (await db.execute(select(VodSource).where(
            VodSource.portal_id == pid,
            VodSource.portal_item_id == iid))).scalar_one_or_none()

    async def _resolve_serie_source(ref):
        if not isinstance(ref, dict):
            return None
        pid = portal_by_name.get((ref.get("portal") or "").strip())
        iid = str(ref.get("portal_item_id") or "").strip()
        if not pid or not iid:
            return None
        return (await db.execute(select(SerieSource).where(
            SerieSource.portal_id == pid,
            SerieSource.portal_item_id == iid))).scalar_one_or_none()

    async def _resolve_local_file(ref):
        if not isinstance(ref, dict):
            return None
        d = (ref.get("directory") or "").strip()
        rp = (ref.get("relative_path") or "").strip()
        if not d or not rp:
            return None
        drow = (await db.execute(select(LocalSource).where(
            LocalSource.directory == d))).scalar_one_or_none()
        if drow is None:
            return None
        return (await db.execute(select(LocalFile).where(
            LocalFile.local_source_id == drow.id,
            LocalFile.relative_path == rp))).scalar_one_or_none()

    async def _restore_links(link_model, playlist_id_col, source_id_col,
                             playlist_id, entries, resolver, label):
        for s in entries or []:
            src_row = await resolver(s)
            if src_row is None:
                applied["skipped"].append(
                    f"{label}->{(s.get('original_name') or '?')[:40]} (source missing)")
                continue
            link = (await db.execute(select(link_model).where(
                getattr(link_model, playlist_id_col) == playlist_id,
                getattr(link_model, source_id_col) == src_row.id))).scalar_one_or_none()
            if link is not None:
                applied["skipped"].append(f"{label}: existing source link")
            else:
                db.add(link_model(**{
                    playlist_id_col: playlist_id,
                    source_id_col: src_row.id,
                    "priority": int(s.get("priority") or 0),
                }))
                applied["imported"] += 1

    pls = data.get("playlists") or {}
    if not isinstance(pls, dict):
        pls = {}

    for e in pls.get("live", []):
        name = (e.get("custom_name") or "").strip()
        if not name:
            continue
        row = await _first(select(LivePlaylist).where(LivePlaylist.custom_name == name))
        new_row = row is None
        if new_row:
            row = LivePlaylist(custom_name=name)
            db.add(row)
        if new_row:
            row.group_name = e.get("group_name")
            row.number = e.get("number")
            row.lock_number = bool(e.get("lock_number", False))
            row.epg_id = e.get("epg_id")
            row.logo = e.get("logo")
            row.enabled = bool(e.get("enabled", True))
            row.order = int(e.get("order") or 0)
            row.ffmpeg_template_id = _tpl_id(e.get("ffmpeg_template"))
        await db.flush()
        await _restore_links(LivePlaylistSource, "live_playlist_id",
                             "live_source_id", row.id, e.get("sources"),
                             _resolve_live_source, f"live-link:{name[:40]}")
        _bump("inserted" if new_row else "existing")
        await db.flush()

    for e in pls.get("vod", []):
        name = (e.get("custom_name") or "").strip()
        if not name:
            continue
        prim = await _resolve_vod_source(e.get("primary"))
        row = await _first(select(VodPlaylist).where(VodPlaylist.custom_name == name))
        if row is None:
            if prim is None:
                applied["skipped"].append(f"vod-playlist:{name[:60]} (primary source missing)")
                continue
            row = VodPlaylist(custom_name=name, vod_source_id=prim.id)
            db.add(row)
            new_row = True
        else:
            new_row = False
        if new_row:
            row.group_name = e.get("group_name")
            row.logo = e.get("logo")
            row.tmdb_id = e.get("tmdb_id")
            row.overview = e.get("overview")
            row.poster = e.get("poster")
            row.rating = e.get("rating")
            row.year = e.get("year")
            row.enabled = bool(e.get("enabled", True))
            row.order = int(e.get("order") or 0)
            row.ffmpeg_template_id = _tpl_id(e.get("ffmpeg_template"))
        await db.flush()
        await _restore_links(VodPlaylistSource, "vod_playlist_id",
                             "vod_source_id", row.id, e.get("sources"),
                             _resolve_vod_source, f"vod-link:{name[:40]}")
        _bump("inserted" if new_row else "existing")
        await db.flush()

    for e in pls.get("series", []):
        name = (e.get("custom_name") or "").strip()
        if not name:
            continue
        prim = await _resolve_serie_source(e.get("primary"))
        row = await _first(select(SeriePlaylist).where(SeriePlaylist.custom_name == name))
        if row is None:
            if prim is None:
                applied["skipped"].append(f"serie-playlist:{name[:60]} (primary source missing)")
                continue
            row = SeriePlaylist(custom_name=name, serie_source_id=prim.id)
            db.add(row)
            new_row = True
        else:
            new_row = False
        if new_row:
            row.group_name = e.get("group_name")
            row.logo = e.get("logo")
            row.tmdb_id = e.get("tmdb_id")
            row.overview = e.get("overview")
            row.poster = e.get("poster")
            row.rating = e.get("rating")
            row.year = e.get("year")
            row.enabled = bool(e.get("enabled", True))
            row.order = int(e.get("order") or 0)
            row.ffmpeg_template_id = _tpl_id(e.get("ffmpeg_template"))
        await db.flush()
        await _restore_links(SeriePlaylistSource, "serie_playlist_id",
                             "serie_source_id", row.id, e.get("sources"),
                             _resolve_serie_source, f"serie-link:{name[:40]}")
        for s in e.get("seasons") or []:
            sn = s.get("season_number")
            if not isinstance(sn, int):
                continue
            # the pick names the season's serie, because the same number can
            # exist on every fallback linked to this playlist
            srow = await _resolve_serie_source(s)
            if srow is None:
                applied["skipped"].append(
                    f"serie-season:{name[:40]}->S{sn} (serie missing)")
                continue
            sseason = (await db.execute(select(SerieSeason).where(
                SerieSeason.serie_source_id == srow.id,
                SerieSeason.season_number == sn))).scalar_one_or_none()
            if sseason is None:
                applied["skipped"].append(
                    f"serie-season:{name[:40]}->S{sn} (season missing)")
                continue
            link = (await db.execute(select(SeriePlaylistSeason).where(
                SeriePlaylistSeason.serie_playlist_id == row.id,
                SeriePlaylistSeason.serie_season_id == sseason.id))).scalar_one_or_none()
            if link is not None:
                applied["skipped"].append(f"serie-season:{name}: existing season link")
            else:
                db.add(SeriePlaylistSeason(serie_playlist_id=row.id,
                                           serie_season_id=sseason.id,
                                           enabled=bool(s.get("enabled", True))))
                applied["imported"] += 1
        _bump("inserted" if new_row else "existing")
        await db.flush()

    for e in pls.get("local", []):
        ref = e.get("file")
        name = (e.get("custom_name") or "").strip()
        frow = await _resolve_local_file(ref)
        row = await _first(select(LocalPlaylist).where(LocalPlaylist.custom_name == name)) if name else None
        if row is None and frow is not None:
            # an unnamed local playlist's identity is the file it plays
            row = (await db.execute(select(LocalPlaylist).where(
                LocalPlaylist.local_file_id == frow.id))).scalars().first()
        if row is None:
            if frow is None:
                applied["skipped"].append(
                    f"local-playlist:{(name or (ref or {}).get('filename') or '?')[:60]} (file missing)")
                continue
            row = LocalPlaylist(custom_name=name or None, local_file_id=frow.id)
            db.add(row)
            new_row = True
        else:
            new_row = False
        if new_row:
            row.group_name = e.get("group_name")
            row.enabled = bool(e.get("enabled", True))
            row.order = int(e.get("order") or 0)
            row.ffmpeg_template_id = _tpl_id(e.get("ffmpeg_template"))
        _bump("inserted" if new_row else "existing")
        await db.flush()

    # ---- legacy `live_playlist` (files predating the `playlists` section):
    #      row fields only, no source links. Superseded by playlists.live when
    #      both are present, so it only runs on old files.
    if not pls.get("live"):
        for e in data.get("live_playlist") or []:
            name = (e.get("custom_name") or "").strip()
            if not name:
                continue
            row = await _first(select(LivePlaylist).where(LivePlaylist.custom_name == name))
            new_row = row is None
            if not new_row:
                applied["skipped"].append(f"live-playlist:{name}")
                continue
            if new_row:
                row = LivePlaylist(custom_name=name)
                db.add(row)
            if e.get("group_name") is not None:
                row.group_name = e.get("group_name")
            if e.get("number") is not None:
                row.number = e.get("number")
            if e.get("epg_id") is not None:
                row.epg_id = e.get("epg_id")
            if e.get("logo") is not None:
                row.logo = e.get("logo")
            row.enabled = bool(e.get("enabled", True))
            row.order = int(e.get("order") or 0)
            if e.get("ffmpeg_template_id") in tpl_by_name.values():
                row.ffmpeg_template_id = e.get("ffmpeg_template_id")
            _bump("inserted" if new_row else "updated")
            await db.flush()

    for k, v in (data.get("settings") or {}).items():
        row = await db.get(Setting, k)
        if row is not None:
            applied["skipped"].append(f"setting:{k}")
            continue
        db.add(Setting(key=k, value=json.dumps(v)))
        applied["imported"] += 1

    await db.commit()
    # a restored `favicon` row selects a different tab icon
    await refresh_favicon()
    await db_log("INFO", "import",
                 f"config import: {applied['imported']} imported, "
                 f"{applied['updated']} updated, {len(applied['skipped'])} skipped ({mode})")
    return applied
