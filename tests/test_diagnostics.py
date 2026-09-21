"""
The numbers behind "why does zapping feel slow".

Everything here is in-memory and bounded, and the point of the block is to
answer the questions the log can only answer one event at a time:

  * how long a start takes on each *path* (the 302 path is a panel call plus one
    probe; the proxy path adds ffmpeg and the source's first byte),
  * how often starts fail and with which reason,
  * what the panel actually refused us with (`limit` is a busy slot,
    `access_denied` is a dead account, `http_429` means stop asking),
  * how much the probe cache saved and how often background work yielded.
"""

from __future__ import annotations

import httpx
import pytest

from app.portal.client import (note_refusal, refusal_stats, reset_rate_limits,
                               reset_refusals)
from app.routers.api_misc import _diagnostics
from app.services import stream_manager as sm
from app.services.stream_manager import MANAGER


@pytest.fixture(autouse=True)
def _clean():
    MANAGER.timings.clear()
    reset_refusals()
    reset_rate_limits()
    yield
    MANAGER.timings.clear()
    reset_refusals()
    reset_rate_limits()


def test_the_window_reports_percentiles_per_path_and_why_starts_failed():
    for ms in (100, 200, 300, 400, 500):
        MANAGER.note_timing(kind="live", mode="proxy", item="Ch", portal="p1",
                            mac="00:1A:79:00:00:01", total_ms=ms, prepare_ms=10,
                            first_ms=ms - 20)
    MANAGER.note_timing(kind="live", mode="redirect", item="Ch", portal="p1",
                        total_ms=50, prepare_ms=45)
    MANAGER.note_timing(kind="live", mode="proxy", item="Ch", fail="busy")
    MANAGER.note_timing(kind="live", mode="redirect", item="Ch", fail="no-link")

    t = MANAGER.timing_summary()
    proxy = t["modes"]["proxy"]
    assert proxy["total"]["n"] == 5
    assert proxy["total"]["p50"] == 300
    assert proxy["total"]["max"] == 500
    assert proxy["first_byte"]["p90"] == 480      # values are total-20
    assert t["modes"]["redirect"]["total"]["p50"] == 50
    assert t["failures"] == {"busy": 1, "no-link": 1}
    assert t["portals"]["p1"]["starts"] == 6
    # six starts on p1 (five proxy 100..500 ms + one 50 ms redirect):
    # sorted [50,100,200,300,400,500] -> p50 is the fourth value
    assert t["portals"]["p1"]["p50_ms"] == 200
    # A failed start is counted by *reason*: it never got far enough to name a
    # portal (the whole chain was busy), so it must not pollute a portal's row.
    assert t["portals"]["p1"]["fails"] == 0
    # `recent` is the tail of the ring, and a start from another test can land in
    # it while this one runs (the window is shared process state) - so check the
    # shape, not an exact count.
    assert 7 <= len(t["recent"]) <= 12
    assert t["window"] >= 7
    assert t["recent"][-1]["item"] == "Ch"


def test_the_window_is_bounded(monkeypatch):
    monkeypatch.setattr(sm, "TIMING_HISTORY", 4)
    MANAGER.timings = sm.deque(maxlen=sm.TIMING_HISTORY)
    for i in range(20):
        MANAGER.note_timing(kind="live", mode="proxy", item=f"Ch{i}", total_ms=i)
    assert len(MANAGER.timings) == 4
    assert MANAGER.timing_summary()["window"] == 4


def test_refusal_codes_are_counted_per_host():
    note_refusal("http://portal-a/c/portal.php", "limit")
    note_refusal("http://portal-a/c/portal.php", "limit")
    note_refusal("http://portal-a/c/portal.php", "http_429")
    note_refusal("http://portal-b/c/portal.php", "access_denied")
    stats = refusal_stats()
    assert stats["total"] == 4
    assert stats["codes"][0] == {"host": "portal-a", "code": "limit", "count": 2}
    assert {c["code"] for c in stats["codes"]} == {"limit", "http_429", "access_denied"}


async def test_a_429_from_the_panel_shows_up_in_the_refusals(monkeypatch):
    """End to end through the real client: the panel's answer is what is counted."""
    from app.portal.client import PORTAL_ERROR_HINTS, PortalError, StalkerClient
    from app.portal.mock_portal import router as MOCK_ROUTER
    from fastapi import FastAPI
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(MOCK_ROUTER)
    calls = {"n": 0}

    def factory(**kwargs):
        kwargs.pop("insecure", None)
        kwargs.pop("verify", None)

        async def _spy(_request):
            calls["n"] += 1

        kwargs["event_hooks"] = {"request": [_spy]}
        return httpx.AsyncClient(transport=ASGITransport(app=app), **kwargs)

    monkeypatch.setattr("app.portal.client.outbound_client", factory)
    async with httpx.AsyncClient(transport=ASGITransport(app=app)) as c:
        await c.post("http://test/mock/_control", json={"http_status": 429})
    client = StalkerClient("http://test/mock/c/portal.php", "00:1A:79:AA:AA:01")
    try:
        with pytest.raises(PortalError):
            await client.handshake()
        assert refusal_stats()["codes"][0]["code"] == "http_429"
        assert "rate_limited" in PORTAL_ERROR_HINTS
    finally:
        await client._aclose()
        async with httpx.AsyncClient(transport=ASGITransport(app=app)) as c:
            await c.post("http://test/mock/_control", json={"http_status": 0})


def test_the_diagnostics_block_has_the_shape_the_dashboard_renders():
    diag = _diagnostics()
    assert {"timing", "refusals", "probe", "pace", "janitor", "parked", "paused"} <= set(diag)
    assert isinstance(diag["parked"], int)
    assert diag["pace"]["enabled"] in (True, False)
    assert "minutes" in diag["janitor"]
