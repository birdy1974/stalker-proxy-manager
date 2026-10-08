"""Catch-up (timeshift) for the Xtream output: redirect to the real source.

Nothing is recorded or relayed here. A request for a past programme becomes a
302 to a URL the upstream panel gives us, and the bytes come from the panel:

* Xtream-adopted portal: the channel's upstream URL (`LiveSource.xtream_url`,
  `.../live/<user>/<pass>/<id>.ts`) is rewritten to the panel's own timeshift
  URL `.../timeshift/<user>/<pass>/<dur>/<stamp>/<id>.ts`. This exposes the
  upstream credentials to the client; that was accepted on purpose.
* Stalker-only portal: the panel is asked, via `create_link`, for an archive
  link for the programme that covers the requested start, and the client is
  redirected to it.

Times in the URL are UTC (`yyyy-MM-dd:HH-mm`), the same convention OwnTV uses.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from ..database import SessionLocal
from ..models import LivePlaylistSource, LiveSource, MacAddress, Portal
from ..portal.account import mac_is_usable
from ..portal.client import PortalError
from ..portal.epg import parse_archive_day
from ..portal.pool import POOL, PortalSession
from .stream_manager import MANAGER

log = logging.getLogger(__name__)

#: how far back a catch-up request may reach
ARCHIVE_DAYS = 7
#: a programme start within this many minutes of the request still counts as a match
MATCH_TOLERANCE = timedelta(minutes=5)
#: safety cap on archive-day pages walked for one request
MAX_PAGES = 10

_START_FMT = "%Y-%m-%d:%H-%M"
_XTREAM_LIVE = re.compile(r"^(?P<base>https?://.+?)/live/(?P<u>[^/]+)/(?P<p>[^/]+)/[^/?]+\.ts(?:\?.*)?$")


class CatchupUnavailable(Exception):
    """Carries the HTTP status the route should answer with."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_start(text: str) -> datetime:
    """`yyyy-MM-dd:HH-mm` (UTC) → aware datetime. Raises CatchupUnavailable(400)."""
    try:
        return datetime.strptime(text, _START_FMT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise CatchupUnavailable(400, "start must be yyyy-MM-dd:HH-mm (UTC)")


def xtream_timeshift_url(xtream_url: str, duration_min: int, start: datetime) -> str | None:
    """Rewrite an upstream live URL to its timeshift form, or None if it is not one."""
    m = _XTREAM_LIVE.match(xtream_url or "")
    if not m:
        return None
    stamp = start.astimezone(timezone.utc).strftime(_START_FMT)
    return (f"{m['base']}/timeshift/{m['u']}/{m['p']}/{int(duration_min)}/{stamp}/"
            f"{_stream_id_of(xtream_url)}.ts")


def _stream_id_of(xtream_url: str) -> str:
    tail = xtream_url.split("?", 1)[0].rsplit("/", 1)[-1]
    return tail[:-3] if tail.endswith(".ts") else tail


def _zone(tz_name: str | None):
    try:
        return ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _archive_day_of(start: datetime, tz_name: str | None) -> str:
    return start.astimezone(_zone(tz_name)).strftime("%Y-%m-%d")


async def find_programme(client, channel_id: str, day: str, start: datetime,
                         tz_name: str | None) -> str | None:
    """Id of the archived programme covering `start`, or the nearest one within tolerance.

    Pages are in time order, so the walk stops as soon as a page has passed `start`.
    """
    ptz = _zone(tz_name)
    best: tuple[timedelta, str] | None = None
    for page in range(1, MAX_PAGES + 1):
        payload = await client.archive_day(channel_id, day, page)
        total, per_page, rows = parse_archive_day(payload, ptz)
        for row in rows:
            if row["start"] <= start < row["stop"] and row["archived"]:
                return row["id"]
            gap = abs(row["start"] - start)
            if row["archived"] and gap <= MATCH_TOLERANCE and (best is None or gap < best[0]):
                best = (gap, row["id"])
        if not rows or per_page <= 0 or page * per_page >= total:
            break
        if rows[-1]["start"] > start:
            break
    return best[1] if best else None


@dataclass
class _Candidate:
    source: LiveSource
    portal: Portal
    channel_id: str


async def _candidates(playlist_id: int) -> list[_Candidate]:
    """Enabled sources of a live channel, in priority order, that advertise archive."""
    out: list[_Candidate] = []
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(LivePlaylistSource).where(LivePlaylistSource.live_playlist_id == playlist_id)
            .order_by(LivePlaylistSource.priority))).scalars().all()
        for r in rows:
            src = await s.get(LiveSource, r.live_source_id)
            if not src or not src.enabled or not src.tv_archive:
                continue
            portal = await s.get(Portal, src.portal_id)
            if not portal or not portal.enabled:
                continue
            out.append(_Candidate(src, portal, str(src.portal_channel_id or "")))
    return out


async def archive_playlist_ids() -> set[int]:
    """Live playlist ids that have at least one archive-capable source (for the catalogue)."""
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(LivePlaylistSource.live_playlist_id, LiveSource.tv_archive, LiveSource.enabled,
                   Portal.enabled)
            .join(LiveSource, LiveSource.id == LivePlaylistSource.live_source_id)
            .join(Portal, Portal.id == LiveSource.portal_id))).all()
    return {pid for pid, archive, src_on, portal_on in rows if archive and src_on and portal_on}


async def _stalker_archive_url(c: _Candidate, start: datetime, user_name: str) -> str:
    portal = c.portal
    async with SessionLocal() as s:
        macs = (await s.execute(select(MacAddress).where(
            MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().all()
    usable = [m for m in macs if mac_is_usable(getattr(m, "status", None))]
    if not usable:
        raise CatchupUnavailable(404, "no usable MAC for this channel's portal")
    free = [m for m in usable if not MANAGER.is_mac_busy(m.id, requester=user_name)]
    if not free:
        raise CatchupUnavailable(503, "every MAC for this portal is busy")
    day = _archive_day_of(start, portal.stb_timezone)
    last_error = ""
    for mac in free:
        client = await POOL.get(PortalSession.from_rows(portal, mac))
        try:
            programme = await find_programme(client, c.channel_id, day, start, portal.stb_timezone)
            if programme is None:
                raise CatchupUnavailable(404, "no archived programme at that time")
            url = await client.create_link(f"auto /media/{programme}.mpg", kind="archive")
        except CatchupUnavailable:
            raise
        except PortalError as exc:
            last_error = str(exc)
            log.info("catch-up: mac %s on portal %s failed: %s", mac.mac, portal.id, exc)
            continue
        finally:
            await client.close()
        MANAGER.lease_mac(mac.id, holder=user_name, item=c.source.original_name, kind="archive")
        return url
    raise CatchupUnavailable(502, f"portal refused the archive link: {last_error or 'no answer'}")


async def resolve(playlist_id: int, start: datetime, duration_min: int,
                  user_name: str) -> str:
    """The URL to redirect a catch-up request to. Raises CatchupUnavailable."""
    now = datetime.now(timezone.utc)
    if start > now:
        raise CatchupUnavailable(404, "start is in the future")
    if start < now - timedelta(days=ARCHIVE_DAYS):
        raise CatchupUnavailable(404, "start is outside the archive window")
    cands = await _candidates(playlist_id)
    if not cands:
        raise CatchupUnavailable(404, "no archive for this channel")
    last: CatchupUnavailable | None = None
    for c in cands:
        if c.portal.xtream_adopted and c.source.xtream_url:
            url = xtream_timeshift_url(c.source.xtream_url, duration_min, start)
            if url:
                return url
        if not c.source.cmd:
            continue
        try:
            return await _stalker_archive_url(c, start, user_name)
        except CatchupUnavailable as exc:
            last = exc
    raise last or CatchupUnavailable(404, "no archive for this channel")
