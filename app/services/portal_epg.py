"""Bounded portal guide discovery, using the portal's existing authenticated session.

Prefer one bulk get_epg_info request. Fall back to paced short-EPG requests only
for enabled playlist inputs (not the complete provider catalogue). Never open media.
"""

import asyncio
import uuid
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
from urllib.parse import quote

from sqlalchemy import select
from ..database import SessionLocal
from ..models import LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal
from ..portal.epg import parse_short_epg
from ..portal.pool import POOL, PortalSession
from .item_info import choose_mac
from .portal_pace import pace_for_playback
from .stream_manager import MANAGER

MAX_SHORT_CHANNELS = 100


async def portal_xml(portal_id: int, namespace: str) -> tuple[bytes, str]:
    async with SessionLocal() as db:
        portal = await db.get(Portal, portal_id)
        if not portal or not portal.enabled or not portal.resolved_url:
            raise ValueError("Portal is missing, disabled or not resolved")
        sources = (
            (
                await db.execute(
                    select(LiveSource)
                    .join(
                        LivePlaylistSource,
                        LivePlaylistSource.live_source_id == LiveSource.id,
                    )
                    .join(
                        LivePlaylist,
                        LivePlaylist.id == LivePlaylistSource.live_playlist_id,
                    )
                    .where(
                        LiveSource.portal_id == portal_id,
                        LivePlaylist.enabled.is_(True),
                    )
                    .order_by(LiveSource.id)
                )
            )
            .scalars()
            .unique()
            .all()
        )
        if not sources:
            raise ValueError("No enabled playlist channels use this portal")
        macs = (
            (
                await db.execute(
                    select(MacAddress)
                    .where(MacAddress.portal_id == portal_id)
                    .order_by(MacAddress.order)
                )
            )
            .scalars()
            .all()
        )
        mac = choose_mac(portal, macs, avoid_busy=True)
        if not mac:
            raise ValueError("Portal has no usable MAC")
        profile = PortalSession.from_rows(portal, mac)
    if MANAGER.is_mac_busy(mac.id):
        raise ValueError("Portal MAC became busy; retry the EPG check later")
    holder = "epg:" + uuid.uuid4().hex
    MANAGER.lease_mac(mac.id, holder=holder, seconds=100, item="Portal EPG check")
    try:
        return await asyncio.wait_for(_read(profile, namespace, sources,
                                           portal_id=portal_id), timeout=90)
    finally:
        if MANAGER.lease_holder(mac.id) == holder:
            MANAGER.redirect_leases.pop(mac.id, None)
            MANAGER.lease_meta.pop(mac.id, None)


async def _read(profile, namespace, sources, portal_id: int | None = None):
    client = await POOL.get(profile)
    deadline = time.monotonic() + 85
    try:
        tz = ZoneInfo(profile.timezone)
        try:
            bulk = await asyncio.wait_for(client.epg_info(), timeout=20)
        except Exception:
            bulk = {}
        if isinstance(bulk, dict) and "js" in bulk:
            bulk = bulk["js"]
        if isinstance(bulk, dict) and isinstance(bulk.get("data"), dict):
            bulk = bulk["data"]
        if not isinstance(bulk, dict):
            bulk = {}
        root = ET.Element("tv", {"generator-info-name": "portal EPG",
            "spm-raw-times": "1", "spm-timezone": profile.timezone})
        count, short_calls = 0, 0
        for src in sources:
            programmes = parse_short_epg(bulk.get(str(src.portal_channel_id), []), tz)
            if not programmes and short_calls < MAX_SHORT_CHANNELS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                short_calls += 1
                # The per-channel fallback is the EPG path that can walk a whole
                # playlist; while somebody watches this portal, it slows down.
                await pace_for_playback(portal_id)
                try:
                    programmes = await asyncio.wait_for(
                        client.short_epg(src.portal_channel_id, size=24, tz=tz),
                        timeout=min(15, remaining),
                    )
                except Exception:
                    # A refusal is not an invitation to hammer every channel.
                    if not count:
                        raise ValueError(
                            "Portal did not provide EPG via bulk or short-EPG APIs"
                        )
                    break
                await asyncio.sleep(0.15)
            if not programmes:
                continue
            tvg = f'portal.{namespace}.{quote(str(src.portal_channel_id), safe="")}'
            channel = ET.SubElement(root, "channel", {"id": tvg})
            ET.SubElement(channel, "display-name").text = src.original_name
            for index, p in enumerate(programmes):
                stop = p.stop
                stop_input = p.stop_input
                if not stop and index + 1 < len(programmes):
                    stop = programmes[index + 1].start
                    stop_input = programmes[index + 1].start_input
                if not p.start or not stop or stop <= p.start:
                    continue
                from .epg import _fmt_ts

                programme = ET.SubElement(
                    root,
                    "programme",
                    {"channel": tvg, "start": _fmt_ts(p.start), "stop": _fmt_ts(stop),
                     "spm-start": str(p.start_input) if p.start_input is not None else p.start.isoformat(),
                     "spm-stop": str(stop_input) if stop_input is not None else stop.isoformat()},
                )
                ET.SubElement(programme, "title").text = p.title
                ET.SubElement(programme, "desc").text = p.description
                count += 1
        if not count:
            raise ValueError(
                "No usable portal EPG programmes returned; coverage may be unavailable"
            )
        note = f"portal: {count} entries, {short_calls} short-guide requests"
        if short_calls >= MAX_SHORT_CHANNELS:
            note += " (request limit reached)"
        return ET.tostring(root, encoding="utf-8", xml_declaration=True), note
    finally:
        await client.close()
