"""
Favicon endpoints: the picture browsers show on the tab, and the Settings
picker that chooses it.

`/favicon.ico` (and `/apple-touch-icon.png`) are deliberately PUBLIC: the login
page has a tab too, and a browser asks for them without carrying the admin
session. Everything that *changes* the icon lives under `/api/branding` and is
admin-only, like the rest of the API.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..database import get_db
from ..models import Setting
from ..security import require_admin
from ..services import branding
from ..services.db_logging import db_log

public = APIRouter(tags=["branding"])
router = APIRouter(prefix="/api/branding", tags=["branding"],
                   dependencies=[Depends(require_admin)])


async def _serve() -> Response:
    path, media_type, _ident = await branding.resolve()
    return Response(
        path.read_bytes(),
        media_type=media_type,
        headers={
            # The <link> tags carry ?v=<fingerprint>, so a long cache is safe for
            # them; the bare /favicon.ico a browser asks for on its own gets the
            # same file and re-checks within the hour.
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            # An uploaded SVG is an image, never a document: no scripts, no
            # outbound requests, even if someone opens the url directly.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


@public.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return await _serve()


@public.get("/apple-touch-icon.png", include_in_schema=False)
@public.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon() -> Response:
    """iOS/iPadOS "add to home screen" asks for this by convention; without the
    route it would hit the 404 handler and be answered with the dashboard."""
    return await _serve()


@router.get("/favicons")
async def list_favicons():
    """Everything the Settings picker needs: choices, current pick, upload state."""
    selected = await branding.selected_id()
    _p, _mt, effective = await branding.resolve()
    custom = branding.custom_path()
    return {
        "selected": selected,
        "effective": effective,          # differs when the pick went stale
        "default": branding.DEFAULT_ID,
        "items": [{"id": b["id"], "label": b["label"], "url": branding.builtin_url(b["id"]),
                   "builtin": True} for b in branding.BUILTINS],
        "custom": ({"id": branding.CUSTOM_ID, "label": "Custom upload",
                    "url": f"/favicon.ico?v={branding.token()}",
                    "filename": custom.name, "bytes": custom.stat().st_size,
                    "builtin": False} if custom else None),
        "max_bytes": branding.MAX_UPLOAD_BYTES,
        "accept": sorted(branding.MEDIA_TYPES),
        "version": branding.token(),
    }


async def _select(db, ident: str) -> str:
    row = await db.get(Setting, branding.SETTING_KEY)
    if row is None:
        row = Setting(key=branding.SETTING_KEY)
        db.add(row)
    row.value = json.dumps(ident)
    await db.commit()
    return await branding.refresh()


@router.post("/favicon")
async def select_favicon(payload: dict, db=Depends(get_db)):
    """Pick a built-in (or the uploaded picture) as the tab icon."""
    ident = (payload.get("id") or "").strip()
    if ident == branding.CUSTOM_ID:
        if branding.custom_path() is None:
            raise HTTPException(400, "no custom picture uploaded yet")
    elif ident not in branding.BUILTIN_IDS:
        raise HTTPException(400, f"unknown favicon '{ident}'")
    version = await _select(db, ident)
    await db_log("INFO", "settings", f"favicon set to '{ident}'")
    return {"ok": True, "selected": ident, "version": version}


@router.post("/favicon/upload")
async def upload_favicon(payload: dict, db=Depends(get_db)):
    """Store an uploaded picture and make it the tab icon.

    The Settings page posts the file as a data URL (same shape as the config
    import), so this stays a plain JSON endpoint.
    """
    filename = (payload.get("filename") or "").strip()
    data_url = payload.get("data") or ""
    try:
        data = branding.decode_data_url(data_url)
        path = branding.save_custom(data, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    version = await _select(db, branding.CUSTOM_ID)
    await db_log("INFO", "settings",
                 f"custom favicon uploaded ({path.name}, {len(data) // 1024 or 1} kB)")
    return {"ok": True, "selected": branding.CUSTOM_ID, "filename": path.name,
            "bytes": len(data), "version": version}


@router.delete("/favicon/custom")
async def delete_custom(db=Depends(get_db)):
    """Drop the uploaded picture; the selection falls back to the default icon."""
    removed = branding.remove_custom()
    selected = await branding.selected_id()
    if selected == branding.CUSTOM_ID:
        await _select(db, branding.DEFAULT_ID)
    else:
        await branding.refresh()
    if removed:
        await db_log("INFO", "settings", "custom favicon removed")
    return {"ok": True, "removed": removed, "selected": await branding.selected_id(),
            "version": branding.token()}
