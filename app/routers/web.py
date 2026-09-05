"""
Server-rendered GUI pages. Heavy data loads happen client-side via the REST
APIs (fast first paint; pages stay light). All pages require the admin session
except /login.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import ADMIN_USERNAME, HTTP_PORT, MOCK_PORTAL_ENABLED
from ..security import check_credentials, require_admin
from ..services import branding

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["gui"])


def _static_versions() -> dict:
    """Per-boot fingerprint (size-mtime) of the GUI's own assets, used as a
    cache-busting ?v= on the <script>/<link> tags in base.html.

    Without it browsers keep serving a WEEKS-old cached app.js after an
    upgrade, and 'fixed' bugs (like the preview player rewrite) look unfixed
    because the page still runs the old code. mtime+size changes whenever the
    file changes; the value stays stable across requests so the URL (and its
    cache entry) only rotates when the asset really did."""
    import hashlib

    out: dict[str, str] = {}
    for key, rel in (("js", "app/static/js/app.js"),
                     ("css", "app/static/css/app.css")):
        try:
            st = os.stat(rel)
            out[key] = hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:10]
        except OSError:
            out[key] = "dev"
    return out


templates.env.globals["static_v"] = _static_versions()
# Tab icon <link> tags for every page, including /login: the choice lives in the
# `favicon` settings row, so it must be read at render time, not at import.
templates.env.globals["favicon_tags"] = branding.favicon_tags

PAGES = {
    "/": ("dashboard.html", "Dashboard", "dashboard"),
    "/portals": ("portals.html", "Portals", "portals"),
    "/sources": ("sources.html", "Input Sources", "sources"),
    "/playlist": ("playlist.html", "Playlist Builder", "playlist"),
    "/ffmpeg": ("ffmpeg.html", "FFmpeg Templates", "ffmpeg"),
    "/users": ("users.html", "User Management", "users"),
    "/areas": ("areas.html", "Areas", "areas"),
    "/enigma2": ("enigma2.html", "Enigma2 Receivers", "enigma2"),
    "/settings": ("settings.html", "Settings", "settings"),
}


# browser redirect target "/" (dispatched by main.py::root_dispatch); with
# SKIP_LOGIN active we never show the login form at all.
from ..config import SKIP_LOGIN  # noqa: E402


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if SKIP_LOGIN:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    if SKIP_LOGIN or check_credentials(username, password):
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


@router.get("/areas", response_class=HTMLResponse)
async def areas(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "areas.html", _ctx(request, "areas", "Areas"))


@router.get("/enigma2", response_class=HTMLResponse)
async def enigma2(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "enigma2.html",
                                      _ctx(request, "enigma2", "Enigma2 Receivers"))


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, admin=Depends(require_admin)):
    return templates.TemplateResponse(request, "settings.html", _ctx(request, "settings", "Settings"))


def _ctx(request: Request, active: str, title: str) -> dict:
    mock_base = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    return {"request": request, "active": active, "title": title,
            "admin": request.session.get("admin", ""), "port": HTTP_PORT,
            "mock_enabled": MOCK_PORTAL_ENABLED,
            "mock_portal_url": f"{mock_base}/mock/c/",
            # the demo portal deliberately contains one MAC per account state the
            # portal can report (active / active / expired / banned), so *Check
            # Portal* shows the whole status vocabulary without a real panel
            "mock_macs": ("00:1A:79:AA:AA:01, 00:1A:79:AA:AA:02, "
                          "00:1A:79:BB:BB:01, 00:1A:79:CC:CC:01")}
