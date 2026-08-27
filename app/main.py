"""
Stalker Proxy Manager - application entrypoint.

Startup order:
  1. create tables (idempotent)
  2. purge active_streams rows left by a previous container run
  3. seed default ffmpeg templates + settings on first boot
  4. log retention cleanup
  5. start background fetch-job worker (implicit via submit)
Everything is also mirrored to stdout, so Portainer shows the full story.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .config import (HTTP_PORT, LOG_LEVEL, MOCK_PORTAL_ENABLED, SECRET_KEY,
                     SESSION_MAX_AGE, log)
from .database import SessionLocal, init_db
from .models import FFmpegTemplate, Setting
from .services.db_logging import cleanup_logs, db_log
from .services.ffmpeg_templates import default_presets
from .services.stream_manager import MANAGER

logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="Stalker Proxy Manager", version="2.0.0-phase2",
              docs_url=None, redoc_url=None, openapi_url=None)


@app.head("/", include_in_schema=False)
@app.get("/", include_in_schema=False)
async def root_dispatch(request: Request):
    """
    Single-owner root route (registered before routers so it wins "/"):
      - no credentials               -> browser visitor  -> redirect to GUI
      - credentials present
          valid xtream user          -> player_api-style JSON identity
          invalid                    -> plain 401 JSON (never an HTML page)
    """
    from fastapi.responses import RedirectResponse
    u, p = request.query_params.get("username"), request.query_params.get("password")
    if u is None and p is None:
        return RedirectResponse("/dashboard", status_code=303)
    from .services.playlist_gen import UserAuth
    async with SessionLocal() as db:
        ok, user = await UserAuth.verify(db, u or "", p or "", "xtream")
    if not ok:
        return JSONResponse({"user_info": {"auth": 0, "status": "Disabled"}}, status_code=401)
    expire = int(user.expire_date.timestamp()) if user.expire_date else None
    return JSONResponse({"user_info": {"username": user.username, "password": user.password,
                                       "message": "", "auth": 1,
                                       "status": "Active" if user.enabled else "Disabled",
                                       "exp_date": str(expire) if expire else None,
                                       "is_trial": "0", "active_cons": str(MANAGER.user_stream_count(user.username)),
                                       "max_connections": str(user.max_connections)}})

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_MAX_AGE,
                   same_site="lax", https_only=False)

# --------------------------------------------------------------- sub-routers
from .routers import api_ffmpeg, api_misc, api_playlist, api_portals, api_sources, api_users, output, web  # noqa: E402

app.include_router(web.router)
app.include_router(output.router)
for r in (api_portals.router, api_sources.router, api_playlist.router,
          api_ffmpeg.router, api_users.router, api_misc.router):
    app.include_router(r)

if MOCK_PORTAL_ENABLED:
    from .portal.mock_portal import router as mock_router
    app.include_router(mock_router)
    log.info("mock portal ENABLED at /mock/c/")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.exception_handler(404)
async def not_found(request: Request, exc):  # noqa: ANN001
    # Only GUI page misses redirect to "/"; machine-facing paths must stay honest.
    p = request.url.path
    if (p.startswith("/api/") or p.startswith("/play/") or p.startswith("/preview")
            or p.endswith(".php") or p.count("/") >= 3):
        return JSONResponse({"detail": getattr(exc, "detail", "not found")}, status_code=404)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/")


@app.exception_handler(401)
async def unauthorized(request: Request, exc):  # noqa: ANN001
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/login")


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    await MANAGER.purge_runtime_rows()
    await cleanup_logs()
    await _seed_defaults()
    await _hardware_sanity()
    await db_log("INFO", "boot",
                 f"Stalker Proxy Manager v2.0.0-phase2 started on port {HTTP_PORT} "
                 f"(mock portal: {'on' if MOCK_PORTAL_ENABLED else 'off'})")


async def _hardware_sanity() -> None:
    """
    If the default ffmpeg template needs a GPU device that is NOT mapped into
    this container (no /dev/dri/renderD128), every item without an explicit
    template would die instantly in ffmpeg. Degrade the default to the copy
    template with a loud boot warning instead of silent broken streams. On the
    DS918+ system /dev/dri exists and nothing changes.
    """
    import os
    from .config import VAAPI_DEVICE
    async with SessionLocal() as s:
        default = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.is_default.is_(True)))).scalar_one_or_none()
        if default and ("-hwaccel vaapi" in (default.command or "")
                        or "hwaccel=qsv" in (default.command or "")) \
                and not os.path.exists(VAAPI_DEVICE):
            passthrough = (await s.execute(select(FFmpegTemplate).where(
                FFmpegTemplate.name.like("%Copy%")))).scalars().first() or default
            default.is_default = False
            passthrough.is_default = True
            await s.commit()
            await db_log("WARNING", "boot",
                         f"VAAPI device {VAAPI_DEVICE} not present, but default template "
                         f"'{default.name}' needs it -> default switched to "
                         f"'{passthrough.name}'. Pass /dev/dri into the container and "
                         "re-set the default in the FFmpeg tab to use Quick Sync.")


async def _seed_defaults() -> None:
    """First boot: default ffmpeg presets + default settings row."""
    async with SessionLocal() as s:
        have = (await s.execute(select(FFmpegTemplate).limit(1))).scalar_one_or_none()
        if have is None:
            from .services.ffmpeg_templates import FFmpegOptions
            for p in default_presets():
                name = p.pop("name")
                s.add(FFmpegTemplate(name=name, is_default=name.startswith("VAAPI 720p"), **{
                    "hw_accel": p["hw_accel"], "device": p["device"],
                    "resolution": p["resolution"], "aspect": p["aspect"],
                    "video_codec": p["video_codec"], "video_bitrate": p["video_bitrate"],
                    "maxrate": p["maxrate"], "bufsize": p["bufsize"], "fps": p["fps"],
                    "gop": p["gop"], "profile": p["profile"], "level": p["level"],
                    "audio_codec": p["audio_codec"], "audio_bitrate": p["audio_bitrate"],
                    "audio_channels": p["audio_channels"], "audio_rate": p["audio_rate"],
                    "output_format": p["output_format"], "extra_input": p["extra_input"],
                    "extra_output": p["extra_output"], "command": p["command"],
                    "command_source": p["command_source"],
                }))
            log.info("seeded %d default ffmpeg templates", len(default_presets()))
        from .routers.api_misc import DEFAULT_SETTINGS
        for k, v in DEFAULT_SETTINGS.items():
            if await s.get(Setting, k) is None:
                s.add(Setting(key=k, value=__import__("json").dumps(v)))
        await s.commit()
