"""
Portal API: CRUD, MAC management, URL resolution, portal test, genre fetch
start, genre enable toggles, per-portal source listings and the delete flow
(Phase-1 D6/G-flow: check fallback usage, optional replace-portal).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, or_, select

from ..database import get_db
from ..models import (
    LiveGenre, LivePlaylistSource, LiveSource, MacAddress, Portal, SerieGenre,
    SeriePlaylistSource, SerieSource, VodGenre, VodPlaylistSource, VodSource,
)
from ..portal.pool import POOL, PortalSession
from ..portal.client import PortalError, status_for_error
from ..portal.account import mac_status
from ..portal.capabilities import (dumps_modules, loads_modules, supports,
                                   version_js_url)
from ..portal.identity import IDENTITY_MODES
from ..portal.resolver import resolve_portal
from ..security import require_admin
from ..services import xtream_bridge
from ..services.db_logging import db_log
from ..services.fetch_jobs import cancel as cancel_job, list_jobs, submit

router = APIRouter(prefix="/api/portals", tags=["portals"], dependencies=[Depends(require_admin)])

MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2}[:\-][0-9a-f]{2}[:\-][0-9a-f]{2}[:\-]"
                    r"[0-9a-f]{2}[:\-][0-9a-f]{2}[:\-][0-9a-f]{2})\b")


def parse_mac_entries(value) -> list[dict]:
    """The `macs` payload -> [{mac, sn, device_id}], in order.

    Accepts the GUI textarea (one string), a list of MAC strings, and a list of
    objects - the last one because a user who captured their box's real `sn` /
    `device_id` needs a way to pin them, and a MAC list that only holds strings
    would force that through a code change. Unknown keys are ignored, and pins
    are trimmed empties -> None so the client derives them from the MAC.
    """
    raw: list = []
    if isinstance(value, str):
        raw = [{"mac": m} for m in parse_macs(value)]
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                raw.append({"mac": item})
            elif isinstance(item, dict) and item.get("mac"):
                raw.append(dict(item))
    out, seen = [], set()
    for entry in raw:
        found = parse_macs(str(entry.get("mac") or ""))
        if not found:
            continue
        mac = found[0]
        if mac in seen:
            continue
        seen.add(mac)
        out.append({"mac": mac,
                    "sn": (str(entry.get("sn") or "").strip() or None),
                    "device_id": (str(entry.get("device_id") or "").strip() or None)})
    return out


def _identity_mode(value) -> str:
    mode = str(value or "").strip().lower()
    if mode not in IDENTITY_MODES:
        raise HTTPException(400, f"identity_mode must be one of: {', '.join(IDENTITY_MODES)}")
    return mode


def parse_macs(text: str) -> list[str]:
    """Spec: comma / semicolon / space / newline delimiters all accepted."""
    found = MAC_RE.findall(text.replace("-", ":"))
    seen, out = set(), []
    for m in found:
        m = m.upper()
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _portal_row(p: Portal, macs: list[MacAddress]) -> dict:
    return {"id": p.id, "name": p.name, "base_url": p.base_url,
            "resolved_url": p.resolved_url, "resolved_path": p.resolved_path,
            "enabled": p.enabled, "proxy_url": p.proxy_url,
            "tls_insecure": bool(p.tls_insecure),
            "identity_mode": p.identity_mode or "mag250",
            "stb_timezone": p.stb_timezone or "",
            "direct_links": bool(getattr(p, "direct_links", True)),
            # R6: what the panel said about itself. `modules` is None until a
            # resolve asked, and the GUI has to tell that apart from "the panel
            # has nothing" - a tab greyed out for a reason nobody can explain is
            # worse than no grey-out at all.
            "portal_version": p.portal_version or "",
            "modules": loads_modules(p.modules),
            "capabilities_at": p.capabilities_at.isoformat() if p.capabilities_at else None,
            "features": {name: supports(loads_modules(p.modules), name)
                         for name in ("live", "vod", "series", "epg", "archive")},
            # R7: the Xtream side of this portal, if it has one. Everything here is
            # masked - the row feeds a GUI table, and a harvested password in a
            # list endpoint is a password in a browser's history.
            "xtream": xtream_bridge.public_observation(xtream_bridge.loads_observation(p.xtream)),
            "xtream_adopted": bool(p.xtream_adopted),
            "xtream_at": p.xtream_at.isoformat() if p.xtream_at else None,
            "macs": [{"id": m.id, "mac": m.mac, "order": m.order, "status": m.status,
                      "online": m.online, "expire_date": m.expire_date,
                      "last_error": m.last_error or "",
                      "force_ch_link_check": bool(m.force_ch_link_check),
                      "sn": m.sn or "", "device_id": m.device_id or "",
                      "fail_count": m.fail_count,
                      "last_checked": m.last_checked.isoformat() if m.last_checked else None}
                     for m in macs]}


@router.get("")
async def list_portals(db=Depends(get_db)):
    portals = (await db.execute(select(Portal).order_by(Portal.name))).scalars().all()
    out = []
    for p in portals:
        macs = (await db.execute(select(MacAddress).where(MacAddress.portal_id == p.id)
                                 .order_by(MacAddress.order))).scalars().all()
        out.append(_portal_row(p, list(macs)))
    return {"items": out, "jobs": list_jobs()}


@router.post("")
async def create_portal(payload: dict, db=Depends(get_db)):
    name = (payload.get("name") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    if not name or not base_url:
        raise HTTPException(400, "name and url are required")
    p = Portal(name=name, base_url=base_url, enabled=bool(payload.get("enabled", True)),
               proxy_url=(payload.get("proxy_url") or None),
               tls_insecure=bool(payload.get("tls_insecure", False)),
               identity_mode=_identity_mode(payload.get("identity_mode", "mag250")),
               stb_timezone=(str(payload.get("stb_timezone") or "").strip() or None),
               direct_links=bool(payload.get("direct_links", True)))
    db.add(p)
    await db.flush()
    for i, entry in enumerate(parse_mac_entries(payload.get("macs", ""))):
        db.add(MacAddress(portal_id=p.id, order=i, **entry))
    await db.commit()
    await db_log("INFO", "portal", f"portal '{name}' created ({base_url})")
    return {"id": p.id}


@router.put("/{pid}")
async def update_portal(pid: int, payload: dict, db=Depends(get_db)):
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    # `xtream_adopted` is deliberately NOT settable here: adoption writes per-channel
    # URLs, and a boolean flipped on its own would put the portal in a state where
    # playback depends on data this endpoint cannot maintain. Use /xtream/adopt.
    for f in ("name", "base_url", "enabled", "proxy_url", "tls_insecure",
              "stb_timezone", "direct_links"):
        if f in payload:
            val = bool(payload[f]) if f in ("tls_insecure", "direct_links") else payload[f]
            if f == "stb_timezone":
                val = str(val or "").strip() or None
            setattr(p, f, val)
    if "identity_mode" in payload:
        # validated separately: a typo here must say so, not silently change what
        # every stream on this portal looks like to the panel
        p.identity_mode = _identity_mode(payload["identity_mode"])
    if "base_url" in payload or "tls_insecure" in payload:
        p.resolved_url = p.resolved_path = None   # endpoint/TLS policy changed -> re-resolve
    p.updated = datetime.now(timezone.utc)
    removed_macs: list[tuple[int, str]] = []
    if "macs" in payload:                        # replace full mac list, keep order/status of existing
        existing = {m.mac: m for m in (await db.execute(
            select(MacAddress).where(MacAddress.portal_id == pid))).scalars().all()}
        entries = parse_mac_entries(payload["macs"])
        wanted = [e["mac"] for e in entries]
        for mac, row in existing.items():
            if mac not in wanted:
                removed_macs.append((row.id, row.mac))
                await db.delete(row)
        have = set(existing) & set(wanted)
        for i, entry in enumerate(entries):
            mac = entry["mac"]
            if mac in have:
                row = existing[mac]
                row.order = i
                # a textarea edit re-sends bare MACs; that must not wipe a pin
                for field in ("sn", "device_id"):
                    if entry.get(field) is not None:
                        setattr(row, field, entry[field])
            else:
                db.add(MacAddress(portal_id=pid, order=i, **entry))
    await db.commit()
    # Runtime leftovers: a deleted MAC must not keep a mac_lock, a redirect
    # lease, or a pooled Stalker session (token + TCP) against the panel.
    if removed_macs:
        await _cleanup_removed_macs(removed_macs)
    return {"ok": True}


# ------------------------------------------------------------------ delete
async def _fallback_usage(db, portal_id: int) -> dict:
    """Which playlist items use this portal's sources in their fallback chain?"""
    live_q = (await db.execute(
        select(LivePlaylistSource, LiveSource)
        .join(LiveSource, LiveSource.id == LivePlaylistSource.live_source_id)
        .where(LiveSource.portal_id == portal_id))).all()
    vod_q = (await db.execute(
        select(VodPlaylistSource, VodSource)
        .join(VodSource, VodSource.id == VodPlaylistSource.vod_source_id)
        .where(VodSource.portal_id == portal_id))).all()
    serie_q = (await db.execute(
        select(SeriePlaylistSource, SerieSource)
        .join(SerieSource, SerieSource.id == SeriePlaylistSource.serie_source_id)
        .where(SerieSource.portal_id == portal_id))).all()
    return {
        "live": [{"playlist_id": r[0].live_playlist_id, "source_id": r[1].id,
                  "name": r[1].original_name, "priority": r[0].priority} for r in live_q],
        "vod": [{"playlist_id": r[0].vod_playlist_id, "source_id": r[1].id,
                 "name": r[1].original_name, "priority": r[0].priority} for r in vod_q],
        "series": [{"playlist_id": r[0].serie_playlist_id, "source_id": r[1].id,
                    "name": r[1].original_name, "priority": r[0].priority} for r in serie_q],
    }


@router.get("/{pid}/delete-check")
async def delete_check(pid: int, db=Depends(get_db)):
    """Ask BEFORE delete: is this portal used in any fallback chain?"""
    if not await db.get(Portal, pid):
        raise HTTPException(404, "portal not found")
    usage = await _fallback_usage(db, pid)
    others = (await db.execute(select(Portal).where(Portal.id != pid, Portal.enabled.is_(True))
                               .order_by(Portal.name))).scalars().all()
    return {"usage": usage,
            "in_use": any(usage[k] for k in usage),
            "replacement_candidates": [{"id": p.id, "name": p.name} for p in others]}


@router.delete("/{pid}")
async def delete_portal(pid: int, db=Depends(get_db), replacement_portal_id: int | None = None):
    """
    Delete a portal + ALL internal references (FK cascades handle sources,
    genres, playlist-source rows). If replacement_portal_id is given, fallback
    chain rows pointing at this portal's sources are first re-pointed to
    best-effort name-matched sources of the replacement portal (G-flow).

    Runtime state is scrubbed too: mac_locks / redirect leases for every MAC of
    this portal, and every pooled Stalker session pointed at its URL.
    """
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    # Snapshot before the cascade wipes the rows — needed for runtime cleanup.
    mac_rows = list((await db.execute(
        select(MacAddress).where(MacAddress.portal_id == pid))).scalars().all())
    removed_macs = [(m.id, m.mac) for m in mac_rows]
    portal_urls = [u for u in (p.resolved_url, p.base_url) if u]
    usage = await _fallback_usage(db, pid)
    replaced = 0
    if replacement_portal_id:
        target = await db.get(Portal, replacement_portal_id)
        if not target:
            raise HTTPException(400, "replacement portal not found")
        replaced = await _repoint_fallbacks(db, pid, replacement_portal_id)
    await db.delete(p)          # cascades: macs, genres, sources, playlist-source rows
    await db.commit()
    await _cleanup_removed_macs(removed_macs, portal_urls=portal_urls)
    msg = f"portal '{p.name}' deleted; fallback rows repointed: {replaced}" if replacement_portal_id \
        else f"portal '{p.name}' deleted ({sum(len(v) for v in usage.values())} fallback rows removed)"
    await db_log("WARNING", "portal", msg)
    return {"ok": True, "repointed": replaced}


async def _cleanup_removed_macs(removed: list[tuple[int, str]], *,
                                portal_urls: list[str] | None = None) -> None:
    """Drop in-memory leftovers of deleted MAC rows / a deleted portal.

    DB FKs already cascade the durable state. What they cannot touch:
      * StreamManager.mac_locks / redirect_leases (keyed by mac_id)
      * ClientPool sessions (keyed by portal_url + normalised MAC)
    Leaving either behind would keep a ghost occupancy that blocks the next
    play, or a live token against a panel the operator just removed.
    """
    from ..services.stream_manager import MANAGER
    for mid, mac in removed:
        try:
            MANAGER.release_mac(mid)
        except Exception:  # noqa: BLE001
            pass
        try:
            await POOL.drop_mac(mac)
        except Exception:  # noqa: BLE001
            pass
    for url in portal_urls or ():
        try:
            await POOL.drop_portal_url(url)
        except Exception:  # noqa: BLE001
            pass


async def _repoint_fallbacks(db, old_pid: int, new_pid: int) -> int:
    """Name-match old portal's sources onto the replacement portal (lowercase compare)."""
    n = 0
    for pl_model, src_model, fk in (
            (LivePlaylistSource, LiveSource, LivePlaylistSource.live_source_id),
            (VodPlaylistSource, VodSource, VodPlaylistSource.vod_source_id),
            (SeriePlaylistSource, SerieSource, SeriePlaylistSource.serie_source_id)):
        rows = (await db.execute(
            select(pl_model, src_model).join(src_model, src_model.id == fk)
            .where(src_model.portal_id == old_pid))).all()
        for link, src in rows:
            match = (await db.execute(select(src_model).where(
                src_model.portal_id == new_pid,
                src_model.original_name.ilike(src.original_name)))).scalars().first()
            if match:
                setattr(link, fk.key, match.id)
                n += 1
    return n


# ------------------------------------------------------------------ resolve/test
@router.post("/{pid}/resolve")
async def resolve(pid: int, db=Depends(get_db)):
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    first_mac = (await db.execute(select(MacAddress).where(MacAddress.portal_id == pid)
                                  .order_by(MacAddress.order))).scalars().first()
    res = await resolve_portal(p.base_url, mac=first_mac.mac if first_mac else None,
                               proxy=p.proxy_url, tls_insecure=p.tls_insecure)
    for line in res.attempts:
        await db_log("DEBUG", "resolve", f"[{p.name}] {line}")
    caps: dict = {}
    if res.ok:
        p.resolved_url, p.resolved_path = res.portal_url, res.path
        if res.version.known:
            p.portal_version = res.version.label[:120]
        p.capabilities_at = datetime.now(timezone.utc)
        await db.commit()
        await db_log("INFO", "resolve",
                     f"[{p.name}] resolved -> {res.portal_url}"
                     + (f" ({res.version.label})" if res.version.known else ""))
        # The module list needs a token, so it comes from a client session and
        # not from the resolver - but only here, where a user is *looking* at the
        # portal: an IP-locked panel is best asked once, on purpose.
        caps = await _probe_capabilities(p, first_mac, res.portal_url, db)
    else:
        await db_log("ERROR", "resolve", f"[{p.name}] resolve failed: {res.error}")
    return {"ok": res.ok, "resolved_url": res.portal_url, "path": res.path,
            "attempts": res.attempts, "error": res.error,
            "referer": res.referer, "version": res.version.public(),
            "version_url": version_js_url(res.portal_url) if res.ok else "",
            **caps}


async def _probe_capabilities(p: Portal, mac_row, portal_url: str, db) -> dict:
    """Ask the panel what it offers, and store the answer. Never fails a resolve.

    Both probes are one cheap GET, and both are *information*: `version.js` needs
    no token at all. That is the whole argument for doing it on Resolve - the
    user is already watching this portal - and the whole argument for letting
    every failure pass silently: an information request must not be able to turn
    a working portal red.
    """
    out: dict = {"modules": loads_modules(p.modules), "features": {}, "modules_error": ""}
    if not mac_row:
        out["modules_error"] = "no MAC to ask with"
        return out
    try:
        client = await POOL.get(PortalSession.from_rows(p, mac_row, portal_url=portal_url))
        try:
            await client.ensure_auth()
            caps = await client.refresh_capabilities()
        finally:
            await client.close()
    except PortalError as exc:
        out["modules_error"] = exc.code
        await db_log("DEBUG", "resolve",
                     f"[{p.name}] capabilities unavailable ({exc.code}) - nothing is "
                     f"gated off on an answer we never got")
        return out
    except Exception as exc:  # noqa: BLE001 - information only, never fatal
        out["modules_error"] = type(exc).__name__
        return out
    modules = caps.get("modules")
    if modules:
        p.modules = dumps_modules(modules)
    version = caps.get("version") or {}
    if version.get("label"):
        p.portal_version = str(version["label"])[:120]
    p.capabilities_at = datetime.now(timezone.utc)
    await db.commit()
    if modules is None:
        await db_log("INFO", "resolve",
                     f"[{p.name}] get_modules said nothing ({caps.get('modules_error')})"
                     f" - no catalogue is gated off")
    else:
        missing = [f for f in ("vod", "series", "epg", "archive")
                   if supports(modules, f) is False]
        await db_log("INFO", "resolve",
                     f"[{p.name}] modules: {', '.join(modules)}"
                     + (f" | nothing for: {', '.join(missing)}" if missing else ""))
    out["modules"] = modules
    out["modules_error"] = str(caps.get("modules_error") or "")
    out["features"] = {name: supports(modules, name) for name in
                       ("live", "vod", "series", "epg", "archive")}
    return out


@router.post("/{pid}/test")
async def test_portal(pid: int, db=Depends(get_db)):
    """Handshake every MAC -> status/online/expire; resolves the URL if needed."""
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    macs = (await db.execute(select(MacAddress).where(MacAddress.portal_id == pid)
                             .order_by(MacAddress.order))).scalars().all()
    results = []
    url = p.resolved_url
    if not url:
        res = await resolve_portal(p.base_url, mac=macs[0].mac if macs else None,
                                   proxy=p.proxy_url, tls_insecure=p.tls_insecure)
        if not res.ok:
            for m in macs:
                m.status, m.online = "offline", False
                m.last_checked = datetime.now(timezone.utc)
            await db.commit()
            await db_log("ERROR", "portal", f"[{p.name}] offline: {res.error}")
            return {"results": [{"mac": m.mac, "status": "offline", "detail": res.error}
                                for m in macs]}
        url = res.portal_url
        p.resolved_url, p.resolved_path = res.portal_url, res.path
    for m in macs:
        client = await POOL.get(PortalSession.from_rows(p, m, portal_url=url))
        code = ""
        try:
            await client.handshake()
            # The panel's own verdict, not "the handshake worked". A MAC can
            # handshake all day and still be banned or out of subscription -
            # and while it stays in the chains it burns a portal connection slot
            # on every attempt and the user sees a black channel.
            verdict = await client.refresh_account()
            m.expire_date = verdict.expire_date
            m.force_ch_link_check = verdict.force_ch_link_check
            m.status, m.online = mac_status(verdict), verdict.online
            m.last_error = None if m.online else verdict.reason[:200]
            m.fail_count = 0 if m.online else m.fail_count
            detail = verdict.reason
            if not m.online:
                code = verdict.status
            await db_log("INFO" if m.online else "WARNING", "portal",
                         f"[{p.name}] mac {m.mac} {m.status} ({detail})")
        except PortalError as exc:
            # Was: guess from substrings of the message ("401" in msg, "token"
            # in msg). The client now carries the portal's own code, so the
            # status is decided from what the panel actually said.
            code = exc.code
            m.status = status_for_error(exc)
            m.online = False
            m.fail_count += 1
            detail = exc.detail()
            m.last_error = detail[:200]
            await db_log("WARNING", "portal",
                         f"[{p.name}] mac {m.mac} failed ({m.status}): {detail}")
        finally:
            m.last_checked = datetime.now(timezone.utc)
            await client.close()
        results.append({"mac": m.mac, "status": m.status, "expire_date": m.expire_date,
                        "online": m.online, "code": code or None, "detail": detail,
                        "force_ch_link_check": bool(m.force_ch_link_check),
                        "sn": m.sn or client.identity.sn,
                        "device_id": m.device_id or client.identity.device_id})
    # A check is also the moment a user wants to know what this portal *is*, and
    # the two probes cost one request each - so fill them in when they were never
    # done, and do not re-ask a panel that has already answered. An IP-locked
    # portal notices both kinds of noise, and `portal_version`/`modules` do not
    # change minute to minute.
    caps = {}
    if results and (not p.modules or not p.portal_version) \
            and any(r["online"] for r in results):
        online = next((m for m in macs if m.online), macs[0] if macs else None)
        caps = await _probe_capabilities(p, online, url, db)
    # R7, same occasion and the same discipline: if this portal has never been
    # looked at for an Xtream side, look once while we already have a working
    # session. Nothing here can fail the check - a portal without the bridge is the
    # ordinary case, and "we found one" is information the user acts on, not an
    # automatic change to how their TV plays.
    xt = {}
    if results and not p.xtream and any(r["online"] for r in results):
        try:
            xt = await xtream_bridge.probe(db, p)
        except Exception as exc:  # noqa: BLE001
            await db_log("DEBUG", "portal", f"[{p.name}] xtream probe crashed: {exc}")
    await db.commit()
    return {"results": results, "capabilities": caps or None, "xtream": xt or None}


# ------------------------------------------------------------------ xtream bridge (R7)
@router.post("/{pid}/xtream")
async def xtream_probe(pid: int, force: bool = False, db=Depends(get_db)):
    """Look for the Xtream account hidden inside this portal's stream links.

    Read-only by design: this stores what it finds and changes no playback. The
    harvest asks the panel for exactly one `create_link` answer - which is a real
    request against a real account, so it happens on demand (here, or during Check
    Portal when nothing is known yet) and not on a timer.
    """
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    out = await xtream_bridge.probe(db, p, force=force)
    return {**out, "portal": _portal_row(p, [])}


@router.post("/{pid}/xtream/adopt")
async def xtream_adopt(pid: int, force: bool = False, db=Depends(get_db)):
    """The explicit step: match the Xtream catalogue and let its links bypass the MAC path.

    `force=1` is the only way to adopt an account the panel reports as expired or
    banned, and it exists because some panels report their own account state
    wrongly - not because adopting a dead login should be easy by accident.
    """
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    out = await xtream_bridge.adopt(db, p, force=force)
    if not out.get("ok") and out.get("error"):
        raise HTTPException(400, out["error"])
    return {**out, "portal": _portal_row(p, [])}


@router.post("/{pid}/xtream/detach")
async def xtream_detach(pid: int, clear: bool = False, db=Depends(get_db)):
    """Stop using the harvested links. `clear=1` also drops them from the rows."""
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    out = await xtream_bridge.detach(db, p, clear=clear)
    return {**out, "portal": _portal_row(p, [])}


@router.post("/test-batch")
async def test_batch(payload: dict, db=Depends(get_db)):
    """ports from GUI: test SELECTED portals or ALL (spec: Add/Delete/Test sel or all)."""
    ids = payload.get("ids") or []
    q = select(Portal) if not ids else select(Portal).where(Portal.id.in_(ids))
    portals = (await db.execute(q)).scalars().all()
    out = {}
    for p in portals:
        out[p.id] = (await test_portal(p.id, db))["results"]
    return {"results": out}


@router.post("/mac-health/refresh")
async def mac_health_refresh(payload: dict | None = None):
    """Run the multi-MAC status/expiry sweep once (same work the scheduler does).

    Body (all optional):
      portal_id   – only this portal
      only_multi  – default true: portals with fewer than 2 MACs are skipped
      skip_busy   – default true: MACs holding a live stream are left alone
    """
    from ..services import mac_health
    body = payload or {}
    pid = body.get("portal_id")
    skip_busy = bool(body.get("skip_busy", True))
    if pid:
        return await mac_health.refresh_portal_macs(int(pid), skip_busy=skip_busy)
    only_multi = bool(body.get("only_multi", True))
    return await mac_health.refresh_all_macs(only_multi=only_multi, skip_busy=skip_busy)


@router.post("/{pid}/compare-genres")
async def compare_portal_genres(pid: int):
    """Ask every online MAC of this portal for its genre lists and report diffs.

    The union of every genre any compared MAC returned is upserted into the
    portal's live/vod/series genre tables (existing `enabled` flags are kept;
    brand-new genres land disabled). Offline/expired/banned MACs are listed
    under `skipped` and not asked. Use this when a secondary MAC might be a
    different package (shared-login resellers often do that).
    """
    from ..services import mac_health
    out = await mac_health.compare_genres(pid)
    if not out.get("ok") and out.get("error") == "portal not found":
        raise HTTPException(404, "portal not found")
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "compare failed")
    return out


# ------------------------------------------------------------------ genres & fetch
@router.post("/{pid}/fetch")
async def start_fetch(pid: int, db=Depends(get_db)):
    """Full fetch: genre lists + items of enabled genres + series seasons."""
    if not await db.get(Portal, pid):
        raise HTTPException(404, "portal not found")
    job = await submit("fetch_portal", pid)
    await db_log("INFO", "fetch", f"fetch job {job.id} queued for portal {pid}")
    return {"job": job.public()}


@router.post("/{pid}/fetch-genres")
async def start_genre_fetch(pid: int, db=Depends(get_db)):
    """Fetch ONLY the genre lists (live, vod, series) - no items."""
    if not await db.get(Portal, pid):
        raise HTTPException(404, "portal not found")
    job = await submit("fetch_genres", pid)
    await db_log("INFO", "fetch", f"genre fetch job {job.id} queued for portal {pid}")
    return {"job": job.public()}


@router.post("/{pid}/fetch-items")
async def start_items_fetch(pid: int, db=Depends(get_db)):
    """Fetch the items of ENABLED genres (live, vod, series) + seasons."""
    if not await db.get(Portal, pid):
        raise HTTPException(404, "portal not found")
    job = await submit("fetch_items", pid)
    await db_log("INFO", "fetch", f"items fetch job {job.id} queued for portal {pid}")
    return {"job": job.public()}


@router.post("/jobs/{job_id}/cancel")
async def cancel_fetch(job_id: str):
    return {"ok": cancel_job(job_id)}


@router.get("/{pid}/genres")
async def genres(pid: int, db=Depends(get_db)):
    out = {}
    for kind, model in (("live", LiveGenre), ("vod", VodGenre), ("series", SerieGenre)):
        rows = (await db.execute(select(model).where(model.portal_id == pid)
                                 .order_by(model.name))).scalars().all()
        out[kind] = [{"id": r.id, "name": r.name, "enabled": r.enabled,
                      "genre_portal_id": r.genre_portal_id,
                      "item_count": r.item_count,
                      "fetched": getattr(r, "channels_fetched", getattr(r, "items_fetched", None))}
                     for r in rows]
    return out


@router.post("/{pid}/genres/toggle")
async def toggle_genres(pid: int, payload: dict, db=Depends(get_db)):
    """payload: {kind: live|vod|series, ids: [...], enabled: true|false}"""
    model = {"live": LiveGenre, "vod": VodGenre, "series": SerieGenre}[payload["kind"]]
    ids = payload.get("ids", [])
    rows = (await db.execute(select(model).where(model.portal_id == pid,
                                                 model.id.in_(ids)))).scalars().all()
    for r in rows:
        r.enabled = bool(payload.get("enabled"))
    await db.commit()
    return {"ok": True, "count": len(rows)}


# ------------------------------------------------------------------ portal source preview
@router.get("/{pid}/items")
async def portal_items(pid: int, kind: str, db=Depends(get_db), q: str = "", page: int = 1,
                       per_page: int = 25):
    """Items already stored for one portal (portal popup tabs; NO fetching here)."""
    model = {"live": LiveSource, "vod": VodSource, "series": SerieSource}.get(kind)
    if model is None:
        raise HTTPException(400, "kind must be live|vod|series")
    stmt = select(model).where(model.portal_id == pid)
    if q:
        stmt = stmt.where(model.original_name.ilike(f"%{q}%"))       # case-insensitive
    stmt = stmt.order_by(model.original_name)
    rows = (await db.execute(stmt)).scalars().all()
    total = len(rows)
    rows = rows[(page - 1) * per_page: page * per_page]
    return {"total": total, "page": page, "per_page": per_page, "items":
            [{"id": r.id, "name": r.original_name, "enabled": r.enabled,
              "poster": getattr(r, "poster", None) or getattr(r, "logo_original", None)}
             for r in rows]}
