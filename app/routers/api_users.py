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
    LivePlaylist, LocalPlaylist, SeriePlaylist, User, VodPlaylist,
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


def _row(u: User, base: str) -> dict:
    groups = json.loads(u.groups_json or "{}")
    return {"id": u.id, "name": u.name, "password": u.password,
            "m3u_enabled": u.m3u_enabled, "xtream_enabled": u.xtream_enabled,
            "expire_date": u.expire_date, "max_connections": u.max_connections,
            "enabled": u.enabled, "groups": groups,
            "m3u_url": f"{base}/playlist.m3u?u={u.name}&p={u.password}",
            "xtream": {"server": base.rsplit(":", 1)[0].replace("://", "://") ,
                       "url": base, "username": u.name, "password": u.password,
                       "player_api": f"{base}/player_api.php",
                       "get_php": f"{base}/get.php?username={u.name}&password={u.password}&type=m3u_plus&output=ts"},
            }


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
    return {"items": [_row(u, base) for u in rows], "groups_available": groups_available}


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
    u.groups_json = json.dumps(payload.get("groups") or {})
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
        u.groups_json = json.dumps(payload["groups"])
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
