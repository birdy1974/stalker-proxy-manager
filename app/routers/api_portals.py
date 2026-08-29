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
from ..portal.pool import POOL
from ..portal.client import PortalError, StalkerClient
from ..portal.resolver import resolve_portal
from ..security import require_admin
from ..services.db_logging import db_log
from ..services.fetch_jobs import cancel as cancel_job, list_jobs, submit

router = APIRouter(prefix="/api/portals", tags=["portals"], dependencies=[Depends(require_admin)])

MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2}[:\-][0-9a-f]{2}[:\-][0-9a-f]{2}[:\-]"
                    r"[0-9a-f]{2}[:\-][0-9a-f]{2}[:\-][0-9a-f]{2})\b")


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
            "macs": [{"id": m.id, "mac": m.mac, "order": m.order, "status": m.status,
                      "online": m.online, "expire_date": m.expire_date,
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
               proxy_url=(payload.get("proxy_url") or None))
    db.add(p)
    await db.flush()
    for i, mac in enumerate(parse_macs(payload.get("macs", ""))):
        db.add(MacAddress(portal_id=p.id, mac=mac, order=i))
    await db.commit()
    await db_log("INFO", "portal", f"portal '{name}' created ({base_url})")
    return {"id": p.id}


@router.put("/{pid}")
async def update_portal(pid: int, payload: dict, db=Depends(get_db)):
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    for f in ("name", "base_url", "enabled", "proxy_url"):
        if f in payload:
            setattr(p, f, payload[f])
    if "base_url" in payload:                    # URL changed -> re-resolve later
        p.resolved_url = p.resolved_path = None
    p.updated = datetime.now(timezone.utc)
    if "macs" in payload:                        # replace full mac list, keep order/status of existing
        existing = {m.mac: m for m in (await db.execute(
            select(MacAddress).where(MacAddress.portal_id == pid))).scalars().all()}
        wanted = parse_macs(payload["macs"])
        for mac, row in existing.items():
            if mac not in wanted:
                await db.delete(row)
        have = set(existing) & set(wanted)
        for i, mac in enumerate(wanted):
            if mac in have:
                existing[mac].order = i
            else:
                db.add(MacAddress(portal_id=pid, mac=mac, order=i))
    await db.commit()
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
    """
    p = await db.get(Portal, pid)
    if not p:
        raise HTTPException(404, "portal not found")
    usage = await _fallback_usage(db, pid)
    replaced = 0
    if replacement_portal_id:
        target = await db.get(Portal, replacement_portal_id)
        if not target:
            raise HTTPException(400, "replacement portal not found")
        replaced = await _repoint_fallbacks(db, pid, replacement_portal_id)
    await db.delete(p)          # cascades: macs, genres, sources, playlist-source rows
    await db.commit()
    msg = f"portal '{p.name}' deleted; fallback rows repointed: {replaced}" if replacement_portal_id \
        else f"portal '{p.name}' deleted ({sum(len(v) for v in usage.values())} fallback rows removed)"
    await db_log("WARNING", "portal", msg)
    return {"ok": True, "repointed": replaced}


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
                               proxy=p.proxy_url)
    for line in res.attempts:
        await db_log("DEBUG", "resolve", f"[{p.name}] {line}")
    if res.ok:
        p.resolved_url, p.resolved_path = res.portal_url, res.path
        await db.commit()
        await db_log("INFO", "resolve", f"[{p.name}] resolved -> {res.portal_url}")
    else:
        await db_log("ERROR", "resolve", f"[{p.name}] resolve failed: {res.error}")
    return {"ok": res.ok, "resolved_url": res.portal_url, "path": res.path,
            "attempts": res.attempts, "error": res.error}


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
        res = await resolve_portal(p.base_url, mac=macs[0].mac if macs else None, proxy=p.proxy_url)
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
        client = await POOL.get(url, m.mac, m.password, p.proxy_url)
        try:
            await client.handshake()
            exp = await client.account_expires()
            m.status, m.online, m.expire_date, m.fail_count = "online", True, exp, 0
            detail = f"expires: {exp or 'unknown'}"
            await db_log("INFO", "portal", f"[{p.name}] mac {m.mac} online ({detail})")
        except PortalError as exc:
            msg = str(exc).lower()
            m.status = ("unauthorized" if "401" in msg or "403" in msg or "token" in msg
                        else "offline" if "request failed" in msg or "http 5" in msg else "error")
            m.online = False
            m.fail_count += 1
            detail = str(exc)
            await db_log("WARNING", "portal", f"[{p.name}] mac {m.mac} failed: {detail}")
        finally:
            m.last_checked = datetime.now(timezone.utc)
            await client.close()
        results.append({"mac": m.mac, "status": m.status, "expire_date": m.expire_date,
                        "detail": detail})
    await db.commit()
    return {"results": results}


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
