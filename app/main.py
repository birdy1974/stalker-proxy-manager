"""
Stalker Proxy Manager - application entrypoint.

Startup order:
  1. create tables (idempotent)
  2. purge active_streams rows left by a previous container run
  3. reconcile built-in ffmpeg templates + default settings (every boot)
  4. log retention cleanup
  5. start background fetch-job worker (implicit via submit)
Everything is also mirrored to stdout, so Portainer shows the full story.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from .config import (ACCESS_LOG, ACCESS_LOG_SKIP, HTTP_PORT, LOG_LEVEL,
                     MOCK_PORTAL_ENABLED, SECRET_KEY, SESSION_MAX_AGE, log)
from .database import SessionLocal, init_db
from .models import FFmpegTemplate, Setting
from .services import api_stats
from .services.db_logging import cleanup_logs, db_log, stop_log_writer
from .services.ffmpeg_templates import default_presets
from .services.stream_manager import MANAGER

# uvicorn configures its own logging *before* it imports this module, and it
# sends its 'default' handler to stderr (only the access log uses stdout).
# Re-attach every uvicorn handler to stdout so the container emits ONE log
# stream: `docker logs <c> | grep ...` then sees the whole story, and Portainer
# keeps the true chronological order instead of splitting records over two
# panes. (Python's own loggers are configured in config.py for the same reason.)
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    for _h in logging.getLogger(_name).handlers:
        if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler) \
                and _h.stream is not sys.stdout:
            _h.setStream(sys.stdout)

# A media proxy answers a lot of small requests (HLS segments, previews,
# reconnecting streams); per-request access lines drown out the messages that
# actually matter here, so they stay at WARNING. Meaningful events are logged
# explicitly (module-tagged, and mirrored into the GUI log pane) instead.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Strong references to the app's own background tasks: asyncio only keeps weak
# ones, so an unheld task can be garbage-collected mid-flight.
_bg: set = set()

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
    # Xtream players probe "/?username=&password=" for identity. UserAuth.verify
    # takes (username, password, need) and returns User | None — a previous
    # call passed the DB session as the username and unpacked a (ok, user)
    # tuple, so every probe 500'd.
    from .services.playlist_gen import UserAuth, xtream_base
    user = await UserAuth.verify(u or "", p or "", "xtream")
    if not user:
        return JSONResponse({"user_info": {"auth": 0, "status": "Disabled"}}, status_code=401)
    from .routers.output import base_url_of
    return JSONResponse(await xtream_base(user, await base_url_of(request)))

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=SESSION_MAX_AGE,
                   same_site="lax", https_only=False)

_api_log = logging.getLogger("spm.api")


@app.middleware("http")
async def access_log(request: Request, call_next):
    """
    One stdout line per request + the counters behind the dashboard's API card.

    uvicorn's access log is deliberately at WARNING (see the note above), which
    is why the container log used to show nothing at all about API traffic.
    This replaces it with the app's own format - method, path, status, time to
    first byte - on stdout, where `docker logs` and the GUI log pane both look.

    For streaming responses the response object is returned as soon as the
    first byte is on its way, so `ms` is time-to-first-byte, not stream
    duration; the stream manager logs start/stop separately.
    """
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        ms = (time.perf_counter() - started) * 1000.0
        path = request.url.path
        api_stats.record(request.method, path, status, ms,
                         request.client.host if request.client else "")
        if ACCESS_LOG and not path.startswith(ACCESS_LOG_SKIP):
            qs = f"?{request.url.query}" if request.url.query else ""
            _api_log.info("%s %s%s -> %d (%.0f ms)",
                          request.method, path, qs[:120], status, ms)

# --------------------------------------------------------------- sub-routers
from .routers import (api_areas, api_branding, api_enigma2, api_ffmpeg, api_epg,  # noqa: E402
                      api_misc, api_playlist, api_portals, api_sources, api_users,
                      output, web)

app.include_router(web.router)
app.include_router(output.router)
for r in (api_portals.router, api_sources.router, api_playlist.router,
          api_ffmpeg.router, api_users.router, api_areas.router, api_misc.router,
          api_epg.router, api_enigma2.router, api_enigma2.public,
          api_branding.router, api_branding.public):
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
    # tab icon: prime the ?v= fingerprint the page templates stamp on <link>
    from .services.branding import refresh as refresh_favicon
    await refresh_favicon()
    await _heal_season_links()
    await _hardware_sanity()
    await db_log("INFO", "boot",
                 f"Stalker Proxy Manager v2.0.0-phase2 started on port {HTTP_PORT} "
                 f"(mock portal: {'on' if MOCK_PORTAL_ENABLED else 'off'})")
    from .config import SKIP_LOGIN
    if SKIP_LOGIN:
        await db_log("ERROR", "boot",
                     "*** LOGIN DISABLED (SPM_SKIP_LOGIN=1) - mockup/preview mode ***")
    # Phase 3: background EPG refresher (checks due sources hourly)
    from .services.epg import epg_scheduler
    import asyncio  # noqa: PLC0415 - the task set is built at startup
    asyncio.create_task(epg_scheduler())
    # Multi-MAC portals: keep every MAC's status + expiry fresh in the background
    # so a secondary account that expires overnight is dropped from fallback
    # chains without anyone pressing Test. Interval is the GUI setting
    # `mac_health_minutes` (0 = paused).
    from .services.mac_health import mac_health_scheduler
    _bg.add(asyncio.create_task(mac_health_scheduler(), name="spm-mac-health"))
    # Reaps streams whose teardown was lost, so a MAC or a user's connection
    # slot can never stay occupied until the next restart.
    _bg.add(asyncio.create_task(MANAGER.reap_dead(), name="spm-stream-reaper"))
    _bg.add(asyncio.create_task(_reap_sessions(), name="spm-session-reaper"))
    # Authenticate resolved portal/MAC sessions before a player asks for its
    # first link; periodic refresh also keeps healthy sessions ahead of expiry.
    from .services.portal_warmup import portal_warmup_scheduler
    _bg.add(asyncio.create_task(portal_warmup_scheduler(), name="spm-portal-warmup"))


async def _reap_sessions(interval: float = 300.0) -> None:
    """Close portal sessions nothing has asked for in a while (keeps the pool
    from holding a socket open per MAC forever).

    `asyncio` has to be a module-level import for this to run at all: it used to
    be imported only inside the startup function, so every pass of this loop died
    with `NameError` inside the `except Exception` below - the reaper logged
    "session reaper failed" every 300 s and never closed a session, which is
    exactly the leak the loop exists to prevent. The interval is a parameter
    rather than a literal so a test can watch one pass.
    """
    from .portal.pool import POOL
    while True:
        try:
            await asyncio.sleep(interval)
            await POOL.reap()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must not die
            log.exception("session reaper failed")


@app.on_event("shutdown")
async def shutdown() -> None:
    # Stop schedulers before closing their database/HTTP resources. In
    # particular, portal warmup must not authenticate a new client while the
    # pool is being torn down.
    tasks = list(_bg)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _bg.clear()
    # Log rows live on a queue drained by a writer task; the pool is torn down
    # right after this, so anything still queued has to be committed now.
    await stop_log_writer()
    from .portal.pool import POOL
    await POOL.close_all()


async def _heal_season_links() -> None:
    """
    One-shot reconciliation at boot: any playlist-series whose seasons were
    fetched after it was added gets its season link rows, so it actually
    appears in the output. Idempotent and cheap (three queries).
    """
    from .services.playlist_sync import sync_season_links
    try:
        async with SessionLocal() as s:
            added = await sync_season_links(s)
        if added:
            await db_log("INFO", "boot",
                         f"linked {added} season(s) to playlist series that had none")
    except Exception:  # noqa: BLE001 - boot must not die on bookkeeping
        log.exception("season link reconciliation failed")


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
    """
    Built-in ffmpeg templates (defaults) live in the DATABASE, not in code.

    These are the "default templates" the user can always rely on: they are
    (re)seeded on EVERY boot so they survive deletion and pick up updates
    (e.g. the VAAPI/Dreambox tuning). Rows are matched by name, so a user who
    edits a built-in keeps their edits; deleting one brings it back at the next
    boot. The "Redirect (bypass ffmpeg)" preset is the built-in default:
    reconciling it on every boot means items without an explicit template
    assignment are redirected to the panel CDN (no ffmpeg), and streams never
    fall back to nothing. Default settings rows are seeded alongside
    (idempotent).
    """
    from .services.ffmpeg_templates import (FFmpegOptions, REDIRECT_PRESET_NAME,
                                             REFERENCE_PRESET_NAME)
    async with SessionLocal() as s:
        for p in default_presets():
            name = p.pop("name")
            row = (await s.execute(select(FFmpegTemplate).where(
                FFmpegTemplate.name == name))).scalar_one_or_none()
            if row is None:
                s.add(FFmpegTemplate(
                    name=name, is_builtin=True,
                    is_default=(name == REDIRECT_PRESET_NAME),
                    **{k: p[k] for k in FFmpegOptions.__dataclass_fields__},
                    command=p["command"], command_source=p["command_source"],
                    enabled=True))
                continue
            # Built-in already exists: refresh identity + structured fields and
            # command, but only from the FIELDS side - a user's manual command
            # text stays untouched (their edits win).
            row.is_builtin = True
            if row.command_source == "fields":
                row.command = p["command"]
            for k, v in p.items():
                if k in FFmpegOptions.__dataclass_fields__ and k not in ("name",):
                    setattr(row, k, v)
        # "Redirect (bypass ffmpeg)" is the built-in default. Reconcile it on
        # every boot (like the other built-in fields), so installs that ran an
        # earlier build - where "VAAPI 720p ~1M" was the fallback default -
        # switch over too. A default that sits on a USER-created template
        # (is_builtin=False) is a deliberate choice and is left untouched.
        default = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.is_default.is_(True)))).scalar_one_or_none()
        redir = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REDIRECT_PRESET_NAME))).scalar_one_or_none()
        if redir is not None and (default is None or default.is_builtin):
            if default is not None and default.id != redir.id:
                default.is_default = False
            redir.is_default = True
        elif default is None:
            # redirect preset missing (cannot happen - it is re-seeded above,
            # unless the user deleted it and the loop somehow skipped it): keep
            # at least ONE enabled template default so streams never fall back
            # to nothing.
            ref = (await s.execute(select(FFmpegTemplate).where(
                FFmpegTemplate.name == REFERENCE_PRESET_NAME))).scalar_one_or_none()
            ref = ref or (await s.execute(select(FFmpegTemplate).where(
                FFmpegTemplate.enabled.is_(True)).limit(1))).scalar_one_or_none()
            if ref is not None:
                ref.is_default = True
        await s.commit()

    async with SessionLocal() as s:
        from .routers.api_misc import DEFAULT_SETTINGS
        for k, v in DEFAULT_SETTINGS.items():
            if await s.get(Setting, k) is None:
                s.add(Setting(key=k, value=__import__("json").dumps(v)))
        await s.commit()
