"""
A 429 is a portal-wide stop sign, not a per-MAC hiccup.

The panel rate-limits one host, not one MAC: once it answers 429, every further
request from that IP - the next MAC in the chain, the next EPG page, the next
health probe, a re-handshake - is what turns "slow down" into a banned IP, and
the ban hits every MAC on that portal. So the pause is recorded per host in
`app.portal.client`, honoured by `_get` *and* by the handshake (both raise
`rate_limited` without opening a socket), and it honours the panel's own
`Retry-After` when it sent one.

The mock portal grew the two knobs this needs: `http_status` (listed but never
applied before - a knob that cannot be turned is not a knob) and `retry_after`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.portal.client import (RATE_LIMITED_CODE, PortalError, StalkerClient,
                               note_rate_limit, portal_host, rate_limit_left,
                               reset_rate_limits)
from app.portal.mock_portal import router as MOCK_ROUTER

MAC = "00:1A:79:AA:AA:01"
PORTAL = "http://test/mock/c/portal.php"
OTHER = "http://other/mock/c/portal.php"


@pytest.fixture(autouse=True)
def _clean():
    reset_rate_limits()
    yield
    reset_rate_limits()


class Wired:
    """A client whose transport is the mock portal's ASGI app, counting calls."""

    def __init__(self, monkeypatch, portal_url: str = PORTAL) -> None:
        self.requests = 0
        app = FastAPI()
        app.include_router(MOCK_ROUTER)
        outer = self

        def factory(**kwargs):
            kwargs.pop("insecure", None)
            kwargs.pop("verify", None)

            async def _spy(request):
                outer.requests += 1

            kwargs["event_hooks"] = {"request": [_spy]}
            return httpx.AsyncClient(transport=ASGITransport(app=app), **kwargs)

        monkeypatch.setattr("app.portal.client.outbound_client", factory)
        self.client = StalkerClient(portal_url, MAC)


def mock_app() -> FastAPI:
    app = FastAPI()
    app.include_router(MOCK_ROUTER)
    return app


async def _control(**payload) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=mock_app())) as c:
        await c.post("http://test/mock/_control", json=payload)


# --------------------------------------------------------------------------- #
def test_a_429_pauses_the_whole_portal_host():
    """The unit-level contract: one host, one deadline, other hosts untouched."""
    left = note_rate_limit("http://portal.example:8080/c/portal.php",
                           retry_after=0, reason="HTTP 429")
    assert left == pytest.approx(30.0, abs=0.1)
    assert rate_limit_left("http://portal.example:8080/c/portal.php") > 0
    # a *different* host is a different panel with a different budget
    assert rate_limit_left("http://elsewhere.example/c/portal.php") == 0.0
    # and it is keyed by host: another .php path / another MAC shares the pause
    assert portal_host("http://PORTAL.example:8080/c/portal.php") == "portal.example:8080"


def test_retry_after_wins_over_the_default_and_is_capped():
    assert note_rate_limit("http://a/", retry_after=7) == pytest.approx(7.0, abs=0.1)
    assert note_rate_limit("http://b/", retry_after=99999) == pytest.approx(600.0, abs=0.1)
    reset_rate_limits()
    assert rate_limit_left("http://a/") == 0.0


async def test_a_429_stops_the_next_call_before_it_reaches_the_panel(monkeypatch):
    wired = Wired(monkeypatch)
    await _control(http_status=429, retry_after="9")
    try:
        with pytest.raises(PortalError) as first:
            await wired.client.handshake()
        assert first.value.code == RATE_LIMITED_CODE
        assert rate_limit_left(PORTAL) > 8            # the panel's own Retry-After
        before = wired.requests
        # Every later call - playback, EPG, health - is refused locally.
        with pytest.raises(PortalError) as second:
            await wired.client.live_genres()
        assert second.value.code == RATE_LIMITED_CODE
        with pytest.raises(PortalError) as third:
            await wired.client.handshake()
        assert third.value.code == RATE_LIMITED_CODE
        assert wired.requests == before, "no socket for a paused portal"
        assert "429" in str(second.value) or "rate limit" in str(second.value)
    finally:
        await _control(http_status=0, retry_after="")
        await wired.client._aclose()


async def test_the_pause_lifts_once_the_window_passed(monkeypatch):
    wired = Wired(monkeypatch)
    note_rate_limit(PORTAL, retry_after=1.0)      # 1 s is the floor
    with pytest.raises(PortalError):
        await wired.client.handshake()
    await asyncio.sleep(1.05)
    assert rate_limit_left(PORTAL) == 0.0
    token = await wired.client.handshake()         # the panel is reachable again
    assert token
    await wired.client._aclose()


async def test_a_429_on_a_mac_s_pause_also_holds_back_its_siblings(monkeypatch):
    """A second MAC on the same portal must not walk into the same 429."""
    wired_a = Wired(monkeypatch)
    wired_b = Wired(monkeypatch)
    note_rate_limit(PORTAL, retry_after=30)
    for c in (wired_a.client, wired_b.client):
        with pytest.raises(PortalError) as err:
            await c.handshake()
        assert err.value.code == RATE_LIMITED_CODE
    assert wired_a.requests == wired_b.requests == 0
    # a *different* portal keeps working
    note_rate_limit(OTHER, retry_after=30)
    assert rate_limit_left(PORTAL) > 0 and rate_limit_left(OTHER) > 0
    await wired_a.client._aclose()
    await wired_b.client._aclose()
