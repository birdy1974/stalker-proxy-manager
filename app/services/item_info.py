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
from ..portal.client import StalkerClient
from .probe import probe_media
from .tmdb import tmdb_lookup


def cmd_to_url(cmd: str) -> str | None:
    """cmds look like 'ffmpeg http://host/file.ts -opts' -> keep the URL part."""
    for tok in (cmd or "").split():
        if tok.startswith("http://") or tok.startswith("https://"):
            return tok
    return None


async def playable_url(db, cmd: str, portal_id: int, kind: str) -> str | None:
    """Resolve via the portal (create_link) when possible; fallback: the raw
    URL inside the cmd (mock portal / already-resolved sources without auth)."""
    portal = await db.get(Portal, portal_id) if portal_id else None
    if portal and portal.enabled and portal.resolved_url:
        mac = (await db.execute(select(MacAddress).where(
            MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().first()
        if mac:
            client = StalkerClient(portal.resolved_url, mac.mac, mac.password)
            try:
                await client.handshake()
                return await client.create_link(cmd, kind)
            except Exception:  # noqa: BLE001 - fall through to raw cmd
                pass
            finally:
                await client.close()
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
