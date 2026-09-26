"""A candidate that produced no data must hand the next try a *fresh* connection.

The line that ends a dead zap -

    ERROR output [source] stream ended without producing data
    (source produced no data)

- is the last word of a chain in which every candidate was asked and none of
them sent a byte. Before the *next* candidate is asked, the failing candidate's
whole portal session has to go: bearer token, cookie/profile, and the pooled
httpx client with its TCP/TLS connection. A panel that answered one MAC with
silence may well have killed that MAC's session server side; asking again over
the same socket reuses whatever state the panel left behind, and the next
candidate inherits it.

`stream_manager._pump` does this in the no-data branch - `client.invalidate()`
plus `await POOL.drop(session)` (the pool entry is popped and its httpx client
closed, so the following `POOL.get()` builds a new client and handshakes from
scratch). These tests pin that, because the behaviour is invisible in the log:
both the "kept the session" and the "rebuilt it" cases log the same
`produced no data ... -> fallback` line.

The second test pins the one deliberate exception. A slot-busy refusal
(`limit`, `account_is_in_use`, `max_connections`) says the bearer is valid and
the slot is taken - and a fresh handshake would *kick* the session the player
is still using, so that failure keeps the session and simply moves to the next
MAC. Turning that into a reconnect as well is a policy change, not a fix, and
it should not happen by accident.
"""

from __future__ import annotations

import asyncio

import pytest

from app.portal.client import PortalError
from app.services import stream_manager as sm
from app.services.stream_manager import MANAGER
from tests.test_mac_availability import _live_route

MAC_A = "00:1A:79:00:00:01"
MAC_B = "00:1A:79:00:00:02"


class _RecordingClient:
    """A portal client that writes every session-level event to a shared log."""

    def __init__(self, events: list):
        self.events = events
        self.portal_url = "http://127.0.0.1:1/c/"
        self.mac = None
        self.refusal = None          # PortalError to raise from create_link

    async def ensure_auth(self):
        return None

    def invalidate(self):
        self.events.append(("invalidate", self.mac))

    async def close(self):
        return None

    async def create_link(self, cmd, kind="live", **kw):
        self.events.append(("create_link", self.mac))
        if self.refusal is not None:
            raise self.refusal
        return "http://cdn/x.ts?play_token=t"


class _RecordingPool:
    """Stands in for portal.pool: `drop` is what "rebuild this session" means."""

    def __init__(self, client, events: list):
        self._client = client
        self.events = events

    async def get(self, session):
        self._client.mac = session.mac
        self.events.append(("get", session.mac))
        return self._client

    async def drop(self, session):
        self.events.append(("drop", session.mac))


async def _no_data(self, command, url, *, title, pace, first_byte_timeout=None):
    """ffmpeg exits before a single byte: the `open_fail` no-data branch."""
    return None, b"", {"rc": 8, "tail": "http://cdn/x.ts: Server returned 404",
                       "stalled": False}


def _wire(monkeypatch, events: list, *, refusal: PortalError | None = None):
    client = _RecordingClient(events)
    client.refusal = refusal
    monkeypatch.setattr("app.services.stream_manager.POOL",
                        _RecordingPool(client, events))
    monkeypatch.setattr("app.services.stream_manager.StreamManager._open_with_identity",
                        _no_data)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT", 0.05)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT_REST", 0.05)
    monkeypatch.setattr(sm, "ZAP_RETRY", False)
    monkeypatch.setattr(sm, "ZAP_RETRY_DELAY", 0.0)
    monkeypatch.setattr(sm, "BUSY_BACKOFF", ())     # no ladder inside a test
    return client


@pytest.fixture(autouse=True)
def _clean():
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()
    yield
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()


async def test_a_silent_candidate_session_is_dropped_before_the_next_mac(monkeypatch):
    """No data on MAC A -> A's session is rebuilt *before* MAC B is asked."""
    pl, _rows = await _live_route(macs=(MAC_A, MAC_B))
    events: list = []
    _wire(monkeypatch, events)

    _handle, gen = await MANAGER.open("live", pl, "bert")
    chunks = [c async for c in gen]

    assert chunks == []                              # nothing reached the player
    tried = [mac for ev, mac in events if ev == "create_link"]
    assert tried == [MAC_A, MAC_B], events           # both candidates were asked

    # 1. every candidate that produced no data had its session dropped ...
    assert [mac for ev, mac in events if ev == "drop"] == tried, events
    # 2. ... and the drop landed *before* the next candidate was asked, not
    #    after the chain had already finished with it.
    for i in range(len(tried) - 1):
        assert events.index(("drop", tried[i])) < events.index(("create_link", tried[i + 1])), events
    # 3. dropping alone is not enough: the token/cookie has to go too, or a
    #    rebuilt client would carry the old bearer into the new handshake.
    assert [mac for ev, mac in events if ev == "invalidate"] == tried, events


async def test_a_busy_slot_refusal_keeps_the_session(monkeypatch):
    """`limit` is not a broken session - reconnecting would kick the player."""
    pl, _rows = await _live_route(macs=(MAC_A, MAC_B))
    events: list = []
    _wire(monkeypatch, events,
          refusal=PortalError("the panel still counts the previous connection",
                              code="limit"))

    _handle, gen = await MANAGER.open("live", pl, "bert")
    chunks = [c async for c in gen]

    assert chunks == []
    tried = [mac for ev, mac in events if ev == "create_link"]
    assert tried == [MAC_A, MAC_B], events
    assert [mac for ev, mac in events if ev == "drop"] == [], events
