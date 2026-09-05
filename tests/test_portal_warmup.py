"""Background portal pre-authentication selects safe, usable sessions."""

from __future__ import annotations

from app.database import SessionLocal
from app.models import MacAddress, Portal
from app.portal.pool import POOL
from app.services.portal_warmup import warm_portal_sessions
from app.services.stream_manager import MANAGER


async def test_warmup_uses_resolved_usable_nonbusy_macs(monkeypatch):
    async with SessionLocal() as session:
        ready = Portal(name="ready", base_url="http://ready/c/",
                       resolved_url="http://ready/c/portal.php", enabled=True)
        unresolved = Portal(name="unresolved", base_url="http://new/c/", enabled=True)
        session.add_all([ready, unresolved])
        await session.flush()
        first = MacAddress(portal_id=ready.id, mac="00:1A:79:00:10:01",
                           status="online", online=True, order=0)
        busy = MacAddress(portal_id=ready.id, mac="00:1A:79:00:10:02",
                          status="online", online=True, order=1)
        unusable = MacAddress(portal_id=ready.id, mac="00:1A:79:00:10:03",
                              status="banned", online=False, order=2)
        not_resolved = MacAddress(portal_id=unresolved.id, mac="00:1A:79:00:10:04",
                                  status="online", online=True)
        session.add_all([first, busy, unusable, not_resolved])
        await session.commit()
        busy_id = busy.id

    captured = []

    async def fake_warm(sessions, concurrency):
        captured.extend(sessions)
        return {"ready": len(sessions), "failed": 0}

    monkeypatch.setattr(POOL, "warm", fake_warm)
    monkeypatch.setattr(MANAGER, "is_mac_busy", lambda mac_id: mac_id == busy_id)
    result = await warm_portal_sessions()
    assert result == {"ready": 1, "failed": 0, "eligible": 1}
    assert [session.mac for session in captured] == [first.mac]
    assert captured[0].portal_url == ready.resolved_url
