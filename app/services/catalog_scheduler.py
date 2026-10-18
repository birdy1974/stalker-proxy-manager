"""Optional, restart-safe background checks of enabled source genres.

A check uses the normal portal item-fetch job; it is a full catalogue walk of
all enabled genres, not a cheap count-only probe. The interval defaults to off,
and the scheduler submits one portal at a time through the existing serialized
fetch worker.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select

from ..database import SessionLocal
from ..models import LiveGenre, Portal, SerieGenre, VodGenre
from .catalog_changes import aware_utc
from .runtime_settings import get_setting

log = logging.getLogger("spm.catalog")
_SCHEDULE_LOCK = asyncio.Lock()


def _interval(value) -> int:
    try:
        if isinstance(value, bool):
            return 0
        hours = int(value)
        return hours if 0 <= hours <= 168 else 0
    except (TypeError, ValueError):
        return 0


async def schedule_info() -> dict:
    """Current global interval. Zero means manual checks only."""
    hours = _interval(await get_setting("catalog_refresh_hours", 0))
    return {"hours": hours, "enabled": hours > 0}


def _active_fetch_exists() -> bool:
    # The fetch worker is intentionally single-threaded. Do not fill its queue
    # with a second automatic crawl while a manual fetch or check is underway.
    from .fetch_jobs import JOBS

    return any(j.status in ("queued", "running")
               and j.kind in ("fetch_portal", "fetch_genres", "fetch_items",
                              "catalog_check", "fetch_seasons")
               for j in JOBS.values())


async def refresh_due_once() -> dict:
    """Queue at most one due enabled portal, or explain why no work was queued."""
    async with _SCHEDULE_LOCK:
        schedule = await schedule_info()
        if not schedule["enabled"]:
            return {"queued": False, "reason": "paused", "hours": schedule["hours"]}
        if _active_fetch_exists():
            return {"queued": False, "reason": "fetch busy", "hours": schedule["hours"]}

        now = datetime.now(timezone.utc)
        interval = timedelta(hours=schedule["hours"])
        has_genres = or_(
            exists(select(LiveGenre.id).where(
                LiveGenre.portal_id == Portal.id, LiveGenre.enabled.is_(True))),
            exists(select(VodGenre.id).where(
                VodGenre.portal_id == Portal.id, VodGenre.enabled.is_(True))),
            exists(select(SerieGenre.id).where(
                SerieGenre.portal_id == Portal.id, SerieGenre.enabled.is_(True))),
        )
        async with SessionLocal() as db:
            portals = (await db.execute(
                select(Portal).where(Portal.enabled.is_(True), has_genres)
                .order_by(Portal.id)
            )).scalars().all()
            due = []
            for portal in portals:
                last = aware_utc(portal.catalog_last_attempt)
                if last is None or now - last >= interval:
                    due.append((last or datetime.min.replace(tzinfo=timezone.utc), portal.id))
            if not due:
                return {"queued": False, "reason": "not due", "hours": schedule["hours"]}
            _, portal_id = min(due)

        # `submit` also guards against a manual fetch racing this scheduler. If
        # it returns a different job kind, that existing job owns this portal
        # and no automatic interval is consumed.
        from .fetch_jobs import submit
        job = await submit("catalog_check", portal_id)
        if job.kind != "catalog_check":
            return {"queued": False, "reason": "portal busy", "job": job.public(),
                    "hours": schedule["hours"]}

        async with SessionLocal() as db:
            portal = await db.get(Portal, portal_id)
            if portal is not None:
                portal.catalog_last_attempt = now
                await db.commit()
        return {"queued": True, "portal_id": portal_id, "job": job.public(),
                "hours": schedule["hours"]}


async def catalog_scheduler(parse: float = 60.0) -> None:
    """Re-read Settings each minute; zero pauses automatic scans without restart."""
    while True:
        await asyncio.sleep(parse)
        try:
            result = await refresh_due_once()
            if result.get("queued"):
                log.info("scheduled catalogue check queued for portal %s", result["portal_id"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad tick must not kill future scans
            log.exception("catalogue scheduler tick failed")
