"""Zap resilience: a fresh open that finds every route busy or failing gets one
delayed retry instead of an instant black screen.

Enigma2 zaps fast - the box asks for the new channel while the panel still
counts the old one against the MAC's single slot (create_link 502, or a link
that sends zero bytes) or while our own watchdog is still tearing the old pipe
down (MAC busy, user at max_connections). Retrying once after a short delay
turns those into a slightly slower zap.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.database import SessionLocal
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal, User,
)
from app.portal.client import PortalError
from app.routers.output import _ensure_slot
from app.services.stream_manager import MANAGER, StreamHandle


@pytest.fixture(autouse=True)
def _clean_manager():
    """MANAGER is a process-global singleton: leases, locks and breaker state
    from one test must not decide the next test's routes."""
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.streams.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.streams.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()


async def _live_route(*, link_flags="use_http_tmp_link"):
    """One portal + MAC + live source + playlist item. The portal is
    pre-resolved so the stream path never touches the network for discovery,
    and the tmp-link flag forces a create_link on every open."""
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/",
                        resolved_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        s.add(MacAddress(portal_id=portal.id, mac="00:1A:79:00:00:01",
                         status="online", order=0))
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
        return pl.id


class _FakeClient:
    """A portal session whose create_link answers follow a script."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0
        self.portal_url = "http://127.0.0.1:1/c/"

    async def ensure_auth(self):
        return None

    def invalidate(self):
        pass

    async def close(self):
        return None

    async def create_link(self, cmd, kind="live", **kw):
        self.calls += 1
        outcome = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakePool:
    def __init__(self, client):
        self._client = client

    async def get(self, session):
        return self._client


async def test_redirect_retries_once_when_create_link_blips(monkeypatch):
    """A panel 502 on the first pass (the slot still holds the previous zap)
    is retried once - the box gets its 302 a moment later instead of
    'no source produced a link'."""
    pid = await _live_route()
    client = _FakeClient([PortalError("HTTP 502", code="http_502"),
                          "http://cdn/x.ts?play_token=fresh"])
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY_DELAY", 0.01)
    url, _name = await MANAGER.resolve("live", pid)
    assert url == "http://cdn/x.ts?play_token=fresh"
    assert client.calls == 2


async def test_redirect_still_fails_when_both_passes_fail(monkeypatch):
    """The retry is one retry, not a loop: a panel that keeps refusing still
    ends in 'no source produced a link'."""
    pid = await _live_route()
    client = _FakeClient([PortalError("HTTP 502", code="http_502")])
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY_DELAY", 0.01)
    url, _name = await MANAGER.resolve("live", pid)
    assert url is None
    assert client.calls == 2


class _FakeOut:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    def __init__(self, chunks, rc=0):
        self.stdout = _FakeOut(chunks)
        self.returncode = rc
        self.pid = 4242

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def test_proxy_retries_a_stillborn_link_once(monkeypatch):
    """The panel hands out a link that sends zero bytes (its slot still holds
    the previous zap): the pump kills it and, instead of 'all fallbacks
    exhausted', asks once more after the retry delay - with a FRESH link."""
    pid = await _live_route()
    links = ["http://cdn/dead.ts?play_token=old", "http://cdn/live.ts?play_token=new"]
    client = _FakeClient(links)
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY_DELAY", 0.01)
    spawns = []

    async def fake_spawn(self, cmd_template, url, title=None, pace=False,
                         user_agent=None):
        spawns.append(url)
        if len(spawns) == 1:
            return _FakeProc([], rc=1)          # EOF, never a byte
        return _FakeProc([b"x" * 188])         # first byte flows

    # class-level: an instance-level monkeypatch leaves a shadowing attribute
    # behind on teardown that hides later class patches (see test_stream_ua_ladder)
    monkeypatch.setattr(type(MANAGER), "_spawn", fake_spawn)
    _handle, gen = await MANAGER.open("live", pid, "zapbox")
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()
    assert first == b"x" * 188
    assert spawns == links          # the second attempt re-asked the panel
    assert client.calls == 2


async def test_max_connections_waits_out_zap_overlap(monkeypatch):
    """A user at max_connections whose old pipe is mid-teardown (the 0.5s
    watchdog window) gets the slot after one short wait - not an instant 429.
    Honest overload still 429s."""
    monkeypatch.setattr("app.routers.output.MAXCONN_RETRY_DELAY", 0.05)
    user = User(name="box", password="pw", max_connections=1)
    old = StreamHandle(id="zap-old", kind="live", item_name="Old",
                       user_name="box", template_name="t", command="c")
    await MANAGER._register(old)

    async def teardown():
        await asyncio.sleep(0.01)
        await MANAGER._deregister(old)

    task = asyncio.create_task(teardown())
    await _ensure_slot(user)                      # would 429 without the retry
    await task

    stuck = StreamHandle(id="zap-stuck", kind="live", item_name="Stuck",
                         user_name="box", template_name="t", command="c")
    await MANAGER._register(stuck)
    try:
        with pytest.raises(HTTPException) as exc:
            await _ensure_slot(user)
        assert exc.value.status_code == 429
    finally:
        await MANAGER._deregister(stuck)
