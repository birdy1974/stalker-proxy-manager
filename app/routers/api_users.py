"""
User management API (output consumers; M3U + Xtream credentials and per-user
group whitelists for all four content types).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ..database import get_db
from ..models import (
    Area, LivePlaylist, LocalPlaylist, SeriePlaylist, User, VodPlaylist,
)
from ..security import require_admin

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_admin)])

FIELDS = ("name", "password", "m3u_enabled", "xtream_enabled", "expire_date",
          "max_connections", "enabled")


async def _base(request: Request) -> str:
    from ..services.runtime_settings import output_base_url
    override = await output_base_url()
    if override:
        return override
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def _row(u: User, base: str, available: dict | None = None,
         areas: dict[int, str] | None = None) -> dict:
    groups = json.loads(u.groups_json or "{}")
    stale: dict[str, list[str]] | None = None
    if available is not None:
        # whitelist entries that no longer match a group in the library: they
        # are invisible in the editor and, when a type has ONLY dead entries,
        # they filter that whole type out of the user's output
        stale = {k: [v for v in (groups.get(k) or [])
                     if str(v).strip().lower() not in {str(g).strip().lower()
                                                       for g in available.get(k, [])}]
                 for k in ("live", "vod", "series", "local")}
    row = {"id": u.id, "name": u.name, "password": u.password,
            "m3u_enabled": u.m3u_enabled, "xtream_enabled": u.xtream_enabled,
            "expire_date": u.expire_date, "max_connections": u.max_connections,
            "enabled": u.enabled, "groups": groups,
            "area_id": u.area_id,
            "area_name": (areas or {}).get(u.area_id) if u.area_id else None,
            "m3u_url": f"{base}/playlist.m3u?u={u.name}&p={u.password}",
            "xtream": {"server": base.rsplit(":", 1)[0].replace("://", "://") ,
                       "url": base, "username": u.name, "password": u.password,
                       "player_api": f"{base}/player_api.php",
                       "get_php": f"{base}/get.php?username={u.name}&password={u.password}&type=m3u_plus&output=ts"},
            }
    if stale is not None:
        row["groups_stale"] = stale
    return row


@router.get("")
async def list_users(request: Request, db=Depends(get_db)):
    base = await _base(request)
    rows = (await db.execute(select(User).order_by(User.name))).scalars().all()
    # all existing group names per type (for the group whitelist editor)
    async def distinct(model):
        return sorted(g for (g,) in (await db.execute(
            select(model.group_name).distinct())).all() if g)
    groups_available = {
        "live": await distinct(LivePlaylist), "vod": await distinct(VodPlaylist),
        "series": await distinct(SeriePlaylist), "local": await distinct(LocalPlaylist),
    }
    area_rows = (await db.execute(select(Area).order_by(Area.name))).scalars().all()
    areas = {a.id: a.name for a in area_rows}
    return {"items": [_row(u, base, groups_available, areas) for u in rows],
            "groups_available": groups_available,
            "areas": [{"id": a.id, "name": a.name, "enabled": a.enabled} for a in area_rows]}


def _clean_groups(value) -> dict:
    """Normalise a group whitelist coming from the GUI/API: strings only,
    whitespace trimmed (a stray space would silently blackhole that content
    type in every output), empties dropped, per-type duplicates removed. A
    bare string is treated as a one-element list instead of exploding into
    single characters."""
    if not isinstance(value, dict):
        value = {}
    out: dict[str, list[str]] = {}
    for kind in ("live", "vod", "series", "local"):
        vals = value.get(kind) or []
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list):
            vals = []
        seen: set[str] = set()
        out[kind] = []
        for v in vals:
            v = str(v).strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out[kind].append(v)
    return out


@router.post("")
async def create_user(payload: dict, db=Depends(get_db)):
    name = (payload.get("name") or "").strip()
    if not name or not payload.get("password"):
        raise HTTPException(400, "name and password required")
    if (await db.execute(select(User).where(User.name == name))).scalar_one_or_none():
        raise HTTPException(409, "user exists")
    u = User(name=name)
    for f in FIELDS:
        if f in payload:
            setattr(u, f, payload[f])
    u.groups_json = json.dumps(_clean_groups(payload.get("groups")))
    if "area_id" in payload:
        u.area_id = int(payload["area_id"]) if payload.get("area_id") else None
    db.add(u)
    await db.commit()
    return {"id": u.id}


@router.put("/{uid}")
async def update_user(uid: int, payload: dict, db=Depends(get_db)):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404, "user not found")
    for f in FIELDS:
        if f in payload:
            setattr(u, f, payload[f])
    if "groups" in payload:
        u.groups_json = json.dumps(_clean_groups(payload["groups"]))
    if "area_id" in payload:
        u.area_id = int(payload["area_id"]) if payload.get("area_id") else None
    await db.commit()
    return {"ok": True}


@router.delete("/{uid}")
async def delete_user(uid: int, db=Depends(get_db)):
    u = await db.get(User, uid)
    if not u:
        raise HTTPException(404, "user not found")
    await db.delete(u)
    await db.commit()
    return {"ok": True}
