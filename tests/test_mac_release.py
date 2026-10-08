"""MAC occupancy that the portal tab shows as "streaming" / "leased".

The Portals table reads `MANAGER.mac_occupancy()`: ffmpeg pipe locks plus
post-302 redirect leases. The dashboard reads the stream registry. They can
disagree, and two things made them do it:

* a client that left while ffmpeg was still waiting for its first byte ended
  the play generator with no process and no registry entry, so the MAC lock
  taken just before was never released (a ghost "streaming" MAC with an empty
  holder, invisible on the dashboard). Fixed in the pump's finally.
* the operator had no way to free a MAC short of a container restart. The
  release actions below stop what really holds it and clear the ghosts.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.database import SessionLocal
from app.models import MacAddress, Portal
from app.services.stream_manager import MANAGER, StreamHandle
from tests.test_fallback_reconnect import _clean, _wire  # noqa: F401  (fixture + stubs)
from tests.test_mac_availability import _live_route

MAC_A = "00:1A:79:00:00:01"


def _handle(user="bert", mac=MAC_A) -> StreamHandle:
    return StreamHandle(id=uuid.uuid4().hex, kind="live", item_name="Npo 1",
                        user_name=user, template_name="copy", command="ffmpeg {url}",
                        portal_name="nexus", mac=mac)


async def test_a_start_cancelled_before_its_first_byte_leaves_no_lock(monkeypatch):
    """Regression: the reproduction that showed a ghost 'streaming' MAC."""
    pl, rows = await _live_route(macs=(MAC_A,))
    _wire(monkeypatch, [])

    async def never_a_byte(self, command, url, *, title, pace, first_byte_timeout=None):
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.stream_manager.StreamManager._open_with_identity",
                        never_a_byte)
    _h, gen = await MANAGER.open("live", pl, "bert")
    task = asyncio.ensure_future(gen.__anext__())
    for _ in range(300):                      # wait until the MAC is taken
        await asyncio.sleep(0.01)
        if MANAGER.mac_locks:
            break
    assert MANAGER.mac_locks, "precondition: the start should hold the MAC"

    task.cancel()                             # the client went away
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await task
    await asyncio.sleep(0.05)

    assert MANAGER.streams == {}
    assert MANAGER.mac_locks == {}, "a cancelled start must not keep the MAC locked"
    assert MANAGER.mac_occupancy(rows[0])["busy"] is False


async def test_release_one_mac_stops_its_pipe_and_clears_ghosts_and_leases(monkeypatch):
    MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    h = _handle()
    MANAGER.streams[h.id] = h
    MANAGER.lock_mac(7, h.id)                 # a real pipe on MAC 7
    MANAGER.lock_mac(7, "ghost-stream")       # a stale lock on MAC 7
    MANAGER.lease_mac(7, holder="bert", item="Rai 1", kind="live", ref=3)
    MANAGER.lock_mac(8, "other-ghost")        # another MAC must not be touched
    try:
        out = await MANAGER.release_mac_occupancy(7)
        assert out == {"mac_id": 7, "streams_killed": 1, "ghost_locks": 1,
                       "lease_released": True}
        assert h.id not in MANAGER.streams and h.dead
        assert 7 not in MANAGER.mac_locks and 7 not in MANAGER.redirect_leases
        assert MANAGER.mac_occupancy(7)["busy"] is False
        assert MANAGER.mac_locks == {8: {"other-ghost"}}, "only MAC 7 is released"
    finally:
        MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
        MANAGER.lease_meta.clear()


async def test_release_all_frees_every_busy_mac():
    MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    h1, h2 = _handle(mac="00:1A:79:00:00:01"), _handle(mac="00:1A:79:00:00:02")
    MANAGER.streams[h1.id] = h1
    MANAGER.streams[h2.id] = h2
    MANAGER.lock_mac(1, h1.id)
    MANAGER.lock_mac(2, h2.id)
    MANAGER.lock_mac(3, "ghost")
    MANAGER.lease_mac(4, holder="anna", item="x")
    try:
        out = await MANAGER.release_all_occupancy()
        assert out["macs"] == 4
        assert out["streams_killed"] == 2
        assert out["ghost_locks"] == 1
        assert out["leases_released"] == 1
        assert MANAGER.streams == {}
        assert MANAGER.mac_locks == {} and MANAGER.redirect_leases == {}
        assert MANAGER.busy_mac_ids() == set()
    finally:
        MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
        MANAGER.lease_meta.clear()


async def test_release_all_when_nothing_is_busy_is_a_no_op():
    MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
    out = await MANAGER.release_all_occupancy()
    assert out["macs"] == 0 and out["streams_killed"] == 0


async def _portal_with_two_macs():
    async with SessionLocal() as s:
        p = Portal(name="nexus", base_url="http://127.0.0.1:1/c/",
                   resolved_url="http://127.0.0.1:1/c/")
        other = Portal(name="other", base_url="http://127.0.0.1:2/c/",
                       resolved_url="http://127.0.0.1:2/c/")
        s.add_all([p, other])
        await s.flush()
        m1 = MacAddress(portal_id=p.id, mac=MAC_A, status="online", order=0)
        m2 = MacAddress(portal_id=other.id, mac="00:1A:79:00:00:09", status="online", order=0)
        s.add_all([m1, m2])
        await s.commit()
        return p.id, m1.id, m2.id


async def test_api_release_one_mac_is_scoped_to_its_portal():
    from app.main import app

    pid, mid, foreign_mid = await _portal_with_two_macs()
    MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
    MANAGER.lock_mac(mid, "ghost")
    MANAGER.lock_mac(foreign_mid, "ghost-elsewhere")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            wrong = await c.post(f"/api/portals/{pid}/macs/{foreign_mid}/release")
            assert wrong.status_code == 404, "a MAC of another portal is not this portal's"
            assert MANAGER.mac_locks.get(foreign_mid) == {"ghost-elsewhere"}

            r = await c.post(f"/api/portals/{pid}/macs/{mid}/release")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True and body["mac"] == MAC_A
            assert body["ghost_locks"] == 1
            assert mid not in MANAGER.mac_locks
    finally:
        MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()


async def test_api_release_all_answers_with_a_summary():
    from app.main import app

    MANAGER.streams.clear(); MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
    MANAGER.lock_mac(41, "ghost-a")
    MANAGER.lock_mac(42, "ghost-b")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/api/portals/macs/release-all")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["macs"] == 2 and body["ghost_locks"] == 2
        assert MANAGER.mac_locks == {}
    finally:
        MANAGER.mac_locks.clear(); MANAGER.redirect_leases.clear()
