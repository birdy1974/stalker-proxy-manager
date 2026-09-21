"""Occupancy must never veto a zap.

The rule these tests pin down came from the reference implementation whose zaps
nobody complains about (STB-Proxy): it keeps no state across requests, so a zap
can never lose against its own bookkeeping. SPM keeps state on purpose (pooled
sessions, per-user quotas, redirect leases), so it has to *behave* as if it did
not:

* a busy MAC is waited for (BUSY_WAIT_S) instead of refused in 13 ms;
* the same user's own pipe is taken over - a zap, not a conflict;
* a panel that reports "already streaming" is asked again a moment later
  (BUSY_BACKOFF) instead of burning the candidate;
* a candidate that just failed is tried last on the retry pass;
* the final answer for "everything was busy" is 503 + Retry-After, not 404.

See docs/STREAM-FETCH-ANALYSIS.md ("What to implement") and Appendix C for the
comparison this encodes.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.database import SessionLocal
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal,
)
from app.portal.client import PortalError
from app.portal.links import plan_for
from app.services import stream_manager
from app.services.stream_manager import (
    MANAGER, StreamHandle, cached_link, demote_failed_candidates, demote_macs,
    drop_link, note_candidate_failure, note_link, reset_zap_state,
)


@pytest.fixture(autouse=True)
def _clean_manager():
    """MANAGER is a process-global singleton; so is the zap memory."""
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()
    reset_zap_state()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()
    reset_zap_state()


async def _route(*, macs=1, link_flags="use_http_tmp_link"):
    """One portal + N MACs + one live item; ids of (playlist, [mac ids])."""
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/",
                        resolved_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        ids = []
        for i in range(macs):
            m = MacAddress(portal_id=portal.id, mac=f"00:1A:79:00:00:{i + 1:02d}",
                           status="online", order=i)
            s.add(m)
            await s.flush()
            ids.append(m.id)
        src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                         original_name="Ch", cmd="ffmpeg http://cdn/x.ts",
                         enabled=True, link_flags=link_flags)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Ch", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id, ids


def _fake_stream(name: str, mac_id: int, user: str) -> StreamHandle:
    """A registered pipe of `user` on `mac_id` (no process: nothing to kill)."""
    h = StreamHandle(id=f"sid-{name}", kind="live", item_name=name,
                     user_name=user, template_name="t", command="ffmpeg")
    MANAGER.streams[h.id] = h
    MANAGER.lock_mac(mac_id, h.id)
    return h


# --------------------------------------------------------------------------- #
#  the wait, and the takeover
# --------------------------------------------------------------------------- #
async def test_another_users_pipe_is_waited_for_then_answered_as_busy(monkeypatch):
    """Not an instant 404: wait the budget, then say "busy"."""
    monkeypatch.setattr(stream_manager, "BUSY_WAIT_S", 0.3)
    monkeypatch.setattr(stream_manager, "BUSY_POLL_S", 0.05)
    pl, (mid,) = await _route()
    _fake_stream("someone-else", mid, "other-user")

    started = time.monotonic()
    handle, _gen = await MANAGER.open("live", pl, "box")
    waited = time.monotonic() - started

    assert handle.dead, "the MAC really is busy - this open must not claim it"
    assert handle.busy, "and the reason is occupancy, not a dead source"
    assert waited >= 0.25, f"it waited for the MAC (waited {waited:.2f}s)"
    # logging in the log: not needed here, but the note must name the holder
    assert "other-user" in (MANAGER._occupied_note(_StubMac(mid)) or "")


class _StubMac:
    def __init__(self, mid):
        self.id = mid
        self.mac = "00:1A:79:00:00:01"


async def test_our_own_pipe_is_taken_over_instead_of_refusing_the_zap(monkeypatch):
    """The channel this box just left is not somebody else's stream.

    Before this, a .ts -> .ts zap answered 404 while the player was still
    tearing the old socket down - the one asymmetry against the redirect path,
    which has allowed the same takeover since the lease existed.
    """
    monkeypatch.setattr(stream_manager, "BUSY_WAIT_S", 0.3)
    pl, (mid,) = await _route()
    old = _fake_stream("Ch1", mid, "box")

    handle, _gen = await MANAGER.open("live", pl, "box")

    assert old.dead, "the old pipe was killed"
    assert not handle.dead, "the zap got the MAC"
    assert MANAGER.is_mac_busy(mid, requester="box") is False or handle.dead


async def test_a_different_user_never_gets_the_pipe_taken_away(monkeypatch):
    monkeypatch.setattr(stream_manager, "BUSY_WAIT_S", 0.2)
    monkeypatch.setattr(stream_manager, "BUSY_POLL_S", 0.05)
    pl, (mid,) = await _route()
    other = _fake_stream("Ch1", mid, "other-user")

    handle, _gen = await MANAGER.open("live", pl, "box")

    assert not other.dead, "another viewer's stream is untouched"
    assert handle.dead and handle.busy


async def test_a_zap_takes_the_free_mac_instead_of_waiting(monkeypatch):
    """A multi-MAC portal: the first MAC is somebody else's, the second is free.

    This is the topology the reference proxy is normally run in (`streams per
    mac` = 1, several MACs): its `/play` loop walks the MAC list, asks
    `isMacFree()` per MAC and plays the first free one - which is why a zap lands
    there without any locking trickery. SPM has to do the same, or a two-MAC
    portal spends BUSY_WAIT_S (and a refused create_link) on a MAC it cannot
    have while a working one sits in the same chain.
    """
    monkeypatch.setattr(stream_manager, "BUSY_WAIT_S", 5.0)     # must NOT be spent
    pl, (mac1, mac2) = await _route(macs=2)
    held = _fake_stream("other", mac1, "other-user")

    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))

    async def fake_open(self, command, url, *, title="", pace=False):
        return _Proc(), b"\x47" * 188 * 4, None

    monkeypatch.setattr(type(MANAGER), "_open_with_identity", fake_open)

    started = time.monotonic()
    handle, gen = await MANAGER.open("live", pl, "box")
    first = await gen.__anext__()
    elapsed = time.monotonic() - started

    assert first, "the stream started"
    assert elapsed < 0.5, f"no waiting while a MAC is free (took {elapsed:.2f}s)"
    assert handle.mac == "00:1A:79:00:00:02", "the free MAC is the one used"
    assert client.calls == 1, "one create_link, on the free MAC"
    assert not held.dead, "another viewer's stream was not touched"
    assert handle.busy is False
    await gen.aclose()


async def test_both_macs_busy_waits_once_for_the_chain(monkeypatch):
    """Nothing free: wait BUSY_WAIT_S once for the chain, not per MAC."""
    monkeypatch.setattr(stream_manager, "BUSY_WAIT_S", 0.4)
    monkeypatch.setattr(stream_manager, "BUSY_POLL_S", 0.05)
    pl, (mac1, mac2) = await _route(macs=2)
    _fake_stream("a", mac1, "other-user")
    _fake_stream("b", mac2, "other-user")

    started = time.monotonic()
    handle, _gen = await MANAGER.open("live", pl, "box")
    elapsed = time.monotonic() - started

    assert handle.dead and handle.busy
    assert 0.35 <= elapsed < 0.9, (
        f"one chain-wide wait, not BUSY_WAIT_S per MAC (took {elapsed:.2f}s)")


async def test_a_redirect_zap_prefers_the_untouched_mac(monkeypatch):
    """Our own 302 lease says "that MAC was streaming a moment ago".

    For the *next* channel the untouched MAC is the better candidate: the panel
    still counts the leased one (so create_link answers `limit` until its table
    clears), while the other one has a slot free right now. The lease is still
    taken back when it is the only option - see
    `test_another_users_pipe_is_waited_for_then_answered_as_busy` and the
    single-MAC tests above.
    """
    pl, (mac1, mac2) = await _route(macs=2)
    MANAGER.lease_mac(mac1, holder="box", item="Ch1", kind="live", ref=pl)
    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))
    monkeypatch.setattr(stream_manager, "link_is_alive", _always_alive())

    url, _name = await MANAGER.resolve("live", pl, requester="box")

    assert url == client.url
    assert MANAGER.lease_holder(mac2) == "box", "the new lease is on the fresh MAC"
    assert (MANAGER.lease_meta.get(mac2) or {}).get("item") == "Ch"


async def test_a_free_mac_outranks_the_one_that_worked_last():
    """Affinity says "this MAC played last"; free says "this one can play now"."""
    pl, (mac1, mac2) = await _route(macs=2)
    _fake_stream("last-time", mac1, "box")          # busy: the zap's own channel
    order = MANAGER.order_by_free(("live", pl), _Src(1), [_Mac(mac1), _Mac(mac2)],
                                  "box")
    assert [m.id for m in order] == [mac2, mac1]


async def test_a_second_stream_is_allowed_when_the_portal_says_so():
    """Portal.streams_per_mac - the knob STB-Proxy calls "streams per mac"."""
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://x/c/", resolved_url="http://x/c/",
                   streams_per_mac=2)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac="00:1A:79:00:00:09", status="online"))
        await s.commit()
        pid, mid = p.id, (await s.execute(
            __import__("sqlalchemy").select(MacAddress).where(
                MacAddress.portal_id == p.id))).scalars().first().id
    MANAGER.note_mac_limit(mid, 2)
    _fake_stream("first", mid, "box")
    assert MANAGER.is_mac_busy(mid) is False, "one of two slots is free"
    _fake_stream("second", mid, "box2")
    assert MANAGER.is_mac_busy(mid) is True, "the second slot is the limit"


# --------------------------------------------------------------------------- #
#  the panel's own slot
# --------------------------------------------------------------------------- #
class _BusyThenOkClient:
    """create_link answers `limit` a few times, then a URL - the panel letting
    go of the connection the previous zap left behind."""

    def __init__(self, refusals: int, url="http://cdn/x.ts?play_token=fresh"):
        self.refusals, self.calls, self.url = refusals, 0, url
        self.portal_url = "http://127.0.0.1:1/c/"

    async def ensure_auth(self):
        return None

    def invalidate(self):
        pass

    async def close(self):
        return None

    async def create_link(self, cmd, kind="live", **kw):
        self.calls += 1
        if self.calls <= self.refusals:
            raise PortalError("portal said limit", code="limit")
        return self.url


async def test_the_same_mac_is_asked_again_while_the_panel_is_busy(monkeypatch):
    """A busy slot is a moment, not a MAC problem - on one MAC it is the only
    candidate there is."""
    monkeypatch.setattr(stream_manager, "BUSY_BACKOFF", (0.01, 0.01, 0.01))
    pl, _ = await _route()
    client = _BusyThenOkClient(refusals=2)
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))

    url, _name = await MANAGER.resolve("live", pl)

    assert url == client.url
    assert client.calls == 3, "asked again instead of burning the candidate"


async def test_a_busy_panel_becomes_503_not_404(monkeypatch):
    """`out["busy"]` is what the route turns into 503 + Retry-After."""
    monkeypatch.setattr(stream_manager, "BUSY_BACKOFF", (0.01,))
    monkeypatch.setattr(stream_manager, "ZAP_RETRY_DELAY", 0.01)
    pl, _ = await _route()
    client = _BusyThenOkClient(refusals=99)
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))

    out: dict = {}
    url, _name = await MANAGER.resolve("live", pl, out=out)

    assert url is None and out["busy"] is True


async def test_a_dead_source_is_not_reported_as_busy(monkeypatch):
    monkeypatch.setattr(stream_manager, "ZAP_RETRY_DELAY", 0.01)
    pl, _ = await _route()
    client = _BusyThenOkClient(refusals=0,
                               url=None)      # a portal that answers no URL
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))

    out: dict = {}
    url, _name = await MANAGER.resolve("live", pl, out=out)

    assert url is None and out.get("busy") is False


class _Pool:
    def __init__(self, client):
        self._client = client

    async def get(self, session):
        return self._client


class _Proc:
    """Enough of an ffmpeg process for the pump: one chunk, then EOF."""

    def __init__(self):
        self.returncode = None
        self.pid = 4242
        self.stdout = self
        self.stderr = self
        self._sent = False

    async def read(self, n):
        if self._sent:
            return b""
        self._sent = True
        return b"\x47" * 188 * 4

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


async def test_the_output_guard_turns_busy_into_503(monkeypatch):
    """The proxy path's answer for "everything was busy" is 503 + Retry-After.

    The engine only knows it after walking the chain, so the verdict arrives
    while the response is being built - `_guarded` is where it has to be read.
    A 404 there told the player the channel does not exist, and a 502 tells it
    the same thing with a different number.
    """
    from fastapi import HTTPException

    from app.routers.output import _guarded

    busy = StreamHandle(id="s", kind="live", item_name="Ch", user_name="box",
                        template_name="t", command="ffmpeg")
    busy.dead = True
    busy.busy = True
    busy.fail_note = "2 attempt(s) without data (all 2 refusal(s) were busy slots)"

    async def empty():
        return
        yield b""                                   # pragma: no cover

    monkeypatch.setattr("app.routers.output._guard_wait", lambda handle: 0.05)
    with pytest.raises(HTTPException) as err:
        await _guarded(empty(), "live #1", "Ch", handle=busy)
    assert err.value.status_code == 503
    assert err.value.headers["Retry-After"] == "2"


async def test_the_output_guard_still_says_502_for_a_dead_source(monkeypatch):
    from fastapi import HTTPException

    from app.routers.output import _guarded

    dead = StreamHandle(id="s", kind="live", item_name="Ch", user_name="box",
                        template_name="t", command="ffmpeg")
    dead.dead = True                      # not busy: the source really is dead

    async def empty():
        return
        yield b""                                   # pragma: no cover

    monkeypatch.setattr("app.routers.output._guard_wait", lambda handle: 0.05)
    with pytest.raises(HTTPException) as err:
        await _guarded(empty(), "live #1", "Ch", handle=dead)
    assert err.value.status_code == 502


# --------------------------------------------------------------------------- #
#  rotation, not affinity
# --------------------------------------------------------------------------- #
class _Src:
    def __init__(self, ident):
        self.id = ident


class _Mac:
    def __init__(self, ident):
        self.id = ident


def test_a_candidate_that_just_failed_is_tried_last():
    route = ("live", 1)
    a, b = _Src(1), _Src(2)
    mac_a, mac_b = _Mac(10), _Mac(11)
    chain = [(a, None, [mac_a]), (b, None, [mac_b])]

    assert demote_failed_candidates(route, chain)[0][0] is a
    note_candidate_failure(route, a, mac_a)
    assert demote_failed_candidates(route, chain)[0][0] is b, "affinity must not win"
    assert demote_macs(route, a, [mac_a, mac_b])[0] is mac_b


def test_the_demotion_expires(monkeypatch):
    route = ("live", 2)
    src, mac = _Src(1), _Mac(10)
    note_candidate_failure(route, src, mac)
    monkeypatch.setattr(stream_manager, "FAILURE_DEMOTE_S", -1.0)   # already expired
    assert demote_macs(route, src, [mac]) == [mac]
    assert demote_failed_candidates(route, [(src, None, [mac])]) == [(src, None, [mac])]


def test_a_failure_on_another_route_does_not_leak():
    src, mac = _Src(1), _Mac(10)
    note_candidate_failure(("live", 1), src, mac)
    assert demote_macs(("live", 2), src, [mac]) == [mac]


# --------------------------------------------------------------------------- #
#  the zap-back cache
# --------------------------------------------------------------------------- #
def test_the_link_cache_remembers_live_and_forgets_the_rest():
    note_link("live", 7, 3, "http://cdn/x.ts")
    assert cached_link("live", 7, 3)[0] == "http://cdn/x.ts"
    note_link("vod", 7, 3, "http://cdn/movie.mp4")
    assert cached_link("vod", 7, 3) is None, "a movie link is not a zap"


def test_the_link_cache_expires(monkeypatch):
    note_link("live", 7, 3, "http://cdn/x.ts")
    monkeypatch.setattr(stream_manager, "LINK_CACHE_S", 0.01)
    time.sleep(0.02)
    assert cached_link("live", 7, 3) is None


def test_a_cached_link_is_per_item_mac_and_user():
    note_link("live", 1, 1, "http://cdn/one.ts", "box")
    assert cached_link("live", 1, 1, user_name="box")[0] == "http://cdn/one.ts"
    assert cached_link("live", 2, 1, user_name="box") is None, "another item"
    assert cached_link("live", 1, 2, user_name="box") is None, "another MAC"
    assert cached_link("live", 1, 1, user_name="anna") is None, \
        "a link resolved for one user is not handed to another"
    drop_link("live", 1, 1, "box")
    assert cached_link("live", 1, 1, user_name="box") is None


async def test_a_zap_back_replays_the_link_without_asking_the_panel(monkeypatch):
    """The whole point: away and back inside LINK_CACHE_S costs one 302."""
    pl, _ = await _route()
    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))
    fake_alive = _always_alive()
    monkeypatch.setattr(stream_manager, "link_is_alive", fake_alive)

    first, _ = await MANAGER.resolve("live", pl)
    MANAGER.redirect_leases.clear()                    # the box zapped away
    second, _ = await MANAGER.resolve("live", pl)

    assert first == second
    assert client.calls == 1, "no second create_link for the zap back"


async def test_a_dead_cached_link_is_dropped_and_replaced(monkeypatch):
    pl, _ = await _route()
    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))

    note_link("live", pl, await _only_mac_id(), "http://cdn/stale.ts", "box")
    alive = {"calls": 0}

    async def probe(url, **kw):
        from app.services.redirect_guard import ProbeResult
        alive["calls"] += 1
        return ProbeResult("/stale" not in url, "fake")

    monkeypatch.setattr(stream_manager, "link_is_alive", probe)
    url, _name = await MANAGER.resolve("live", pl, requester="box")

    assert url == client.url, "the stale link was not handed out"
    assert alive["calls"] >= 2, "the cached link was probed before being trusted"


async def _only_mac_id() -> int:
    from sqlalchemy import select as sa_select

    from app.models import MacAddress
    async with SessionLocal() as s:
        return (await s.execute(sa_select(MacAddress.id).order_by(MacAddress.id))).scalars().first()


def _always_alive():
    async def probe(url, **kw):
        from app.services.redirect_guard import ProbeResult
        return ProbeResult(True, "fake")
    return probe


# =========================================================================== #
# ghost pipes: what a hard restart leaves behind
# =========================================================================== #
def _fake_proc_tree(tmp_path, procs):
    """A `/proc` stand-in: {pid: (exe, environ, cmdline_extra)}."""
    for pid, (exe, environ) in procs.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(exe.encode() + b"\0-i\0http://x\0")
        (d / "environ").write_bytes(environ.encode() + b"\0")
    return str(tmp_path)


def test_the_sweep_finds_only_our_own_orphans(tmp_path, monkeypatch):
    """Verified live before this existed: `kill -9` the server and the ffmpeg
    pipe keeps running, still reading the panel stream - so the panel keeps
    counting that MAC's connection while the dashboard shows nothing. After a
    crash/container restart only a marker-based sweep can find them, and it must
    never touch an ffmpeg that is not ours."""
    import os as _os

    mine = _os.path.basename(stream_manager.FFMPEG_BIN)
    root = _fake_proc_tree(tmp_path, {
        11: (mine, "PATH=/usr/bin SPM_STREAM_ID=abc"),          # ours, orphaned
        12: (mine, "PATH=/usr/bin"),                            # ffmpeg, not ours
        13: ("/usr/bin/python3", "SPM_STREAM_ID=abc"),          # marked, not ffmpeg
        14: (mine, "SPM_STREAM_ID=no-prefix"),                  # ours
    })
    monkeypatch.setattr(stream_manager, "_orphan_ffmpeg_pids", stream_manager._orphan_ffmpeg_pids)
    found = stream_manager._orphan_ffmpeg_pids(root)
    assert sorted(found) == [11, 14]


async def test_the_sweep_kills_them_and_says_so(tmp_path, monkeypatch):
    import os as _os

    mine = _os.path.basename(stream_manager.FFMPEG_BIN)
    root = _fake_proc_tree(tmp_path, {21: (mine, "SPM_STREAM_ID=x")})
    real = stream_manager._orphan_ffmpeg_pids
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(stream_manager.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    # the real scanner against the fake tree, then the same list handed to the
    # manager (which must SIGKILL it and report the count)
    assert real(root) == [21]
    monkeypatch.setattr(stream_manager, "_orphan_ffmpeg_pids",
                        lambda root="/proc": real(root) if root != "/proc" else [21])
    n = await MANAGER.sweep_orphans("boot")
    assert n == 1 and killed and killed[0][0] == 21


async def test_every_spawn_carries_the_marker(monkeypatch):
    """Without the marker on the child there is nothing to sweep by."""
    seen: dict = {}

    class _P:
        pid = 4242
        returncode = None

    async def fake_exec(*args, **kwargs):
        seen.update(kwargs)
        return _P()

    monkeypatch.setattr(stream_manager.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(type(MANAGER), "_drain_stderr",
                        lambda self, proc: asyncio.sleep(0))
    await MANAGER._spawn("ffmpeg -i <url> -c copy -f mpegts pipe:1", "http://h/x.ts", "Ch")
    env = seen.get("env") or {}
    assert env.get("SPM_STREAM_ID"), "the child is identifiable"
    assert "PATH" in env, "and still sees the environment a template may need"


# =========================================================================== #
# mid-stream: the link died, not the channel
# =========================================================================== #
class _DyingProc:
    """ffmpeg that sends a burst and then ends - a stream the panel dropped."""

    def __init__(self, chunks: int = 2):
        self.returncode = None
        self.pid = 5150
        self.stdout = self
        self.stderr = self
        self._left = chunks

    async def read(self, n):
        if self._left <= 0:
            self.returncode = 0
            return b""
        self._left -= 1
        return b"\x47" * 188 * 10

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


async def test_a_live_stream_that_dies_is_restarted_in_the_same_response(monkeypatch):
    """The panel invalidated the URL - the channel is fine.

    Before this, the pump walked the remaining candidates and ended the response,
    so the player saw a dead stream until it decided to reconnect. Now the chain
    is re-walked with a *fresh* create_link (the cached link is dropped - it is
    the thing that died) and the bytes keep coming on the same HTTP response.
    """
    import app.services.stream_manager as sm
    monkeypatch.setattr(sm, "MIDSTREAM_RESTART_DELAY", 0.01)
    pl, (mac,) = await _route()
    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(sm, "POOL", _Pool(client))
    monkeypatch.setattr(sm, "link_is_alive", _always_alive())
    spawned = []

    async def fake_spawn(*a, **kw):
        spawned.append(1)
        return _DyingProc(chunks=2 if len(spawned) == 1 else 3)

    monkeypatch.setattr(type(MANAGER), "_spawn", fake_spawn)
    monkeypatch.setattr(type(MANAGER), "_drain_stderr",
                        lambda self, proc: asyncio.sleep(0))

    handle, gen = await MANAGER.open("live", pl, "box")
    got = 0
    async for chunk in gen:
        got += len(chunk)

    assert got >= 188 * 10 * 4, "bytes from both the first and the restarted stream"
    assert len(spawned) >= 2, "a second ffmpeg was started instead of giving up"
    assert client.calls >= 2, "and it asked the panel for a fresh link"


async def test_the_restart_is_bounded(monkeypatch):
    """Every restart ends the same way here: the chain must not loop forever."""
    import app.services.stream_manager as sm
    monkeypatch.setattr(sm, "MIDSTREAM_RESTARTS", 2)
    monkeypatch.setattr(sm, "MIDSTREAM_RESTART_DELAY", 0.01)
    pl, (mac,) = await _route()
    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(sm, "POOL", _Pool(client))
    monkeypatch.setattr(sm, "link_is_alive", _always_alive())
    spawned = []

    async def fake_spawn(*a, **kw):
        spawned.append(1)
        return _DyingProc(chunks=1)

    monkeypatch.setattr(type(MANAGER), "_spawn", fake_spawn)
    monkeypatch.setattr(type(MANAGER), "_drain_stderr",
                        lambda self, proc: asyncio.sleep(0))

    handle, gen = await MANAGER.open("live", pl, "box")
    async for _chunk in gen:
        pass
    assert len(spawned) == 1 + 2, "one start plus exactly MIDSTREAM_RESTARTS retries"


async def test_a_finished_movie_is_not_restarted(monkeypatch):
    """A VOD that reached its end is *finished*, not dropped - and a kind that
    is not on the list is never restarted, whatever the reason it ended."""
    import app.services.stream_manager as sm
    assert "live" in sm.MIDSTREAM_RESTART_KINDS
    assert "vod" not in sm.MIDSTREAM_RESTART_KINDS, "a finished movie must not replay"
    monkeypatch.setattr(sm, "MIDSTREAM_RESTART_DELAY", 0.01)
    monkeypatch.setattr(sm, "MIDSTREAM_RESTART_KINDS", set())   # nothing may restart
    pl, (mac,) = await _route()
    client = _BusyThenOkClient(refusals=0)
    monkeypatch.setattr(sm, "POOL", _Pool(client))
    monkeypatch.setattr(sm, "link_is_alive", _always_alive())
    spawned = []

    async def fake_spawn(*a, **kw):
        spawned.append(1)
        return _DyingProc(chunks=2)

    monkeypatch.setattr(type(MANAGER), "_spawn", fake_spawn)
    monkeypatch.setattr(type(MANAGER), "_drain_stderr",
                        lambda self, proc: asyncio.sleep(0))

    handle, gen = await MANAGER.open("live", pl, "box")
    async for _chunk in gen:
        pass
    assert len(spawned) == 1, "no replay of a stream that simply ended"
