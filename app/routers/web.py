"""
Server-rendered GUI pages. Heavy data loads happen client-side via the REST
APIs (fast first paint; pages stay light). All pages require the admin session
except /login.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import ADMIN_USERNAME, HTTP_PORT, MOCK_PORTAL_ENABLED
from ..security import check_credentials, require_admin

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["gui"])

PAGES = {
    "/": ("dashboard.html", "Dashboard", "dashboard"),
    "/portals": ("portals.html", "Portals", "portals"),
    "/sources": ("sources.html", "Input Sources", "sources"),
    "/playlist": ("playlist.html", "Playlist Builder", "playlist"),
    "/ffmpeg": ("ffmpeg.html", "FFmpeg Templates", "ffmpeg"),
    "/users": ("users.html", "User Management", "users"),
    "/settings": ("settings.html", "Settings", "settings"),
}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    if check_credentials(username, password):
        request.session["admin"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"error": "Invalid username or password"},
                                      status_code=401)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# "/" itself is handled by the app-level dispatcher in main.py (xtream vs GUI);
# browsers land here via its redirect.
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "dashboard.html", _ctx(request, "dashboard", "Dashboard"))


@router.get("/portals", response_class=HTMLResponse)
async def portals(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "portals.html", _ctx(request, "portals", "Portals"))


@router.get("/sources", response_class=HTMLResponse)
async def sources(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "sources.html", _ctx(request, "sources", "Input Sources"))


@router.get("/playlist", response_class=HTMLResponse)
async def playlist(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "playlist.html", _ctx(request, "playlist", "Playlist Builder"))


@router.get("/ffmpeg", response_class=HTMLResponse)
async def ffmpeg(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "ffmpeg.html", _ctx(request, "ffmpeg", "FFmpeg Templates"))


@router.get("/users", response_class=HTMLResponse)
async def users(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "users.html", _ctx(request, "users", "User Management"))


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "settings.html", _ctx(request, "settings", "Settings"))


def _ctx(request: Request, active: str, title: str) -> dict:
    mock_base = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    return {"request": request, "active": active, "title": title,
            "admin": request.session.get("admin", ""), "port": HTTP_PORT,
            "mock_enabled": MOCK_PORTAL_ENABLED,
            "mock_portal_url": f"{mock_base}/mock/c/",
            "mock_macs": "00:1A:79:AA:AA:01, 00:1A:79:AA:AA:02"}
