"""
Admin GUI authentication (Phase-1 decision Q5=A).

* Stateless signed session cookie (itsdangerous via Starlette SessionMiddleware
  is configured in main.py; here we just stamp/verify `request.session`).
* All /api/* and GUI pages require it; the OUTPUT endpoints (m3u, xtream,
  streams, xmltv), /static, /login and /mock do not (they have their own
  per-user credential checks).
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from .config import ADMIN_PASSWORD, ADMIN_USERNAME, SKIP_LOGIN


def check_credentials(username: str, password: str) -> bool:
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


def require_admin(request: Request) -> str:
    """FastAPI dependency - raises 401 JSON (API) handled by GUI JS redirect."""
    if SKIP_LOGIN:                                   # mockup mode: no login wall
        return ADMIN_USERNAME
    user = request.session.get("admin")
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user
