"""Consumer-facing browser player for output-user accounts.

The player uses the existing User accounts and playback engine. A signed
SessionMiddleware cookie keeps credentials out of browser URLs and access logs;
playlist visibility, template ids, connection limits and actual stream setup are
all checked by the same services used by M3U/Xtream playback.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..database import get_db
from ..models import FFmpegTemplate, User
from ..services.playlist_gen import (
    UserAuth, user_can_play_playlist_item, web_player_playlist_items,
)

router = APIRouter(tags=["web-player"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/player", response_class=HTMLResponse)
async def web_player_page(request: Request):
    response = templates.TemplateResponse(request, "player.html", {})
    response.headers["Cache-Control"] = "no-store"
    return response


async def _session_user(request: Request, db) -> User:
    user_id = request.session.get("web_player_user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise HTTPException(401, "web-player login required")
    user = await db.get(User, user_id)
    if not UserAuth.permitted(user, "stream"):
        request.session.pop("web_player_user_id", None)
        raise HTTPException(401, "web-player session expired or account disabled")
    return user


@router.post("/player/login")
async def web_player_login(payload: dict, request: Request):
    username = payload.get("username")
    password = payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(400, "username and password are required")
    user = await UserAuth.verify(username.strip(), password, "stream")
    if user is None:
        raise HTTPException(403, "Invalid username or password, or account is disabled/expired")
    request.session["web_player_user_id"] = user.id
    return {"ok": True, "user": {"name": user.name}}


@router.post("/player/logout")
async def web_player_logout(request: Request):
    request.session.pop("web_player_user_id", None)
    return {"ok": True}


@router.get("/player/api/playlist")
async def web_player_playlist(request: Request, db=Depends(get_db)):
    user = await _session_user(request, db)
    return {"items": await web_player_playlist_items(user),
            "user": user.name}


@router.get("/player/api/templates")
async def web_player_templates(request: Request, db=Depends(get_db)):
    await _session_user(request, db)
    rows = (await db.execute(select(FFmpegTemplate).where(
        FFmpegTemplate.enabled.is_(True),
        FFmpegTemplate.output_format == "mpegts").order_by(
            FFmpegTemplate.is_default.desc(), FFmpegTemplate.name))).scalars().all()
    # Only publish identifiers and display metadata, never command text. The
    # browser accepts predefined enabled MPEG-TS templates only; it cannot send
    # an arbitrary FFmpeg command or choose an output the TS player cannot read.
    return {"items": [{"id": row.id, "name": row.name,
                       "is_default": bool(row.is_default)} for row in rows]}


@router.api_route("/player/stream/{kind}/{pid}.ts", methods=["GET", "HEAD"])
async def web_player_stream(kind: str, pid: int, request: Request,
                            tpl: int = Query(..., ge=1),
                            db=Depends(get_db)):
    user = await _session_user(request, db)
    # "series" is an alias for "episode" (playlist uses episode ids for series)
    if kind == "series":
        kind = "episode"
    if kind not in ("live", "vod", "episode", "local"):
        raise HTTPException(404, "playlist item not found")
    if not await user_can_play_playlist_item(user, kind, pid):
        # Match the output playlist: disabled, unlinked or group-filtered items
        # must not become playable merely by guessing an integer id.
        raise HTTPException(404, "item not found in this user's playlist")
    template = await db.get(FFmpegTemplate, tpl)
    if (template is None or not template.enabled
            or template.output_format != "mpegts"):
        raise HTTPException(404, "browser-compatible FFmpeg template not found")
    if request.method == "HEAD":
        return Response(status_code=200, media_type="video/mp2t", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
            "Accept-Ranges": "none",
        })

    # `mode=proxy` prevents a configured Redirect preset from sending the
    # browser to a provider/CDN; StreamManager substitutes its safe Copy
    # fallback for that sentinel, and otherwise runs the selected saved template.
    from .output import _stream_response
    return await _stream_response(
        kind, pid, user, f"{kind} #{pid}", request,
        mode="proxy", template_id=tpl,
    )
