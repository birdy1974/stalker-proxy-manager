"""
Is this MAC available right now, or is somebody else already using it?

The question an operator asks after a black channel. There are two independent
answers and the app has to keep them apart:

  1. **Our view** (free). `StreamManager.mac_occupancy` knows the two things we
     hold: an ffmpeg pipe (a stream of ours, right now) and a post-302 redirect
     lease (the player is on the panel's CDN, so "probably still watching", with
     N seconds left on a time-bounded guess). This costs nothing and contacts
     nobody - `GET /api/portals` carries it for every MAC, and the Portals table
     shows it next to the MAC.

  2. **The panel's view** (this module). A Stalker panel has no "who is using
     this MAC?" endpoint: the *only* moment it tells you is when you ask it for
     a link, and then only as a refusal code -

        `limit` / `account_is_in_use`   the MAC already has a stream open
        `access_denied`                 the MAC is not enrolled any more
        a link that then sends nothing  the slot is held mid-flight (the ~5-7s
                                        deliberation documented in the README)

     So the test is exactly what a play does: handshake, `create_link` for one
     real channel, then read the first bytes of the answer. It is
     operator-triggered on purpose - a probe costs a portal request and, on
     panels that count the media request, a connection slot for a few seconds.

The probe never occupies the MAC in our own bookkeeping: it holds no lock and
leaves no lease, so a probe can not make the MAC look busy to the next play.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
from sqlalchemy import select

from ..database import SessionLocal
from ..models import LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal
from ..portal.client import PortalError
from ..portal.identity import normalize_mac
from ..portal.links import plan_for
from ..portal.pool import POOL, PortalSession
from ..portal.resolver import resolve_portal
from . import stream_identity
from .channel_translations import attach_overrides
from .db_logging import db_log
from .stream_manager import MANAGER

log = logging.getLogger("spm.mac_probe")

#: How long the media request may take to deliver its first bytes. A live TS
#: origin normally starts in well under a second; anything that needs longer is
#: exactly the "panel accepted the link and sends nothing" case worth reporting.
READ_TIMEOUT = float(os.environ.get("SPM_MAC_PROBE_READ_TIMEOUT", "10"))
#: Bytes to read before hanging up. One chunk proves the panel streams to this
#: MAC; more would only cost the slot time.
READ_BYTES = 32 * 1024


def _referer_of(url: str) -> str:
    scheme, _, rest = str(url).partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}/"


def _media_client(**kwargs) -> httpx.AsyncClient:
    """The client used for the media read - a seam for the tests, exactly like
    ``outbound_client`` is for the portal client (the mock portal runs in a test
    transport, not on a socket)."""
    return httpx.AsyncClient(**kwargs)


async def _first_bytes(url: str, *, timeout: float = READ_TIMEOUT,
                       uas: list[str] | None = None) -> dict:
    """Pull the first chunk of `url` as a player would; report what happened.

    Walks the same media-UA ladder the stream path uses (learned identity first,
    then the MAG player UA, then - only on a policy 4xx - the portal browser
    UA), because "no bytes with the wrong identity" is not an answer about the
    MAC's availability.
    """
    verdict = {"bytes": 0, "ua": "", "status": None, "detail": "no attempt",
               "elapsed_s": 0.0}
    for ua in (uas or stream_identity.ladder(url)):
        started = time.monotonic()
        headers = {"User-Agent": ua, "Referer": _referer_of(url)}
        try:
            async with _media_client(timeout=timeout, follow_redirects=True,
                                     headers=headers) as client:
                async with client.stream("GET", url) as resp:
                    verdict["status"] = resp.status_code
                    if resp.status_code >= 400:
                        verdict.update(ua=ua, bytes=0,
                                       detail=f"HTTP {resp.status_code}",
                                       elapsed_s=round(time.monotonic() - started, 2))
                        if resp.status_code in stream_identity.UA_POLICY_4XX:
                            continue                      # identity-shaped refusal
                        return verdict
                    async with asyncio.timeout(timeout):
                        async for chunk in resp.aiter_bytes():
                            if chunk:
                                verdict.update(ua=ua, bytes=len(chunk), detail="ok",
                                               elapsed_s=round(time.monotonic() - started, 2))
                                stream_identity.remember(url, ua)
                                return verdict
                    verdict.update(ua=ua, bytes=0, detail="stream closed with no bytes",
                                   elapsed_s=round(time.monotonic() - started, 2))
                    return verdict
        except asyncio.TimeoutError:
            verdict.update(ua=ua, bytes=0,
                           detail=f"no bytes within {timeout:.0f}s",
                           elapsed_s=round(time.monotonic() - started, 2))
            return verdict
        except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
            verdict.update(ua=ua, bytes=0, detail=f"{type(exc).__name__}: {exc}",
                           elapsed_s=round(time.monotonic() - started, 2))
            return verdict
    return verdict


async def _pick_channel(portal_id: int, ref_id: int | None):
    """The channel to ask the panel about: a chosen playlist item, else a live
    source of this portal (the probe only needs *a* real cmd - the answer is
    about the MAC, not the channel).

    An enabled source wins, but a fetched-but-not-enabled one is good enough:
    the operator pressed Test, and a channel that is merely switched off in the
    catalogue is still a perfectly valid vehicle for the question. Requiring an
    enabled one would answer "no source to ask about" on a portal that has
    channels sitting right there.
    """
    async with SessionLocal() as s:
        if ref_id:
            item = await s.get(LivePlaylist, int(ref_id))
            rows = (await s.execute(
                select(LivePlaylistSource)
                .where(LivePlaylistSource.live_playlist_id == int(ref_id))
                .order_by(LivePlaylistSource.priority))).scalars().all()
            for r in rows:
                src = await s.get(LiveSource, r.live_source_id)
                if src and src.cmd and src.portal_id == portal_id and src.enabled:
                    return src, (item.custom_name if item else "")
        src = (await s.execute(
            select(LiveSource).where(LiveSource.portal_id == portal_id,
                                     LiveSource.enabled.is_(True))
            .order_by(LiveSource.id).limit(1))).scalars().first()
        if src is None:
            src = (await s.execute(
                select(LiveSource).where(LiveSource.portal_id == portal_id)
                .order_by(LiveSource.id).limit(1))).scalars().first()
        return src, (src.original_name if src else "")


async def probe_mac(portal_id: int, mac_id: int, *, ref_id: int | None = None,
                    timeout: float = READ_TIMEOUT, force: bool = False) -> dict:
    """Test one MAC against its panel. Returns a report, never raises.

    The report is shaped for the GUI *and* for the log: `available` is the
    yes/no, `reason` one of ``free`` / ``busy-ours`` / ``in-use`` / ``unusable``
    / ``no-data`` / ``error``, and `detail` a sentence an operator can act on.
    """
    started = time.monotonic()
    async with SessionLocal() as s:
        portal = await s.get(Portal, portal_id)
        mac = await s.get(MacAddress, mac_id)
        if not portal or not mac or mac.portal_id != portal_id:
            return {"ok": False, "available": False, "reason": "error",
                    "detail": "portal or MAC not found"}
        mac_str = normalize_mac(mac.mac)
        portal_name = portal.name
        base_url = portal.base_url
        proxy_url = portal.proxy_url
        tls_insecure = bool(portal.tls_insecure)
        resolved = portal.resolved_url

    # Our own view first: it is free, and "we are streaming through this MAC
    # right now" answers the question without touching the panel at all.
    ours = MANAGER.mac_occupancy(mac_id) or {"busy": False, "reason": "free"}
    if ours.get("busy") and not force:
        holder = ours.get("holder") or "unknown user"
        left = float(ours.get("remaining_s") or 0)
        mode = ("this proxy is streaming through it right now (ffmpeg)"
                if ours.get("reason") == "pipe"
                else f"this proxy handed the player to the panel CDN "
                     f"{left:.0f}s ago and cannot see when it stopped")
        return {"ok": True, "available": False, "reason": "busy-ours",
                "mac": mac_str, "portal": portal_name, "source": None,
                "ours": ours, "holder": ours.get("holder"),
                "detail": f"busy in this proxy: {mode}"
                          + (f" (used by {holder})" if holder != "unknown user" else "")}

    row, item_name = await _pick_channel(portal_id, ref_id)
    if row is None:
        return {"ok": False, "available": False, "reason": "error",
                "mac": mac_str, "portal": portal_name, "source": None,
                "detail": "this portal has no live channel to ask the panel about "
                          "- fetch its sources first, or pass ref_id"}

    url = resolved
    if not url:
        res = await resolve_portal(base_url, mac=mac.mac, proxy=proxy_url,
                                   tls_insecure=tls_insecure)
        if not res.ok:
            return {"ok": False, "available": False, "reason": "error",
                    "mac": mac_str, "portal": portal_name, "source": item_name,
                    "detail": f"portal URL could not be resolved: {res.error}"}
        url = res.portal_url
        async with SessionLocal() as s:
            p = await s.get(Portal, portal_id)
            if p and not p.resolved_url:
                p.resolved_url, p.resolved_path = res.portal_url, res.path
                await s.commit()

    # A saved per-MAC translation (if any) must ride along: this MAC numbers
    # the channel differently, and asking with the fetch MAC's cmd would
    # measure the wrong account. `row` may be detached — own session inside.
    await attach_overrides([(row, portal, [mac])])

    # Always ask (ffmpeg=True forces the "ask the panel" policy): a stored link
    # would be handed to nobody and the refusal code IS the answer we came for.
    plan = plan_for(row, mac, ffmpeg=True)
    link = ""
    code = ""
    try:
        client = await POOL.get(PortalSession.from_rows(portal, mac, portal_url=url))
        try:
            await client.ensure_auth()
            link = await client.create_link(plan.cmd, "live", **plan.request_kwargs())
        finally:
            await client.close()
    except PortalError as exc:
        code = exc.code or ""
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "available": False, "reason": "error",
                "mac": mac_str, "portal": portal_name, "source": item_name,
                "detail": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.monotonic() - started, 2)}

    if not link:
        unusable = code in ("access_denied", "unauthorized", "not_authorized",
                            "no_token", "http_401", "http_403", "blocked",
                            "token", "invalid_token")
        in_use = code in ("limit", "account_is_in_use", "max_connections")
        reason = "unusable" if unusable else ("in-use" if in_use else "no-link")
        detail = {
            "unusable": "the panel refused the MAC itself (not enrolled / "
                        "credentials) - fix it in Portals, it will never play",
            "in-use": "the panel says this MAC already has a stream open "
                      "(limit / account is in use) - another device is watching "
                      "on it, or the panel has not timed out its last stream yet",
            "no-link": f"the panel refused to build a link ({code or 'no url'}) "
                       "- try another channel to tell a panel problem from a "
                       "channel problem",
        }[reason]
        await db_log("WARNING" if reason != "unusable" else "ERROR", "portal",
                     f"[{portal_name}] MAC probe {mac_str}: {reason.upper()} "
                     f"({code or 'no url'}, {item_name or 'no channel'})")
        return {"ok": True, "available": False, "reason": reason, "code": code or None,
                "mac": mac_str, "portal": portal_name, "source": item_name,
                "ours": ours, "detail": detail,
                "elapsed_s": round(time.monotonic() - started, 2)}

    media = await _first_bytes(link, timeout=timeout)
    elapsed = round(time.monotonic() - started, 2)
    if media["bytes"]:
        detail = (f"available: the panel built a link and sent {media['bytes']} "
                  f"bytes in {media['elapsed_s']}s (identity {media['ua']}) - "
                  f"nobody else is holding this MAC")
        await db_log("INFO", "portal",
                     f"[{portal_name}] MAC probe {mac_str}: AVAILABLE "
                     f"({media['bytes']} bytes in {media['elapsed_s']}s, "
                     f"{item_name or 'no channel'})")
        return {"ok": True, "available": True, "reason": "free", "code": None,
                "mac": mac_str, "portal": portal_name, "source": item_name,
                "ours": ours, "media": media, "detail": detail, "elapsed_s": elapsed}

    detail = (f"the panel built a link but sent no data ({media['detail']}) - the "
              "MAC's single connection slot is probably still held (a zap, another "
              "device, or a stream the panel has not timed out): wait a moment, "
              "or use another MAC")
    await db_log("WARNING", "portal",
                 f"[{portal_name}] MAC probe {mac_str}: NO DATA ({media['detail']}, "
                 f"{item_name or 'no channel'})")
    return {"ok": True, "available": False, "reason": "no-data", "code": None,
            "mac": mac_str, "portal": portal_name, "source": item_name,
            "ours": ours, "media": media, "detail": detail, "elapsed_s": elapsed}


async def probe_portal(portal_id: int, *, only_free: bool = False) -> dict:
    """Probe every MAC of a portal (the GUI button). Sequential on purpose: a
    panel that rate-limits does not enjoy four concurrent slot tests."""
    async with SessionLocal() as s:
        portal = await s.get(Portal, portal_id)
        if not portal:
            return {"ok": False, "error": "portal not found"}
        macs = list((await s.execute(select(MacAddress)
                                     .where(MacAddress.portal_id == portal_id)
                                     .order_by(MacAddress.order))).scalars().all())
    results = []
    for m in macs:
        if only_free and (MANAGER.mac_occupancy(m.id) or {}).get("busy"):
            results.append({"ok": True, "available": False, "reason": "busy-ours",
                            "mac": m.mac, "portal": portal.name,
                            "detail": "skipped: this proxy is using the MAC"})
            continue
        results.append(await probe_mac(portal_id, m.id))
    return {"ok": True, "portal_id": portal_id, "name": portal.name,
            "macs": results,
            "available": sum(1 for r in results if r.get("available")),
            "tested": len(results)}
