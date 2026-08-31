"""
Shared assembly for the detail popups (round-3 GUI): resolve a source cmd to a
playable URL (through the portal's create_link, like the real pipeline does),
probe it with the bundled ffmpeg, and look up TMDB metadata when an API key is
configured.
"""

from __future__ import annotations

import os

from sqlalchemy import select

from ..models import MacAddress, Portal
from ..portal.client import apply_mac_placeholder, extract_url
from ..portal.links import link_policy
from ..portal.pool import POOL, PortalSession
from .probe import probe_media
from .tmdb import tmdb_lookup


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
