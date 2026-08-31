"""
Enigma2 output API: profiles, preview, and the files a receiver installs.

Admin endpoints live under /api/enigma2 (session-authenticated like the rest of
the GUI). The two PUBLIC endpoints a box calls itself are in this module too,
on a separate router that authenticates with the profile's opaque token
instead - a set-top box has no admin session, and putting the GUI password in
a cron line on the receiver would be worse than the token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from ..database import get_db
from ..models import Enigma2Profile, User
from ..security import require_admin
from ..services import enigma2_bouquets as e2
from ..services import enigma2_push as e2push
from ..services.db_logging import db_log

router = APIRouter(prefix="/api/enigma2", tags=["enigma2"],
                   dependencies=[Depends(require_admin)])
public = APIRouter(tags=["enigma2"])

# Columns a client may write. `token` is generated, the status columns are ours.
EDITABLE = [
    "name", "enabled", "user_id", "host", "web_port", "use_https",
    "owif_auth", "owif_user",
    "owif_pass", "transport", "ftp_port", "ssh_port", "login", "password",
    "bouquet_prefix", "player_live", "player_vod", "player_series",
    "container_mode", "container_live", "container_vod", "container_series",
    "delivery_mode",
    "include_live", "include_vod", "include_series", "include_local",
    "groups_json", "layout", "max_entries",
]
READONLY = ["id", "token", "last_build_at", "bouquet_count", "service_count",
            "last_push_at", "last_push_result"]


def _row(p: Enigma2Profile) -> dict:
    out = {c: getattr(p, c) for c in EDITABLE + READONLY}
    # never hand a password back to the browser; say whether one is stored
    out["password"] = ""
    out["owif_pass"] = ""
    out["has_password"] = bool(p.password)
    out["has_owif_pass"] = bool(p.owif_pass)
    return out


def _apply(p: Enigma2Profile, payload: dict) -> None:
    for f in EDITABLE:
        if f not in payload:
            continue
        val = payload[f]
        # an empty password field means "keep the stored one" (the GUI never
        # sees it, so it cannot send it back)
        if f in ("password", "owif_pass") and (val is None or val == ""):
            continue
        if f in ("web_port", "ftp_port", "ssh_port", "max_entries"):
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        if f == "user_id":
            val = int(val) if str(val or "").strip() not in ("", "0", "None") else None
        if f in ("player_live", "player_vod", "player_series"):
            val = str(val)
            if val not in e2.PLAYERS:
                raise HTTPException(400, f"unknown Enigma2 player/service type {val!r}")
        if f in ("container_live", "container_vod", "container_series") \
                and val not in e2.CONTAINERS:
            raise HTTPException(400, f"container must be one of {e2.CONTAINERS}")
        if f == "container_mode" and val not in e2.CONTAINER_MODES:
            raise HTTPException(400, f"container mode must be one of {e2.CONTAINER_MODES}")
        if f == "layout" and val not in e2.LAYOUTS:
            raise HTTPException(400, f"layout must be one of {e2.LAYOUTS}")
        if f == "delivery_mode" and val not in e2.DELIVERY_MODES:
            raise HTTPException(400, f"delivery must be one of {e2.DELIVERY_MODES}")
        if f == "owif_auth" and val not in e2push.OWIF_AUTH:
            raise HTTPException(400, f"OpenWebif auth must be one of {e2push.OWIF_AUTH}")
        if f == "transport" and val not in e2.TRANSPORTS:
            raise HTTPException(400, f"transport must be one of {e2.TRANSPORTS}")
        setattr(p, f, val)


async def _base_url(request: Request) -> str:
    """Public base URL the BOX must be able to reach (Settings override first)."""
    from ..services.runtime_settings import output_base_url
    override = await output_base_url()
    if override:
        return override
    return f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


# ------------------------------------------------------------------ metadata
@router.get("/meta")
async def meta(db=Depends(get_db)):
    """Everything the editor's dropdowns need, straight from the source of
    truth (so a new player/layout never has to be added in two places)."""
    users = (await db.execute(select(User).order_by(User.name))).scalars().all()
    return {
        "players": e2.PLAYERS,
        "containers": list(e2.CONTAINERS),
        "container_modes": list(e2.CONTAINER_MODES),
        "layouts": list(e2.LAYOUTS),
        "delivery_modes": list(e2.DELIVERY_MODES),
        "transports": list(e2.TRANSPORTS),
        "owif_auth": list(e2push.OWIF_AUTH),
        "reload_mode": e2push.RELOAD_MODE,
        "users": [{"id": u.id, "name": u.name, "enabled": u.enabled} for u in users],
    }


# ------------------------------------------------------------------ CRUD
@router.get("/profiles")
async def list_profiles(db=Depends(get_db)):
    rows = (await db.execute(select(Enigma2Profile)
                             .order_by(Enigma2Profile.name))).scalars().all()
    return {"items": [_row(r) for r in rows]}


@router.post("/profiles")
async def create_profile(payload: dict, db=Depends(get_db)):
    p = Enigma2Profile(name=(payload.get("name") or "New receiver").strip(),
                       token=e2.new_token())
    _apply(p, payload)
    db.add(p)
    await db.commit()
    await db_log("INFO", "enigma2", f"profile '{p.name}' created")
    return {"item": _row(p)}


@router.put("/profiles/{pid}")
async def update_profile(pid: int, payload: dict, db=Depends(get_db)):
    p = await db.get(Enigma2Profile, pid)
    if not p:
        raise HTTPException(404, "profile not found")
    _apply(p, payload)
    await db.commit()
    return {"item": _row(p)}


@router.delete("/profiles/{pid}")
async def delete_profile(pid: int, db=Depends(get_db)):
    p = await db.get(Enigma2Profile, pid)
    if not p:
        raise HTTPException(404, "profile not found")
    await db.delete(p)
    await db.commit()
    return {"ok": True}


@router.post("/profiles/{pid}/token")
async def rotate_token(pid: int, db=Depends(get_db)):
    """New pull token - the old install.sh/tarball URLs stop working."""
    p = await db.get(Enigma2Profile, pid)
    if not p:
        raise HTTPException(404, "profile not found")
    p.token = e2.new_token()
    await db.commit()
    await db_log("INFO", "enigma2", f"profile '{p.name}': pull token rotated")
    return {"item": _row(p)}


# ------------------------------------------------------------------ render
async def _bundle(pid: int, request: Request, db) -> tuple[Enigma2Profile, e2.Bundle, str]:
    p = await db.get(Enigma2Profile, pid)
    if not p:
        raise HTTPException(404, "profile not found")
    base = await _base_url(request)
    bundle = await e2.build_bundle(p, base)
    p.last_build_at = datetime.now(timezone.utc)
    p.bouquet_count = len(bundle.files)
    p.service_count = bundle.services
    await db.commit()
    return p, bundle, base


@router.post("/profiles/{pid}/preview")
async def preview(pid: int, request: Request, db=Depends(get_db)):
    """Exactly what would be written, before anything leaves the server."""
    p, bundle, base = await _bundle(pid, request, db)
    warnings = list(bundle.warnings)
    if base.split("//")[-1].split(":")[0] in ("localhost", "127.0.0.1"):
        warnings.append(f"the URLs point at {base} - a receiver cannot reach that. "
                        "Set the public base URL in Settings first.")
    return {
        "summary": bundle.summary() | {"warnings": warnings},
        "base_url": base,
        "install_url": f"{base.rstrip('/')}/enigma2/{p.token}/install.sh",
        "files": [{"name": f.name, "title": f.title, "services": f.services,
                   "text": f.text} for f in bundle.files],
        "bouquets_add": e2.bouquets_add_file([f.name for f in bundle.files])
        if bundle.files else "",
    }


@router.get("/profiles/{pid}/bouquets.tar.gz")
async def download(pid: int, request: Request, db=Depends(get_db)):
    p, bundle, _base = await _bundle(pid, request, db)
    if not bundle.files:
        raise HTTPException(409, "nothing to download: this profile renders no services")
    data = e2.tarball_bytes(bundle, p.bouquet_prefix)
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{e2.slugify(p.name)}-bouquets.tar.gz"'})


# ------------------------------------------------------------------ push (E3)
async def _finish_push(p: Enigma2Profile, rep, db, what: str) -> dict:
    """Store the outcome on the profile so the GUI shows it after a reload."""
    if not rep.dry_run:
        p.last_push_at = datetime.now(timezone.utc)
        p.last_push_result = rep.text() or ("ok" if rep.ok else rep.error)
        await db.commit()
    await db_log("INFO" if rep.ok else "ERROR", "enigma2",
                 f"profile '{p.name}': {what} "
                 f"{'ok' if rep.ok else 'failed - ' + (rep.error or 'see log')}")
    return rep.as_dict() | {"item": _row(p)}


@router.post("/profiles/{pid}/test")
async def test_receiver(pid: int, db=Depends(get_db)):
    """Log in over FTP and ping OpenWebif; writes nothing on the box."""
    p = await db.get(Enigma2Profile, pid)
    if not p:
        raise HTTPException(404, "profile not found")
    rep = await e2push.test_connection(p)
    return rep.as_dict()


@router.post("/profiles/{pid}/push")
async def push(pid: int, request: Request, dry_run: bool = False, db=Depends(get_db)):
    """Render, then upload to the receiver and reload its service list.

    `dry_run=1` connects and reports exactly what would happen without writing
    a single byte - the recommended first click for a new receiver.
    """
    p, bundle, base = await _bundle(pid, request, db)
    if base.split("//")[-1].split(":")[0] in ("localhost", "127.0.0.1"):
        raise HTTPException(409, f"the URLs would point at {base}, which the receiver "
                                 "cannot reach - set the public base URL in Settings first")
    rep = await e2push.push_bundle(p, bundle, dry_run=dry_run)
    return await _finish_push(p, rep, db, "dry-run push" if dry_run else "push")


@router.post("/profiles/{pid}/restore")
async def restore(pid: int, db=Depends(get_db)):
    """Put the receiver's backed-up bouquets.tv back and drop our bouquets."""
    p = await db.get(Enigma2Profile, pid)
    if not p:
        raise HTTPException(404, "profile not found")
    rep = await e2push.restore(p)
    return await _finish_push(p, rep, db, "restore")


# ------------------------------------------------------------------ public
# The receiver fetches these itself (installer one-liner or a cron entry). They
# carry the profile's token instead of an admin session; the streams inside
# still need the profile user's credentials, which are baked into the URLs.
async def _by_token(token: str, db) -> Enigma2Profile:
    p = (await db.execute(select(Enigma2Profile).where(
        Enigma2Profile.token == token))).scalar_one_or_none()
    if not p or not p.enabled:
        raise HTTPException(404, "unknown or disabled profile")
    return p


@public.get("/enigma2/{token}/bouquets.tar.gz")
async def public_tarball(token: str, request: Request, db=Depends(get_db)):
    p = await _by_token(token, db)
    base = await _base_url(request)
    bundle = await e2.build_bundle(p, base)
    p.last_build_at = datetime.now(timezone.utc)
    p.bouquet_count, p.service_count = len(bundle.files), bundle.services
    await db.commit()
    await db_log("INFO", "enigma2",
                 f"profile '{p.name}': receiver pulled {len(bundle.files)} bouquets "
                 f"({bundle.services} services)")
    return Response(e2.tarball_bytes(bundle, p.bouquet_prefix),
                    media_type="application/gzip")


@public.get("/enigma2/{token}/install.sh")
async def public_installer(token: str, request: Request, db=Depends(get_db)):
    p = await _by_token(token, db)
    base = await _base_url(request)
    return PlainTextResponse(e2.install_script(base, p.token, p.bouquet_prefix),
                             media_type="text/x-shellscript")
