"""
Shared assembly for the detail popups (round-3 GUI): resolve a source cmd to a
playable URL (through the portal's create_link, like the real pipeline does),
probe it with the bundled ffmpeg, and look up TMDB metadata when an API key is
configured.
"""

from __future__ import annotations

import os

from sqlalchemy import select

from ..models import (
    LivePlaylist, LivePlaylistSource, LiveSource, LocalFile, LocalPlaylist,
    LocalSource, MacAddress, Portal, SerieEpisode, SeriePlaylist,
    SeriePlaylistSeason, SerieSeason, SerieSource, VodPlaylist,
    VodPlaylistSource, VodSource,
)
from ..portal.client import apply_mac_placeholder, extract_url
from ..portal.links import link_policy
from ..portal.pool import POOL, PortalSession
from .probe import probe_media
from .tmdb import tmdb_lookup

PLAYLIST_KINDS = ("live", "vod", "series", "local")


def cmd_to_url(cmd: str) -> str | None:
    """cmds look like 'ffmpeg http://host/file.ts -opts' -> keep the URL part.

    Delegates to the portal client's `extract_url`: this used to be a second,
    weaker copy (it missed percent-encoded cmds and every scheme but http), and a
    popup that parses the cmd differently from the stream path reports a URL that
    nobody will ever play.
    """
    return extract_url(cmd) or None


async def playable_url(db, cmd: str, portal_id: int, kind: str, *, src=None) -> str | None:
    """The URL this item would really be played with - same rules as the stream path.

    `src` is the source row the popup is showing. Passing it matters: its stored
    link flags are what decide whether the portal has to be asked at all (R2), so
    without it the popup asks on every open and can report a URL the player never
    sees. A caller without a row gets today's behaviour (unknown flags = ask).
    """
    portal = await db.get(Portal, portal_id) if portal_id else None
    if portal and portal.enabled and portal.resolved_url:
        mac = (await db.execute(select(MacAddress).where(
            MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().first()
        if mac:
            flags = getattr(src, "link_flags", None) if src is not None else None
            force = bool(getattr(mac, "force_ch_link_check", False))
            # Classic-Stalker episode: cmd addresses the SEASON; only create_link
            # with `series=<n>` yields this episode's URL, so direct play is out.
            series = None
            if src is not None and bool(getattr(src, "series_param", False)):
                ep_num = getattr(src, "episode_number", None)
                if ep_num is not None:
                    series = int(ep_num)
            stored = cmd_to_url(cmd) or ""
            policy = link_policy(url=stored, link_flags=flags, force_ch_link_check=force,
                                 allow_direct=bool(getattr(portal, "direct_links", True))
                                 and series is None)
            if policy.direct:
                return apply_mac_placeholder(stored, mac.mac)
            # Pooled: do NOT handshake here. create_link() authenticates lazily
            # and reuses the cached token, so a detail popup costs no extra
            # portal round trip once a session already exists.
            client = await POOL.get(PortalSession.from_rows(portal, mac,
                                                             portal_url=portal.resolved_url))
            try:
                return await client.create_link(cmd, kind, link_flags=flags,
                                                force_ch_link_check=force, series=series)
            except Exception:  # noqa: BLE001 - fall through to raw cmd
                pass
            finally:
                await client.close()      # no-op for a pooled client
    return cmd_to_url(cmd)


async def probe_target(target: str, *, is_url: bool) -> dict:
    return await probe_media(target, is_url=is_url)


async def enrich(name: str, year: str | None, kind: str) -> dict | None:
    """kind: 'vod' | 'series' (live/local have no meaningful TMDB entry)."""
    if kind not in ("vod", "series"):
        return None
    clean = name
    y = None
    if isinstance(year, str) and year[:4].isdigit():
        y = year[:4]
    elif isinstance(year, int):
        y = str(year)
    return await tmdb_lookup(clean, y, kind)


def probe_is_url(kind: str) -> bool:
    return kind != "local"


def local_file_path(directory: str, rel: str | None) -> str:
    from ..config import MEDIA_ROOT
    if os.path.isabs(directory):
        return os.path.join(directory, rel or "")
    return str(MEDIA_ROOT / directory / (rel or ""))


async def playlist_primary_input(db, kind: str, pid: int):
    """Return (cmd_or_path, portal_id, is_url, source_row) for a playlist item's primary source.

    Same resolution the Playlist detail popup and the ffmpeg demo use, so a
    template test against a channel sees the URL the stream path would play.
    """
    if kind == "live":
        link = (await db.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == pid)
            .order_by(LivePlaylistSource.priority))).scalars().first()
        src = await db.get(LiveSource, link.live_source_id) if link else None
        return (src.cmd if src else None), (src.portal_id if src else None), True, src
    if kind == "vod":
        link = (await db.execute(select(VodPlaylistSource).where(
            VodPlaylistSource.vod_playlist_id == pid)
            .order_by(VodPlaylistSource.priority))).scalars().first()
        src = await db.get(VodSource, link.vod_source_id) if link else None
        if not src:
            pl = await db.get(VodPlaylist, pid)
            src = await db.get(VodSource, pl.vod_source_id) if pl else None
        return (src.cmd if src else None), (src.portal_id if src else None), True, src
    if kind == "series":
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
            return None, None, True, None
        season = await db.get(SerieSeason, eps[0].serie_season_id)
        ssrc = await db.get(SerieSource, season.serie_source_id) if season else None
        return eps[0].cmd, (ssrc.portal_id if ssrc else None), True, eps[0]
    if kind == "local":
        r = await db.get(LocalPlaylist, pid)
        lf = await db.get(LocalFile, r.local_file_id) if r else None
        ls = await db.get(LocalSource, lf.local_source_id) if lf else None
        path = local_file_path(ls.directory, lf.relative_path) if ls and lf else None
        return path, None, False, None
    return None, None, True, None


async def resolve_playlist_input(db, kind: str, pid: int) -> dict:
    """Playable URL/path + labels for one enabled playlist item.

    Raises ValueError with a GUI-safe message when the item cannot be tested.
    """
    if kind not in PLAYLIST_KINDS:
        raise ValueError("kind must be live|vod|series|local")
    models = {"live": LivePlaylist, "vod": VodPlaylist, "series": SeriePlaylist,
              "local": LocalPlaylist}
    row = await db.get(models[kind], pid)
    if not row:
        raise ValueError("playlist item not found")
    if not row.enabled:
        raise ValueError("playlist item is disabled")
    cmd, portal_id, is_url, src = await playlist_primary_input(db, kind, pid)
    name = getattr(row, "custom_name", None) or f"{kind} #{pid}"
    src_name = getattr(src, "original_name", None) or getattr(src, "name", None)
    if not cmd:
        raise ValueError(f"no usable source stream on '{name}'")
    if is_url:
        url = await playable_url(db, cmd, portal_id, kind, src=src)
        if not url:
            raise ValueError(f"could not resolve a playable URL for '{name}'")
        return {"kind": kind, "id": pid, "name": name, "url": url, "is_url": True,
                "source": src_name, "cmd": cmd}
    return {"kind": kind, "id": pid, "name": name, "url": cmd, "is_url": False,
            "source": src_name, "cmd": cmd}
