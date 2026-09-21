"""
Public output endpoints (NO admin auth - they carry user credentials instead):

  /playlist.m3u?u=&p=             per-user M3U (also /get.php for Xtream players)
  /player_api.php                 Xtream Codes API subset
  /xmltv.php, /epg.xml            EPG output (merged sources land in Phase 3)
  /play/{kind}/{id}.ts?u=&p=      the actual proxied/transcoded streams
  /play/{kind}/{id}.mkv?u=&p=     same stream, announced as Matroska (the
                                  subtitle-capable container: Enigma2 +
                                  exteplayer3, VLC, Kodi)
  /live/{u}/{p}/{id}.ts           Xtream-style URLs
  /movie/{u}/{p}/{id}.ts          Xtream-style VOD
  /series/{u}/{p}/{episode}.ts    Xtream-style series episodes
  /preview/{kind}/{id}            admin web-player probe (original sources)

Streams honour per-user max_connections and group filters.
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from ..database import get_db
from ..models import (
    LivePlaylist, LiveSource, LocalPlaylist, LocalFile, SerieEpisode,
    SeriePlaylist, User, VodPlaylist, VodSource,
)
from ..services.db_logging import db_log
from ..services.playlist_gen import (
    UserAuth, build_m3u, local_id_from_xtream, xtream_base, xtream_categories,
    xtream_live, xtream_series, xtream_series_info, xtream_vod,
)
from ..services.local_files import media_type_for
from ..services.stream_manager import MANAGER, START_BUDGET_SLACK
from sqlalchemy import select

router = APIRouter(tags=["output"])


async def base_url_of(request: Request) -> str:
    """Public base URL: GUI setting, then env, else derive from the request."""
    from ..services.runtime_settings import output_base_url
    override = await output_base_url()
    if override:
        return override
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


async def _authed(u: str | None, p: str | None, need: str = "m3u") -> User:
    user = await UserAuth.verify(u, p, need)
    if not user:
        raise HTTPException(403, "invalid credentials")
    return user


# ---------------------------------------------------------------- playlists
def _m3u_response(text: str, *, filename: str | None = None) -> PlainTextResponse:
    """Serve an M3U body the way players (VLC, Kodi, TiviMate, …) expect it.

    * `audio/x-mpegurl` is the historical type VLC sniffs most reliably;
      `application/x-mpegURL` alone is sometimes treated as an opaque download.
    * UTF-8 charset so non-ASCII channel names survive.
    * `inline` (not `attachment`): VLC opening the URL as a network stream must
      parse the body, not try to save a file. Browsers still offer Save-As via
      the filename parameter when the user clicks the link in the GUI.
    """
    headers = {
        "Cache-Control": "no-store",
        "Content-Type": "audio/x-mpegurl; charset=utf-8",
    }
    if filename:
        # Keep ASCII-safe filename; RFC 5987 filename* is overkill here.
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
        headers["Content-Disposition"] = f'inline; filename="{safe}"'
    return PlainTextResponse(text, media_type="audio/x-mpegurl; charset=utf-8",
                             headers=headers)


@router.get("/playlist.m3u")
async def playlist_m3u(request: Request, u: str = "", p: str = ""):
    user = await _authed(u, p, "m3u")
    text = await build_m3u(await base_url_of(request), user)
    return _m3u_response(text, filename=f"playlist_{user.name}.m3u")


@router.get("/get.php")
async def get_php(request: Request, username: str = "", password: str = "",
                  type: str = Query("m3u_plus"), output: str = "ts"):  # noqa: A002
    user = await _authed(username, password, "xtream")
    text = await build_m3u(await base_url_of(request), user)
    return _m3u_response(text, filename=f"playlist_{user.name}.m3u")


# ---------------------------------------------------------------- xtream api
@router.get("/player_api.php")
async def player_api(request: Request, username: str = "", password: str = "",
                     action: str = "", vod_id: int = 0, series_id: int = 0):
    user = await _authed(username, password, "xtream")
    base = await base_url_of(request)
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
    user = await _authed(u or username, p or password, "stream")
    xml = await epg_svc.build_xmltv(await base_url_of(request), user)
    return Response(xml, media_type="application/xml")


# ---------------------------------------------------------------- streaming
# Answering `200 OK` + `content-type: video/mp2t` with an empty body is what a
# browser turns into "player popup, black screen, no error anywhere" and what a
# set-top box turns into a silent hang. We can only change the status code
# before the first byte goes out, so peek at the first chunk and fail loudly.
#
# This is a BACKSTOP, not the engine's working budget. The fallback chain takes
# `candidates x SPM_STREAM_START_TIMEOUT x passes` (two MACs + the zap retry =
# 50s), so a fixed 25s guard used to fire while the pump was still walking - the
# log said "produced no data within 25s -> 502" and the retry pass never ran.
# `_guarded` therefore waits for `handle.start_budget + START_BUDGET_SLACK`
# whenever it is given a handle, and the engine stops itself at its own budget.
FIRST_CHUNK_TIMEOUT = float(os.environ.get("SPM_FIRST_CHUNK_TIMEOUT", "25"))
# Headers for every infinite stream: X-Accel-Buffering tells reverse proxies
# (nginx and everything speaking its conventions) not to buffer the response -
# a buffering proxy turns a live TS pipe into "player receives nothing", and
# the browser aborts the fetch long before the first frame.
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                  "Connection": "keep-alive"}
# One retry for zap overlap: Enigma2 opens the new channel while our
# disconnect watchdog (<=0.5s) is still tearing down the old pipe, so a
# user at max_connections would otherwise 429 on every fast zap. Honest
# overload still 429s, one short delay later.
MAXCONN_RETRY_DELAY = float(os.environ.get("SPM_MAXCONN_RETRY_DELAY", "1.2"))


def _guard_wait(handle=None) -> float:
    """Seconds the first-chunk guard waits, before calling the pipe dead.

    `FIRST_CHUNK_TIMEOUT` is the floor (a preview, a local file, an engine that
    reported no budget). A stream open hands us the engine's own budget, and the
    guard must not fire earlier than the engine's own deadline plus slack -
    otherwise "walking the fallbacks" reads as "produced no data" and the retry
    pass never runs. See stream_manager.STREAM_START_BUDGET.
    """
    wait = FIRST_CHUNK_TIMEOUT
    if handle is not None:
        wait = max(wait, float(getattr(handle, "start_budget", 0.0) or 0.0)
                   + START_BUDGET_SLACK)
    return wait


async def _guarded(gen, label: str, item_name: str = "", handle=None):
    """
    Yield `gen` unchanged, but only after proving it produces at least one
    chunk. Raises HTTPException(502) instead of streaming nothing.

    With a `handle` (a real stream open) the wait follows the engine's own start
    budget - how long the fallback chain may legitimately spend looking for a
    first byte - plus slack for the portal round trips in between. The 502 then
    names the MACs/sources that were tried and why each one failed, instead of
    blaming a timeout the engine had not even reached yet.
    """
    wait = _guard_wait(handle)
    first = None
    try:
        async with asyncio.timeout(wait):
            async for chunk in gen:
                if chunk:
                    first = chunk
                    break
    except TimeoutError:
        pass
    except Exception as exc:  # noqa: BLE001 - report, never swallow
        raise HTTPException(502, f"{label}: the stream pipe failed: "
                                 f"{type(exc).__name__}: {exc}")
    if first is None:
        detail = ""
        if handle is not None:
            note = getattr(handle, "fail_note", "") or ""
            trace = getattr(handle, "trace", "") or ""
            detail = " | ".join(part for part in (note, trace) if part)
        if handle is not None and getattr(handle, "busy", False):
            # Everything the engine tried was refused by occupancy - our own
            # pipe/lease, or the panel's connection slot. That is "come back in
            # a second", and it is the one failure players retry; 502 would tell
            # the box the channel is broken (and Enigma2 shows "no signal").
            await db_log("ERROR", "output",
                         f"[{item_name or label}] produced no data within "
                         f"{wait:.0f}s -> 503 + Retry-After (every candidate busy)"
                         + (f" - {detail}" if detail else ""))
            raise HTTPException(503, f"{label}: every MAC is busy right now"
                                     + (f" ({detail})" if detail else ""),
                                headers={"Retry-After": "2"})
        await db_log("ERROR", "output",
                     f"[{item_name or label}] produced no data within "
                     f"{wait:.0f}s -> 502 (not a silent 200)"
                     + (f" - {detail}" if detail else ""))
        raise HTTPException(502, f"{label}: the source produced no data "
                                 f"(ffmpeg missing, template failed, or the panel "
                                 f"returned an empty stream)."
                                 + (f" {detail}" if detail else "")
                                 + " Check Logs → stream.")

    async def body():
        yield first
        async for chunk in gen:
            yield chunk
    return body()


async def _wants_redirect(kind: str, ref_id: int, mode: str,
                          user_name: str | None = None) -> bool:
    """Whether this stream should 302 straight to the panel's CDN.

    Decided by, in priority order:
      1. an explicit `?mode=redirect|proxy` query param (per-URL override);
      2. the item's effective FFmpeg template (area overlay, then the
         playlist assignment, then the built-in redirect default).
    """
    if mode in ("proxy", "redirect"):
        return mode == "redirect"
    return await MANAGER.uses_redirect(kind, ref_id, user_name)


# Container announced to the client. The real container is whatever the item's
# ffmpeg template muxes (`output_format`); the URL extension only decides the
# Content-Type - and set-top boxes DO sniff the extension, which is why the
# Enigma2 VOD/series bouquets point at `.mkv` while live keeps `.ts`.
MEDIA_TYPES = {"ts": "video/mp2t", "mkv": "video/x-matroska"}


def _stream_head(media_type: str = "video/mp2t") -> Response:
    """Metadata-only player probe: never resolve a portal or start FFmpeg."""
    return Response(status_code=200, media_type=media_type,
                    headers=STREAM_HEADERS | {"Accept-Ranges": "none"})


async def _ensure_slot(user: User | None) -> None:
    """A connection slot for one more stream, with one retry for zap overlap.

    See MAXCONN_RETRY_DELAY: without the wait, a user at max_connections
    429s whenever the box opens the new channel before our watchdog has
    noticed the old socket is gone.
    """
    uname = user.name if user else None
    max_conn = user.max_connections if user else None
    if MANAGER.can_open_for(uname, max_conn):
        return
    await asyncio.sleep(MAXCONN_RETRY_DELAY)
    if MANAGER.can_open_for(uname, max_conn):
        return
    await db_log("WARNING", "output",
                 f"user {uname or 'admin'} exceeded max_connections")
    raise HTTPException(429, "max connections reached for this user")


async def _stream_response(kind: str, ref_id: int, user: User | None, label: str,
                            request: Request, mode: str = "",
                            media_type: str = "video/mp2t"):
    """
    Serve a stream: either proxied through ffmpeg (default) or redirected
    straight to the resolved portal URL.

    Redirect skips ffmpeg entirely - instant start, no CPU, works for panels
    whose stream ffmpeg refuses to remux - at the cost of mid-stream fallback
    and transport-stream rewriting. It is chosen per channel by assigning the
    "Redirect (bypass ffmpeg)" template (or per URL with `?mode=redirect`).
    Local files never 302 (the client cannot see our filesystem). Direct/copy
    templates serve the original file; transcode templates still go through
    ffmpeg.
    """
    started = time.perf_counter()
    if kind != "local" and await _wants_redirect(
            kind, ref_id, mode, user.name if user else None):
        from fastapi.responses import RedirectResponse
        resolve_started = time.perf_counter()
        info: dict = {}
        url, item_name = await MANAGER.resolve(kind, ref_id,
                                               requester=user.name if user else None,
                                               out=info)
        resolve_ms = (time.perf_counter() - resolve_started) * 1000
        if url:
            total_ms = (time.perf_counter() - started) * 1000
            await db_log("INFO", "output",
                         f"[{item_name}] startup timing: redirect resolve={resolve_ms:.0f}ms "
                         f"total={total_ms:.0f}ms")
            return RedirectResponse(url, status_code=302, headers={
                "Server-Timing": f"resolve;dur={resolve_ms:.1f}, total;dur={total_ms:.1f}",
            })
        if info.get("busy"):
            # Every candidate was refused by occupancy (our pipe/lease, or the
            # panel's own connection slot), not by a dead source. That is a
            # "retry in a second", and players do retry a 503 - a 502 "no
            # source produced a link" reads as "this channel is broken".
            raise HTTPException(503, f"{label}: every MAC is busy right now",
                                headers={"Retry-After": "2"})
        raise HTTPException(502, f"{label}: no source produced a link to redirect to")

    await _ensure_slot(user)
    open_started = time.perf_counter()
    handle, gen = await MANAGER.open(kind, ref_id, user.name if user else None,
                                     force_proxy=(mode == "proxy"))
    open_ms = (time.perf_counter() - open_started) * 1000
    if handle.dead:
        if handle.busy:
            # Same reasoning as the redirect path above, and the one answer that
            # used to be wrong: a 404 in 13 ms for "my own previous stream still
            # holds the MAC" tells an Enigma2 box the channel does not exist.
            raise HTTPException(503, f"{label}: every MAC is busy right now",
                                headers={"Retry-After": "2"})
        raise HTTPException(404, f"{label}: no available source (all busy or unreachable)")
    # watchdog lives until the stream deregisters (normal end) or the client
    # disappears (then it kills the stream; see watch_disconnect). watch() keeps
    # a strong reference, so the task cannot be garbage-collected mid-flight.
    MANAGER.watch(request, handle)
    first_started = time.perf_counter()
    body = await _guarded(gen, label, handle.item_name, handle=handle)
    first_ms = (time.perf_counter() - first_started) * 1000
    total_ms = (time.perf_counter() - started) * 1000
    await db_log("INFO", "output",
                 f"[{handle.item_name}] startup timing: prepare={open_ms:.0f}ms "
                 f"source+ffmpeg+first-byte={first_ms:.0f}ms total={total_ms:.0f}ms")
    timing = (f"prepare;dur={open_ms:.1f}, first-byte;dur={first_ms:.1f}, "
              f"total;dur={total_ms:.1f}")
    return StreamingResponse(body, media_type=media_type,
                             headers=STREAM_HEADERS | {"X-SPM-Stream": handle.id,
                                                       "Server-Timing": timing})


@router.api_route("/play/live/{pid}.ts", methods=["GET", "HEAD"])
async def play_live(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "stream")
    if request.method == "HEAD":
        return _stream_head()
    return await _stream_response("live", pid, user, f"live #{pid}", request, mode)


@router.api_route("/play/vod/{pid}.ts", methods=["GET", "HEAD"])
async def play_vod(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "stream")
    if request.method == "HEAD":
        return _stream_head()
    return await _stream_response("vod", pid, user, f"vod #{pid}", request, mode)


@router.api_route("/play/episode/{eid}.ts", methods=["GET", "HEAD"])
async def play_episode(request: Request, eid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "stream")
    if request.method == "HEAD":
        return _stream_head()
    return await _stream_response("episode", eid, user, f"episode #{eid}", request, mode)


# ---- Matroska aliases ------------------------------------------------------
# Same items, same templates, same fallback chain - only the announced
# container differs. Point these at an item whose template muxes `matroska`
# (the two built-in "Enigma2 VOD" presets) and the copied SRT/ASS/PGS tracks
# arrive intact: MPEG-TS has no slot for text subtitles, Matroska has.
@router.api_route("/play/live/{pid}.mkv", methods=["GET", "HEAD"])
async def play_live_mkv(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "stream")
    if request.method == "HEAD":
        return _stream_head(MEDIA_TYPES["mkv"])
    return await _stream_response("live", pid, user, f"live #{pid}", request, mode,
                                  MEDIA_TYPES["mkv"])


@router.api_route("/play/vod/{pid}.mkv", methods=["GET", "HEAD"])
async def play_vod_mkv(request: Request, pid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "stream")
    if request.method == "HEAD":
        return _stream_head(MEDIA_TYPES["mkv"])
    return await _stream_response("vod", pid, user, f"vod #{pid}", request, mode,
                                  MEDIA_TYPES["mkv"])


@router.api_route("/play/episode/{eid}.mkv", methods=["GET", "HEAD"])
async def play_episode_mkv(request: Request, eid: int, u: str = "", p: str = "", mode: str = ""):
    user = await _authed(u, p, "stream")
    if request.method == "HEAD":
        return _stream_head(MEDIA_TYPES["mkv"])
    return await _stream_response("episode", eid, user, f"episode #{eid}", request, mode,
                                  MEDIA_TYPES["mkv"])


@router.api_route("/play/local/{pid}.{ext}", methods=["GET", "HEAD"])
async def play_local(request: Request, pid: int, ext: str, u: str = "", p: str = "",
                     mode: str = ""):  # noqa: ARG001
    user = await _authed(u, p, "stream")
    # _local_response handles HEAD without opening FFmpeg while preserving the
    # real file's Content-Length and Range metadata for direct playback.
    return await _local_response(pid, user, request, ext)


def _requested_matches_file(path: str, ext: str | None) -> bool:
    """True when the URL alias is the file's own suffix (or none was given).

    A `.ts` / `.mkv` URL on an `.mp4` file is how Enigma2 bouquets ask for a
    stream container. Serving the original bytes under the wrong extension is
    the audio-only / black-picture failure on the box.
    """
    if not ext:
        return True
    want = ext.lower().lstrip(".")
    have = os.path.splitext(path)[1].lower().lstrip(".")
    return want == have


async def _local_response(pid: int, user, request: Request, ext: str | None = None):
    """Serve a local playlist item: original file for direct/copy, else ffmpeg."""
    started = time.perf_counter()
    path_started = time.perf_counter()
    path, item_name = await MANAGER.local_disk_path(pid)
    path_ms = (time.perf_counter() - path_started) * 1000
    if not path:
        raise HTTPException(404, f"local #{pid}: file not found on disk")
    template_started = time.perf_counter()
    serves_original = await MANAGER.local_serves_original(
        pid, user.name if user else None)
    template_ms = (time.perf_counter() - template_started) * 1000
    if serves_original and _requested_matches_file(path, ext):
        await _ensure_slot(user)
        media_type = media_type_for(path)
        # Do not let nginx-compatible reverse proxies fill a large response
        # buffer before VLC receives the first bytes. FileResponse supplies
        # Content-Length, Accept-Ranges and efficient asynchronous file reads.
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        if request.method == "HEAD":
            return FileResponse(path, media_type=media_type, headers=headers)
        register_started = time.perf_counter()
        handle = await MANAGER.register_local_file(
            pid, user.name if user else None, path, item_name)
        register_ms = (time.perf_counter() - register_started) * 1000
        MANAGER.watch(request, handle)
        total_ms = (time.perf_counter() - started) * 1000
        headers["X-SPM-Stream"] = handle.id
        headers["Server-Timing"] = (
            f"path;dur={path_ms:.1f}, template;dur={template_ms:.1f}, "
            f"register;dur={register_ms:.1f}, total;dur={total_ms:.1f}")
        await db_log("INFO", "output",
                     f"[{item_name}] startup timing: direct-local path={path_ms:.0f}ms "
                     f"template={template_ms:.0f}ms register={register_ms:.0f}ms "
                     f"total={total_ms:.0f}ms")
        return FileResponse(path, media_type=media_type, headers=headers,
                            background=BackgroundTask(MANAGER.kill, handle.id))
    # The URL alias follows the selected template. In particular, an Enigma2
    # Matroska remux must be announced as video/x-matroska rather than TS.
    media_type = MEDIA_TYPES["mkv"] if (ext or "").lower().lstrip(".") == "mkv" \
        else MEDIA_TYPES["ts"]
    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type,
                        headers={"Cache-Control": "no-store"})
    return await _stream_response("local", pid, user, f"local #{pid}", request,
                                  "proxy", media_type)


# Xtream-style stream URLs -----------------------------------------------
async def _xtream_stream(request: Request, kind: str, sid: int, u: str, p: str,
                         mode: str, container: str = "ts"):
    """Xtream namespace is gated by xtream_enabled, independent of M3U access."""
    user = await _authed(u, p, "xtream")
    media_type = MEDIA_TYPES.get(container, MEDIA_TYPES["ts"])
    if request.method == "HEAD":
        return _stream_head(media_type)
    return await _stream_response(kind, sid, user, f"{kind} #{sid}", request,
                                  mode, media_type)


@router.api_route("/live/{u}/{p}/{sid}.ts", methods=["GET", "HEAD"])
async def xlive(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await _xtream_stream(request, "live", sid, u, p, mode)


async def _xtream_movie(request: Request, sid: int, u: str, p: str,
                        mode: str, ext: str):
    user = await _authed(u, p, "xtream")
    local_id = local_id_from_xtream(sid)
    if local_id is not None:
        return await _local_response(local_id, user, request, ext)
    media_type = MEDIA_TYPES.get(ext, MEDIA_TYPES["ts"])
    if request.method == "HEAD":
        return _stream_head(media_type)
    return await _stream_response("vod", sid, user, f"vod #{sid}", request,
                                  mode, media_type)


@router.api_route("/movie/{u}/{p}/{sid}.ts", methods=["GET", "HEAD"])
async def xmovie(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await _xtream_movie(request, sid, u, p, mode, "ts")


@router.api_route("/series/{u}/{p}/{sid}.ts", methods=["GET", "HEAD"])
async def xseries(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await _xtream_stream(request, "episode", sid, u, p, mode)


@router.api_route("/movie/{u}/{p}/{sid}.mkv", methods=["GET", "HEAD"])
async def xmovie_mkv(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await _xtream_movie(request, sid, u, p, mode, "mkv")


@router.api_route("/movie/{u}/{p}/{sid}.{ext}", methods=["GET", "HEAD"])
async def xmovie_file(request: Request, sid: int, ext: str, u: str, p: str,
                      mode: str = ""):
    """Original local-file extensions (mp4/avi/webm/...) advertised as VOD."""
    if local_id_from_xtream(sid) is None:
        raise HTTPException(404, "unknown movie extension")
    return await _xtream_movie(request, sid, u, p, mode, ext)


@router.api_route("/series/{u}/{p}/{sid}.mkv", methods=["GET", "HEAD"])
async def xseries_mkv(request: Request, sid: int, u: str, p: str, mode: str = ""):
    return await _xtream_stream(request, "episode", sid, u, p, mode, "mkv")


# -------------------------------------------------- admin quick-play (GUI)
@router.get("/preview-play/{kind}/{pid}.ts")
async def admin_play(kind: str, pid: int, request: Request, mode: str = "", db=Depends(get_db)):
    """Play a PLAYLIST item from the GUI with the admin session (no user creds),
    through the *real* pipeline - template, fallback chain, MAC tracking."""
    from ..security import require_admin
    require_admin(request)
    if kind == "series":
        # The Playlist detail dialog passes a series playlist ID, not an
        # episode ID. Pick its first enabled linked season's playable episode.
        from ..services.item_info import playlist_primary_input
        _, _, _, episode = await playlist_primary_input(db, "series", pid)
        if episode is None:
            raise HTTPException(404, "no playable episode in the enabled seasons")
        kind, pid = "episode", episode.id
    if kind not in ("live", "vod", "episode", "local"):
        raise HTTPException(400, "kind must be live|vod|series|episode|local")
    # Browser MSE needs same-origin bytes, not a redirect to a provider/CDN or
    # a raw local MP4 mislabeled as .ts. Keep public player delivery unchanged.
    return await _stream_response(kind, pid, None, f"{kind} #{pid}", request, "proxy")


# ---------------------------------------------------------------- preview
@router.get("/preview/{kind}/{sid}.ts")
async def preview(kind: str, sid: int, request: Request, db=Depends(get_db),
                  tpl: int | None = Query(None)):
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
    handle, gen = await MANAGER._open_preview(src, portal, list(macs), kind=link_kind,
                                              name=name, template_id=tpl)
    if handle.dead:
        raise HTTPException(404, "preview failed: no data from source")
    MANAGER.watch(request, handle)
    body = await _guarded(gen, f"preview {kind} #{sid}", name)
    return StreamingResponse(body, media_type="video/mp2t",
                             headers=STREAM_HEADERS)
