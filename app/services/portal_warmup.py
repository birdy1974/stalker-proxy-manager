"""Proactively authenticate enabled portal/MAC sessions for fast first play."""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select

from ..database import SessionLocal
from ..models import MacAddress, Portal
from ..portal.account import mac_is_usable
from ..portal.pool import POOL, PortalSession

log = logging.getLogger("spm.portal.warmup")
WARM_INTERVAL = float(os.environ.get("SPM_PORTAL_WARM_INTERVAL", "600"))
WARM_CONCURRENCY = max(1, int(os.environ.get("SPM_PORTAL_WARM_CONCURRENCY", "4")))


async def warm_portal_sessions() -> dict:
    """Authenticate usable MACs whose portals already have a resolved endpoint."""
    from .stream_manager import MANAGER  # avoid module cycle during app import

    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Portal, MacAddress)
            .join(MacAddress, MacAddress.portal_id == Portal.id)
            .where(Portal.enabled.is_(True), Portal.resolved_url.is_not(None))
            .order_by(Portal.id, MacAddress.order)
        )).all()
    sessions = [
        PortalSession.from_rows(portal, mac)
        for portal, mac in rows
        if mac_is_usable(mac.status) and not MANAGER.is_mac_busy(mac.id)
    ]
    result = await POOL.warm(sessions, concurrency=WARM_CONCURRENCY)
    result["eligible"] = len(sessions)
    if sessions:
        log.info("portal pre-auth: %d ready, %d failed", result["ready"], result["failed"])
    return result


async def portal_warmup_scheduler(interval: float = WARM_INTERVAL) -> None:
    """Warm immediately, then refresh activity/tokens before idle reaping."""
    while True:
        try:
            await warm_portal_sessions()
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background optimization must survive
            log.exception("portal pre-auth scheduler failed")
            await asyncio.sleep(interval)
