"""
Public output endpoints (NO admin auth - they carry user credentials instead):

  /playlist.m3u?u=&p=             per-user M3U (also /get.php for Xtream players)
  /player_api.php                 Xtream Codes API subset
  /xmltv.php, /epg.xml            EPG output (merged sources land in Phase 3)
  /play/{kind}/{id}.ts?u=&p=      the actual proxied/transcoded streams
  /live/{u}/{p}/{id}.ts           Xtream-style URLs
  /movie/{u}/{p}/{id}.ts          Xtream-style VOD
  /series/{u}/{p}/{episode}.ts    Xtream-style series episodes
  /preview/{kind}/{id}            admin web-player probe (original sources)

Streams honour per-user max_connections and group filters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from ..config import OUTPUT_BASE_URL
from ..database import get_db
from ..models import (
    LivePlaylist, LiveSource, LocalPlaylist, LocalFile, SerieEpisode,
    SeriePlaylist, User, VodPlaylist, VodSource,
)
from ..services.db_logging import db_log
from ..services.playlist_gen import (
    UserAuth, build_m3u, xtream_base, xtream_categories, xtream_live,
    xtream_series, xtream_series_info, xtream_vod,
)
from ..services.stream_manager import MANAGER
from sqlalchemy import select

router = APIRouter(tags=["output"])


def base_url_of(request: Request) -> str:
    """Public base URL: settings/env override wins, else derive from request."""
    if OUTPUT_BASE_URL:
        return OUTPUT_BASE_URL
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


async def _authed(u: str | None, p: str | None, need: str = "m3u") -> User:
    user = await UserAuth.verify(u, p, need)
    if not user:
        raise HTTPException(403, "invalid credentials")
    return user


# ---------------------------------------------------------------- playlists
@router.get("/playlist.m3u")
async def playlist_m3u(request: Request, u: str = "", p: str = ""):
    user = await _authed(u, p, "m3u")
    text = await build_m3u(base_url_of(request), user)
    fname = f"playlist_{user.name}.m3u"
    return PlainTextResponse(text, media_type="application/x-mpegURL",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/get.php")
async def get_php(request: Request, username: str = "", password: str = "",
                  type: str = Query("m3u_plus"), output: str = "ts"):  # noqa: A002
    user = await _authed(username, password, "xtream")
    text = await build_m3u(base_url_of(request), user)
    return PlainTextResponse(text, media_type="application/x-mpegURL")


# ---------------------------------------------------------------- xtream api
@router.get("/player_api.php")
async def player_api(request: Request, username: str = "", password: str = "",
                     action: str = "", vod_id: int = 0, series_id: int = 0):
    user = await _authed(username, password, "xtream")
    base = base_url_of(request)
    if not action:
        return await xtream_base(user, base)
    if action == "get_live_categories":
        return await xtream_categories(user, "live")
    if action == "get_live_streams":
        return await xtream_live(user, base)
    if action == "get_vod_categories":
        return await xtream_categories(user, "vod")
    if action == "get_vod_streams":
        return await xtream_vod(user)
    if action == "get_vod_info":
        from ..services.playlist_gen import xtream_vod_info
        info = await xtream_vod_info(user, vod_id)
        if info is None:
            raise HTTPException(404, "vod not found")
        return info
    if action == "get_series":
        return await xtream_series(user)
    if action == "get_series_categories":
        return await xtream_categories(user, "series")
    if action == "get_series_info":
        info = await xtream_series_info(user, series_id)
        if info is None:
            raise HTTPException(404, "series not found")
        return info
    raise HTTPException(404, f"unknown action {action}")


@router.get("/xmltv.php")
@router.get("/epg.xml")
async def xmltv(request: Request, u: str = "", p: str = "", username: str = "", password: str = ""):
    """Merged XMLTV: the user's visible live channels + programmes of all
    enabled EPG sources (auto-matched or manually assigned tvg-ids)."""
    from ..services import epg as epg_svc
    user = await _authed(u or username, p or password, "xtream")
    xml = await epg_svc.build_xmltv(base_url_of(request), user)
    return Response(xml, media_type="application/xml")


# ---------------------------------------------------------------- streaming
async def _stream_mode(explicit: str) -> str:
    """`proxy` (ffmpeg in the middle) or `redirect` (302 to the panel's CDN)."""
    if explicit in ("proxy", "redirect"):
        return explicit
    from ..services.playlist_gen import get_setting
    mode = await get_setting("stream_mode", "proxy")
    return mode if mode in ("proxy", "redirect") else "proxy"


async def _stream_response(kind: str, ref_id: int, user: User | None, label: str,
                            request: Request, mode: str = ""):
    """
    Serve a stream: either proxied through ffmpeg (default) or redirected
    straight to the resolved portal URL.

    Redirect skips ffmpeg entirely - instant start, no CPU, works for panels
    whose stream ffmpeg refuses to remux - at the cost of mid-stream fallback
    and transport-stream rewriting. Local files can never be redirected (the
    client cannot see our filesystem), so they always proxy.
    """
    if await _stream_mode(mode) == "redirect" and kind != "local":
        from fastapi.responses import RedirectResponse
        url, item_name = await MANAGER.resolve(kind, ref_id)
        if url:
            await db_log("INFO", "output",
                         f"[{item_name}] redirect mode -> sending client to the source")
            return RedirectResponse(url, status_code=302)
        raise HTTPException(502, f"{label}: no source produced a link to redirect to")

    if not MANAGER.can_open_for(user.name if user else None,
                                user.max_connections if user else None):
        await db_log("WARNING", "output",
                     f"user {user.name if user else 'admin'} exceeded max_connections")
        raise HTTPException(429, "max connections reached for this user")
    handle, gen = await MANAGER.open(kind, ref_id, user.name if user else None)
    if handle.dead:
        raise HTTPException(404, f"{label}: no available source (all busy or unreachable)")
    # watchdog lives until the stream deregisters (normal end) or the client
    # disappears (then it kills the stream; see watch_disconnect). watch() keeps
    # a strong reference, so the task cannot be garbage-collected mid-flight.
    MANAGER.watch(request, handle)
    return StreamingResponse(gen, media_type="video/mp2t",
                             headers={"Cache-Control": "no-store",
                                      "X-SPM-Stream": handle.id})


@router.api_route("/play/live/{pid}.ts", methods=["GET", "HEAD"])
async def play_live(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "m3u")
    return await _stream_response("live", pid, user, f"live #{pid}", request, mode)


@router.get("/play/vod/{pid}.ts")
async def play_vod(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "m3u")
    return await _stream_response("vod", pid, user, f"vod #{pid}", request, mode)


@router.get("/play/episode/{eid}.ts")
async def play_episode(request: Request, eid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "m3u")
    return await _stream_response("episode", eid, user, f"episode #{eid}", request, mode)


@router.get("/play/local/{pid}.ts")
async def play_local(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "m3u")
    # local files are never redirectable: the client cannot see our filesystem
    return await _stream_response("local", pid, user, f"local #{pid}", request, "proxy")


# Xtream-style stream URLs -----------------------------------------------
@router.get("/live/{u}/{p}/{sid}.ts")
async def xlive(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await play_live(request, sid, u, p, mode)


@router.get("/movie/{u}/{p}/{sid}.ts")
async def xmovie(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await play_vod(request, sid, u, p, mode)


@router.get("/series/{u}/{p}/{sid}.ts")
async def xseries(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await play_episode(request, sid, u, p, mode)


# -------------------------------------------------- admin quick-play (GUI)
@router.get("/preview-play/{kind}/{pid}.ts")
async def admin_play(kind: str, pid: int, request: Request, mode: str = ""):
    """Play a PLAYLIST item from the GUI with the admin session (no user creds),
    through the *real* pipeline - template, fallback chain, MAC tracking."""
    from ..security import require_admin
    require_admin(request)
    if kind not in ("live", "vod", "episode", "local"):
        raise HTTPException(400, "kind must be live|vod|episode|local")
    return await _stream_response(kind, pid, None, f"{kind} #{pid}", request, mode)


# ---------------------------------------------------------------- preview
@router.get("/preview/{kind}/{sid}.ts")
async def preview(kind: str, sid: int, request: Request, db=Depends(get_db)):
    """
    Web-player probe of ORIGINAL source streams (spec: test before playlist).
    Admin-session OR valid user credentials both work. Uses the stream
    manager's preview pipe (passthrough copy, MAC fallback rules apply).
    """
    from ..security import require_admin
    try:
        require_admin(request)                       # GUI session
    except HTTPException:
        await _authed(request.query_params.get("u", ""),
                      request.query_params.get("p", ""), "m3u")

    from ..models import MacAddress, Portal, SerieSeason, SerieSource
    link_kind = "live"
    if kind == "live":
        src = await db.get(LiveSource, sid)
    elif kind == "vod":
        src = await db.get(VodSource, sid)
        link_kind = "vod"
    elif kind == "series":
        ep = await db.get(SerieEpisode, sid)
        season = await db.get(SerieSeason, ep.serie_season_id) if ep else None
        serie = await db.get(SerieSource, season.serie_source_id) if season else None
        src, portal_id = (ep, serie.portal_id) if serie else (None, None)
        link_kind = "vod"
    else:
        raise HTTPException(400, "kind must be live|vod|series")
    if not src or not getattr(src, "cmd", None):
        raise HTTPException(404, "source not found or has no stream cmd")
    pid = portal_id if kind == "series" else src.portal_id
    portal = await db.get(Portal, pid)
    macs = (await db.execute(select(MacAddress).where(MacAddress.portal_id == pid)
                             .order_by(MacAddress.order))).scalars().all()
    if not portal or not macs:
        raise HTTPException(404, "no portal/mac for this source")
    name = getattr(src, "original_name", None) or getattr(src, "name", "preview")
    handle, gen = await MANAGER._open_preview(src, portal, list(macs), kind=link_kind, name=name)
    if handle.dead:
        raise HTTPException(404, "preview failed: no data from source")
    MANAGER.watch(request, handle)
    return StreamingResponse(gen, media_type="video/mp2t",
                             headers={"Cache-Control": "no-store"})
