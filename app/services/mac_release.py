"""
Let a MAC go: end everything this proxy holds on it, and give the panel a
reason to forget the session that may still be counting a connection.

Why this module exists
----------------------
A Stalker panel has no "close stream" call. It hands out one connection slot
per MAC and only ever talks about it sideways - as a refusal when you ask for
a link (`limit` / `account_is_in_use`), or as a link that never sends a byte
because the slot is still held from the last play. So a MAC that will not play
again can be stuck for two very different reasons, and an operator needs one
button that deals with both instead of a log dive:

  1. **We are holding it.** An ffmpeg pipe of ours (a real stream, visible on
     the dashboard), a redirect lease (we 302'd a player to the panel's CDN
     and cannot see when it stopped), or a *ghost lock* - bookkeeping left
     behind by a start that was cancelled before it produced data. All three
     are local, and all three are ended here.

  2. **The panel is still counting a connection.** Our pipe is gone but the
     panel has not timed the slot out yet - or somebody else's box really is
     watching on that MAC. What we can do about it is drop the pooled session
     for that MAC and hand-shake again: a panel binds one token per MAC, so a
     fresh handshake retires the previous session's token. Whether that freed
     the slot is not something we can assume - so we ask, with the same probe
     the Test button uses, and report the panel's own answer.

Nothing here is automatic. Releasing ends streams that people may be watching,
so it is a button and a confirmation, never a scheduler.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..database import SessionLocal
from ..models import MacAddress, Portal
from ..portal.pool import POOL, PortalSession
from ..portal.resolver import resolve_portal
from .db_logging import db_log
from .stream_manager import MANAGER

log = logging.getLogger("spm.mac_release")


def _portal_url(portal) -> str:
    return (getattr(portal, "resolved_url", None)
            or getattr(portal, "base_url", "") or "")


async def _ensure_resolved(portal) -> str:
    """The portal URL to talk to, resolving it once if nobody ever did."""
    url = _portal_url(portal)
    if url:
        return url
    res = await resolve_portal(portal.base_url, proxy=portal.proxy_url,
                               tls_insecure=bool(portal.tls_insecure))
    if not res.ok:
        return ""
    async with SessionLocal() as s:
        row = await s.get(Portal, portal.id)
        if row is not None and not row.resolved_url:
            row.resolved_url, row.resolved_path = res.portal_url, res.path
            await s.commit()
    return res.portal_url


async def _panel_step(portal, mac_row) -> dict:
    """Drop this MAC's pooled session and bring up a fresh one.

    Cheap enough to do for every release (one handshake) and it is the only
    lever we have on the panel's side: the new token retires the old session,
    and a pooled client that was mid-stream is closed instead of left holding
    a socket the panel still counts.
    """
    out = {"session_dropped": False, "rehandshaked": False, "error": ""}
    try:
        url = await _ensure_resolved(portal)
        if not url:
            out["error"] = "portal URL could not be resolved"
            return out
        sess = PortalSession.from_rows(portal, mac_row, portal_url=url)
        await POOL.drop(sess)
        out["session_dropped"] = True
        client = await POOL.get(sess)
        await client.ensure_auth()
        out["rehandshaked"] = True
    except Exception as exc:  # noqa: BLE001 - a release must never raise
        out["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("panel release step failed for %s/%s: %s",
                    getattr(portal, "name", "?"), getattr(mac_row, "mac", "?"), exc)
    return out


async def release_one(portal: Portal, mac_row: MacAddress, *,
                      verify: bool = False, panel: bool = True) -> dict:
    """Release one MAC. Never raises; the report is the answer either way."""
    mac_str = (mac_row.mac or "").upper()
    local = await MANAGER.free_mac(mac_row.id)
    report = {"mac_id": int(mac_row.id), "mac": mac_str,
              "portal_id": int(portal.id), "portal": portal.name,
              "local": local,
              "panel": {"session_dropped": False, "rehandshaked": False,
                        "error": "skipped"} if not panel else await _panel_step(portal, mac_row),
              "verify": None}
    if verify:
        from . import mac_probe
        try:
            report["verify"] = await mac_probe.probe_mac(
                int(portal.id), int(mac_row.id), force=True)
        except Exception as exc:  # noqa: BLE001
            report["verify"] = {"ok": False, "available": False,
                                "reason": "error", "detail": str(exc)}
    await db_log("WARNING", "portal", _summary(report))
    return report


def _summary(r: dict) -> str:
    local = r.get("local") or {}
    killed = local.get("killed") or []
    bits = [f"MAC release {r['mac']} ({r['portal']})"]
    if killed:
        bits.append(", ".join(f"{k['item']}"
                              + (f" ({k['user']})" if k.get("user") else "")
                              for k in killed[:4])
                     + (f" +{len(killed) - 4} more" if len(killed) > 4 else ""))
    else:
        bits.append("no pipe of ours")
    if local.get("lease_dropped"):
        bits.append("redirect lease dropped")
    if local.get("ghosts"):
        bits.append(f"{local['ghosts']} stale lock(s) cleared")
    if local.get("starting"):
        bits.append(f"{local['starting']} start(s) still opening, left alone")
    if not local.get("was_busy"):
        bits.append("was already free here")
    p = r.get("panel") or {}
    if p.get("error"):
        bits.append(f"panel: {p['error']}")
    elif p.get("rehandshaked"):
        bits.append("panel: fresh handshake")
    v = r.get("verify") or {}
    if v:
        bits.append(f"panel now says {'available' if v.get('available') else v.get('reason')}")
    return " - ".join(bits)


async def release_macs(pairs: list, *, verify: bool = False,
                       panel: bool = True) -> dict:
    """Release several (portal, mac) pairs, sequentially.

    Sequential on purpose: every release may handshake, and a panel that rate
    limits does not enjoy a burst of them.
    """
    results = []
    for portal, mac_row in pairs:
        try:
            results.append(await release_one(portal, mac_row, verify=verify,
                                             panel=panel))
        except Exception as exc:  # noqa: BLE001 - one MAC must not stop the rest
            results.append({"mac_id": int(getattr(mac_row, "id", 0) or 0),
                            "mac": (getattr(mac_row, "mac", "") or "").upper(),
                            "portal_id": int(getattr(portal, "id", 0) or 0),
                            "portal": getattr(portal, "name", ""),
                            "local": {"killed": [], "lease_dropped": False,
                                      "ghosts": 0, "was_busy": False},
                            "panel": {"error": f"{type(exc).__name__}: {exc}"},
                            "verify": None})
    return {"count": len(results),
            "released_streams": sum(len(r["local"]["killed"]) for r in results),
            "stale_locks": sum(r["local"]["ghosts"] for r in results),
            "results": results}


async def release_portal(portal_id: int, *, verify: bool = False,
                         panel: bool = True, only_busy: bool = False) -> dict:
    """Release every MAC of one portal."""
    async with SessionLocal() as s:
        portal = await s.get(Portal, int(portal_id))
        if portal is None:
            return {"ok": False, "error": "portal not found", "results": []}
        macs = list((await s.execute(
            select(MacAddress).where(MacAddress.portal_id == portal.id)
            .order_by(MacAddress.order))).scalars().all())
    pairs = [(portal, m) for m in macs
             if not only_busy or (MANAGER.mac_occupancy(m.id) or {}).get("busy")]
    out = await release_macs(pairs, verify=verify, panel=panel)
    out["ok"] = True
    out["skipped"] = len(macs) - len(pairs)
    return out


async def release_all(*, verify: bool = False, panel: bool = True,
                      only_busy: bool = False) -> dict:
    """Release every MAC of every portal (the 'release all' toolbar button)."""
    async with SessionLocal() as s:
        portals = list((await s.execute(
            select(Portal).order_by(Portal.name))).scalars().all())
        pairs = []
        total = 0
        for p in portals:
            macs = list((await s.execute(
                select(MacAddress).where(MacAddress.portal_id == p.id)
                .order_by(MacAddress.order))).scalars().all())
            total += len(macs)
            for m in macs:
                if only_busy and not (MANAGER.mac_occupancy(m.id) or {}).get("busy"):
                    continue
                pairs.append((p, m))
    out = await release_macs(pairs, verify=verify, panel=panel)
    out["ok"] = True
    out["portals"] = len(portals)
    out["skipped"] = total - len(pairs)
    return out


def occupancy_overview() -> dict:
    """What the release buttons would touch, without touching it.

    Used to word the confirmation ("this ends 2 streams on 3 MACs") and to
    make the two views agree: a MAC that is busy here with no dashboard row is
    a stale lock, and it is named as such instead of pretending to be a stream.
    """
    busy = {mid: info for mid, info in MANAGER.occupancy_map().items()}
    ghosts = MANAGER.ghost_lock_ids()
    return {"busy": len(busy), "ghost": len(ghosts), "ghost_mac_ids": sorted(ghosts)}
