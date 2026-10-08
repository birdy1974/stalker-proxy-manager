"""
"Portals shows this MAC as streaming, the dashboard shows no active stream."

Two different failures hide behind that one report, and both are pinned here.

**1. A start that is cancelled before it produces data leaks its MAC lock.**

The engine locks a MAC *before* it spawns ffmpeg, so a second request sees it
as taken straight away. Every release of that lock used to sit on a code path
that an `await` could be cancelled out of: a player that hangs up while the
engine is still waiting for the first byte (up to STREAM_START_TIMEOUT) cancels
the request task, the `CancelledError` skips every `unlock_mac` call, and the
pump's `finally` did not run at all for a handle that had neither registered
nor spawned a process yet.

What is left is the worst state to be in, and it is permanent:

    Portals tab .. MAC shows "streaming"
    Dashboard .... no active stream (the handle never reached the registry)
    Reaper ....... only walks the registry, so it cannot see it
    Panel ........ keeps answering `limit` / "account is in use"

The same cancellation also orphaned the ffmpeg process, which is the other
half of the report: a pipe nobody owns still holds the panel's connection slot.

**2. There was no way to let a MAC go at all.**

Once a MAC is stuck - by the bug above, by a redirect lease, or by a panel that
has not timed a connection out - the operator's only remedy was restarting the
container. `services/mac_release.py` is the button, and these tests pin what it
does: it ends the streams first (so a MAC is never "released" while a viewer's
pipe is still running on it), drops the lease and stale locks, retires the
panel session with a fresh handshake, and reports the panel's own verdict.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.database import SessionLocal
from app.models import LiveSource, MacAddress, Portal
from app.services import mac_release, stream_manager as sm
from app.services.db_logging import flush_logs
from app.services.stream_manager import MANAGER, StreamHandle


class _Portal:
    name = "releaseportal"
    base_url = "http://portal.invalid/c/"
    resolved_url = "http://portal.invalid/c/portal.php"
    proxy_url = None
    tls_insecure = False


class _Mac:
    def __init__(self, id_, mac):
        self.id, self.mac, self.password = id_, mac, ""
        self.sn = None
        self.device_id = None


class _Src:
    cmd = "ffmpeg -i {url} -f mpegts pipe:1"


class _FakeStalkerClient:
    shared = False

    def __init__(self, *a, **k):
        self.portal_url = a[0] if a else ""

    async def handshake(self):
        return None

    async def ensure_auth(self):
        return None

    def invalidate(self):
        return None

    async def create_link(self, cmd, kind, **kw):
        return "http://portal.invalid/stream/1.ts"

    async def close(self):
        return None


async def _silent_spawn(self, cmd_template, url, title=None, pace=False,
                        user_agent=None):
    """An ffmpeg that never sends a byte: the pump parks in `_first_bytes`."""
    return await asyncio.create_subprocess_exec(
        "sleep", "30",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)


@pytest.fixture(autouse=True)
async def _drain_background_work():
    """Let every write this test started finish before the next test begins.

    A cancelled pump can leave a pooled DB connection or a queued log write
    behind; if that is still pending when the loop moves on, it is garbage
    collected inside some later test and reported as a dropped connection
    there (see tests/conftest.py `pool_errors`).
    """
    yield
    for _ in range(20):
        pending = [t for t in MANAGER._watchers if not t.done()]
        for h in list(MANAGER.streams.values()):
            if h.row_task is not None and not h.row_task.done():
                pending.append(h.row_task)
        if not pending:
            break
        await asyncio.sleep(0.02)
    await flush_logs()
    await asyncio.sleep(0.05)


@pytest.fixture(autouse=True)
def _clean_manager():
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()


def _handle(sid="release-test", **kw) -> StreamHandle:
    base = dict(id=sid, kind="live", item_name="NPO 1", user_name="bert",
                template_name="Copy", command="ffmpeg {url}")
    base.update(kw)
    return StreamHandle(**base)


async def _cancel_while_starting(h: StreamHandle, chain):
    """Run the pump and cancel it once it holds the MAC - a hung-up client."""
    async def consume():
        async for _ in MANAGER._pump(h, chain, "live"):
            pass

    task = asyncio.create_task(consume())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if MANAGER.mac_locks:
            break
    assert MANAGER.mac_locks, "harness: the pump never locked the MAC"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)


# ========================================================================== #
# 1. the leak
# ========================================================================== #
async def test_a_cancelled_start_leaves_no_mac_lock_behind(monkeypatch):
    """The reported symptom: Portals says streaming, dashboard says nothing.

    Cancelling a play while the engine waits for its first byte must release
    the MAC - not "eventually", and not only after a restart.
    """
    monkeypatch.setattr(sm, "LINGER_S", 0)
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    monkeypatch.setattr(sm.StreamManager, "_spawn", _silent_spawn)

    h = _handle()
    chain = [(_Src(), _Portal(), [_Mac(1, "00:1A:79:01:6D:BF")])]
    await _cancel_while_starting(h, chain)

    assert h.id not in MANAGER.streams, "a stream that never played is not a stream"
    assert MANAGER.mac_locks == {}, (
        "MAC stayed locked after the client hung up: the Portals tab shows it "
        "as streaming while the dashboard lists nothing")
    occ = MANAGER.mac_occupancy(1)
    assert occ["busy"] is False and occ["reason"] == "free"


async def test_a_cancelled_start_does_not_orphan_its_ffmpeg(monkeypatch):
    """The process is killed too - it holds the panel's connection slot.

    Before this, an ffmpeg started for a play that never produced a byte kept
    running (and kept the panel counting a connection) until the next boot
    swept it up.
    """
    monkeypatch.setattr(sm, "LINGER_S", 0)
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    monkeypatch.setattr(sm.StreamManager, "_spawn", _silent_spawn)

    h = _handle(sid="orphan-test")
    chain = [(_Src(), _Portal(), [_Mac(1, "00:1A:79:01:6D:BF")])]
    await _cancel_while_starting(h, chain)

    assert h.proc is not None, "the handle never recorded the process it spawned"
    assert h.proc.returncode is not None, (
        "ffmpeg survived the cancellation: it is still holding the panel slot")


def test_a_lock_with_no_stream_behind_it_is_named_a_ghost():
    """Not "streaming": it must not look like a stream somebody is watching."""
    MANAGER.lock_mac(5, "gone-stream")
    assert MANAGER.ghost_lock_ids() == {5: ["gone-stream"]}
    occ = MANAGER.mac_occupancy(5)
    assert occ["busy"] is True and occ["reason"] == "ghost"

    assert MANAGER.prune_ghost_locks() == {5: ["gone-stream"]}
    assert MANAGER.mac_locks == {}
    assert MANAGER.mac_occupancy(5)["busy"] is False


def test_pruning_a_ghost_does_not_touch_a_live_pipe():
    """Conservative in the other direction: a real stream is never dropped."""
    live = _handle()
    MANAGER.streams[live.id] = live
    MANAGER.lock_mac(5, live.id)
    MANAGER.lock_mac(5, "gone-stream")

    assert MANAGER.prune_ghost_locks() == {5: ["gone-stream"]}
    assert MANAGER._lock_set(5) == {live.id}
    assert MANAGER.mac_occupancy(5)["reason"] == "pipe"


async def test_the_reaper_clears_ghosts_a_running_instance_already_has():
    """Self-healing for installs that are stuck right now, without a restart."""
    MANAGER.lock_mac(9, "gone-stream")
    assert MANAGER.ghost_lock_ids() == {9: ["gone-stream"]}

    task = asyncio.create_task(MANAGER.reap_dead(interval=0.01))
    try:
        for _ in range(100):
            if not MANAGER.ghost_lock_ids():
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert MANAGER.mac_locks == {}


# ========================================================================== #
# 2. releasing a MAC
# ========================================================================== #
def _db_rows(n_macs: int = 2):
    """A portal with `n_macs` MAC rows, as the release endpoints see them."""
    async def build():
        async with SessionLocal() as s:
            p = Portal(name="releaseportal", base_url="http://portal.invalid/c/",
                       resolved_url="http://portal.invalid/c/portal.php",
                       enabled=True)
            s.add(p)
            await s.flush()
            macs = []
            for i in range(n_macs):
                m = MacAddress(portal_id=p.id, mac=f"00:1A:79:AA:BB:0{i + 1}",
                               order=i, status="online", online=True)
                s.add(m)
                await s.flush()
                macs.append(m)
            s.add(LiveSource(portal_id=p.id, portal_channel_id="1",
                             original_name="NPO 1", cmd="ffmpeg http://x/1.ts",
                             enabled=True))
            await s.commit()
            return p, macs
    return build


async def test_release_kills_the_pipe_it_reports():
    """`free_mac` ends the streams before it calls the MAC released."""
    h = _handle()
    MANAGER.streams[h.id] = h
    MANAGER.lock_mac(1, h.id)
    proc = await asyncio.create_subprocess_exec(
        "sleep", "30", stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    h.proc = proc

    report = await MANAGER.free_mac(1)

    assert [k["item"] for k in report["killed"]] == ["NPO 1"]
    assert report["killed"][0]["user"] == "bert"
    assert report["was_reason"] == "pipe"
    assert MANAGER.mac_locks == {}
    assert h.id not in MANAGER.streams
    assert proc.returncode is not None, "the released stream's pipe kept running"


async def test_release_drops_a_redirect_lease():
    """A lease is "the player is on the panel's CDN" - releasing ends it."""
    MANAGER.lease_mac(2, seconds=180, holder="bert", item="NPO 1", kind="live")
    report = await MANAGER.free_mac(2)
    assert report["lease_dropped"] is True
    assert MANAGER.lease_remaining(2) == 0.0
    assert MANAGER.mac_occupancy(2)["busy"] is False


async def test_release_clears_a_stale_lock_that_is_already_broken():
    """The stuck-MAC case: nothing is playing, the lock is just bookkeeping."""
    MANAGER.lock_mac(3, "gone-stream")
    report = await MANAGER.free_mac(3)
    assert report["ghosts"] == 1
    assert report["killed"] == []
    assert MANAGER.mac_locks == {}


async def test_release_reports_a_mac_that_was_already_free():
    report = await MANAGER.free_mac(4)
    assert report["was_busy"] is False
    assert report["killed"] == [] and report["ghosts"] == 0


async def test_release_one_does_the_panel_step_and_logs_it(monkeypatch):
    """End to end through the service: local release + a fresh session."""
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    portal, macs = await _db_rows(2)()
    h = _handle()
    MANAGER.streams[h.id] = h
    MANAGER.lock_mac(macs[0].id, h.id)

    rep = await mac_release.release_one(portal, macs[0])

    assert rep["mac"] == "00:1A:79:AA:BB:01"
    assert [k["item"] for k in rep["local"]["killed"]] == ["NPO 1"]
    assert rep["panel"]["session_dropped"] is True
    assert rep["panel"]["rehandshaked"] is True
    assert "NPO 1" in mac_release._summary(rep)


async def test_release_all_walks_every_portal_and_counts(monkeypatch):
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    portal, macs = await _db_rows(3)()
    for i, m in enumerate(macs[:2]):
        h = _handle(sid=f"rel-{i}", item_name=f"Channel {i}")
        MANAGER.streams[h.id] = h
        MANAGER.lock_mac(m.id, h.id)

    out = await mac_release.release_all()

    assert out["count"] == 3
    assert out["released_streams"] == 2
    assert out["portals"] >= 1
    assert MANAGER.mac_locks == {}


async def test_release_all_can_skip_macs_nothing_is_running_on(monkeypatch):
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    _portal, macs = await _db_rows(3)()
    MANAGER.lock_mac(macs[1].id, "gone-stream")

    out = await mac_release.release_all(only_busy=True)

    assert out["count"] == 1
    assert out["skipped"] == 2
    assert out["stale_locks"] == 1


async def test_release_portal_is_scoped_to_one_portal(monkeypatch):
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    portal, macs = await _db_rows(2)()
    out = await mac_release.release_portal(portal.id)
    assert out["ok"] is True and out["count"] == 2
    assert {r["mac"] for r in out["results"]} == {m.mac for m in macs}
    assert await mac_release.release_portal(99999) == {
        "ok": False, "error": "portal not found", "results": []}


def test_occupancy_overview_separates_stale_locks_from_streams():
    """What the confirmation dialog words, and what the dashboard compares."""
    live = _handle()
    MANAGER.streams[live.id] = live
    MANAGER.lock_mac(1, live.id)
    MANAGER.lock_mac(2, "gone-stream")

    ov = mac_release.occupancy_overview()
    assert ov["busy"] == 2
    assert ov["ghost"] == 1
    assert ov["ghost_mac_ids"] == [2]


# ========================================================================== #
# a stream that is still starting is not a ghost
# ========================================================================== #
def test_a_lock_of_a_stream_still_starting_is_not_a_ghost():
    """The window between taking the lock and the first byte is legitimate.

    The engine takes the MAC lock before ffmpeg has produced anything, and the
    handle is only registered once bytes arrive. A sweep that treated that
    lock as dead would free the MAC under a start that is working, and the
    next request would be handed the same MAC.
    """
    MANAGER.starting["opening"] = 1
    MANAGER.lock_mac(4, "opening")

    assert MANAGER.ghost_lock_ids() == {}
    assert MANAGER.prune_ghost_locks() == {}
    assert MANAGER._lock_set(4) == {"opening"}
    occ = MANAGER.mac_occupancy(4)
    assert occ["busy"] is True and occ["reason"] == "pipe"
    assert mac_release.occupancy_overview()["ghost"] == 0
    MANAGER.starting.clear()


async def test_release_does_not_kill_a_start_it_cannot_see_but_says_so():
    MANAGER.starting["opening"] = 1
    MANAGER.lock_mac(4, "opening")
    report = await MANAGER.free_mac(4)
    assert report["starting"] == 1
    assert report["killed"] == []
    assert MANAGER._lock_set(4) == {"opening"}, "a start in progress is not ripped out"
    MANAGER.starting.clear()
    MANAGER.mac_locks.clear()


# ========================================================================== #
# the HTTP surface
# ========================================================================== #
async def test_release_endpoints_over_http(monkeypatch):
    import httpx
    from app.main import app
    from app.portal import pool as _pool
    monkeypatch.setattr(_pool, "StalkerClient", _FakeStalkerClient)
    portal, macs = await _db_rows(2)()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        ov = await c.get("/api/portals/release-overview")
        assert ov.status_code == 200 and set(ov.json()) >= {"busy", "ghost", "ghost_mac_ids"}

        one = await c.post(f"/api/portals/{portal.id}/macs/{macs[0].id}/release",
                           json={"verify": False})
        assert one.status_code == 200, one.text
        body = one.json()
        assert body["mac"] == macs[0].mac and body["panel"]["rehandshaked"] is True

        missing = await c.post(f"/api/portals/{portal.id}/macs/999999/release", json={})
        assert missing.status_code == 404

        whole = await c.post(f"/api/portals/{portal.id}/release", json={})
        assert whole.status_code == 200 and whole.json()["count"] == 2

        nope = await c.post("/api/portals/999999/release", json={})
        assert nope.status_code == 404

        allp = await c.post("/api/portals/release-streams", json={"only_busy": True})
        assert allp.status_code == 200 and allp.json()["count"] == 0

        dash = await c.get("/api/dashboard")
        assert dash.status_code == 200
        assert "mac_occupancy" in dash.json()["api"]
