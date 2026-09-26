"""
The portal traffic log: what we asked each panel, and what it answered.

The diagnostics card has always answered "why is zapping slow" from aggregates -
percentiles, refusal counts. What it could not answer is the question a support
conversation actually starts with: *which request, at what time, and what exactly
did the panel say back?* That needed `docker logs` and a grep, and the container
log has no idea what the request looked like.

So every panel call is recorded as one row - sent at, round trip, host, MAC,
the request line, the HTTP status, the refusal code, and a bounded summary of the
answer - in a bounded in-memory ring (see app/services/portal_traffic.py). These
tests pin the three things that make the rows trustworthy:

  * a row exists for every exit, including the ones where nothing was sent,
  * the timestamps and durations are real, not decoration,
  * no credential ever reaches a row (a bearer in a screenshot is a leak).
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.portal.client import (RATE_LIMITED_CODE, PortalError, note_rate_limit,
                               reset_rate_limits, reset_refusals)
from app.routers.api_misc import _diagnostics
from app.services import portal_traffic as pt
from mockclient import GOOD, PORTAL, Wired

PORTAL_A = "http://portal-a/c/portal.php"
PORTAL_B = "http://portal-b/c/portal.php"


@pytest.fixture(autouse=True)
def _clean():
    pt.clear()
    reset_rate_limits()
    reset_refusals()
    yield
    pt.clear()
    reset_rate_limits()
    reset_refusals()


# --------------------------------------------------------------------------- #
# the row itself
# --------------------------------------------------------------------------- #
def test_an_exchange_records_the_request_the_answer_and_when_it_happened():
    ex = pt.exchange(PORTAL_A, GOOD, {"type": "itv", "action": "create_link",
                                      "cmd": "ffmpeg http://cdn/1.ts",
                                      "JsHttpRequest": "1-xml"})
    row = ex.done(status=200, size=1400,
                  data={"js": {"cmd": "http://cdn/1.ts", "id": "7"}})
    assert row["host"] == "portal-a"
    assert row["mac"] == GOOD
    assert row["type"] == "itv" and row["action"] == "create_link"
    assert row["params"] == "type=itv&action=create_link&cmd=ffmpeg http://cdn/1.ts"
    assert row["outcome"] == "ok" and row["status"] == 200 and row["bytes"] == 1400
    assert "cmd=http://cdn/1.ts" in row["answer"] and "id=7" in row["answer"]
    # a catalogue page answers {"js":{"data":[…]}} - the count is the summary
    assert pt.summarize_answer({"js": {"data": [{}] * 12, "total_items": 400}}) == \
        "data: 12 item(s), total_items=400"
    # the clock is the point of the feature
    assert row["t"] == pytest.approx(time.time(), abs=5)
    assert row["ms"] >= 0
    stats = pt.stats()
    assert stats["kept"] == 1 and stats["counts"]["ok"] == 1
    assert stats["last_minute"] == 1
    assert stats["hosts"] == [{"host": "portal-a", "count": 1}]


def test_closing_an_exchange_twice_writes_one_row():
    """The `finally` net must not double-count a request that reported itself."""
    ex = pt.exchange(PORTAL_A, GOOD, {"action": "create_link"})
    assert ex.done(status=200)["id"] == 1
    assert ex.done(status=200) == {}
    assert pt.stats()["kept"] == 1


def test_no_credential_reaches_a_row():
    ex = pt.exchange(PORTAL_A, GOOD,
                     {"type": "stb", "action": "handshake", "prehash": "abc123",
                      "token": "", "mac": GOOD})
    row = ex.done(status=200, data={"js": {"token": "s3cr3t",
                                           "cmd": "http://cdn/1.ts?play_token=deadbeef"}})
    assert "abc123" not in row["params"] and "prehash=***" in row["params"]
    assert "s3cr3t" not in row["answer"] and "token=***" in row["answer"]
    assert "deadbeef" not in row["answer"] and "play_token=***" in row["answer"]


def test_the_ring_is_bounded(monkeypatch):
    """A catalogue walk must not be able to grow the log without limit."""
    monkeypatch.setattr(pt, "TRAFFIC_HISTORY", 4)
    monkeypatch.setattr(pt, "_ring", deque(maxlen=4))
    for i in range(20):
        pt.record(portal_url=PORTAL_A, mac=GOOD, params={"action": f"a{i}"}, status=200)
    kept = pt.query(per_page=50)
    assert kept["total"] == 4
    assert [r["action"] for r in kept["items"]] == ["a19", "a18", "a17", "a16"], \
        "newest first, oldest dropped"


def test_spm_portal_traffic_zero_turns_the_log_off(monkeypatch):
    monkeypatch.setattr(pt, "TRAFFIC_HISTORY", 0)
    assert pt.exchange(PORTAL_A, GOOD, {"action": "x"}).done(status=200) == {}
    assert pt.stats()["kept"] == 0


# --------------------------------------------------------------------------- #
# filtering / paging - what the dashboard's table and its header controls send
# --------------------------------------------------------------------------- #
def _seed() -> None:
    pt.record(portal_url=PORTAL_A, mac="AA", status=200,
              params={"type": "itv", "action": "get_all_channels"},
              answer=pt.summarize_answer({"js": [{}] * 12}))
    pt.record(portal_url=PORTAL_A, mac="AA", status=200, code="limit", error="limit",
              params={"type": "vod", "action": "create_link"})
    pt.record(portal_url=PORTAL_B, mac="BB", status=0, code="timeout",
              error="timeout after 8s", params={"type": "itv", "action": "create_link"})
    pt.record(portal_url=PORTAL_B, mac="BB", status=0, code=RATE_LIMITED_CODE,
              skipped=True, error="not sent - portal host paused for 30s",
              params={"type": "stb", "action": "handshake"})


def test_the_log_can_be_filtered_by_portal_outcome_and_text():
    _seed()
    assert pt.query()["total"] == 4
    assert pt.query()["items"][0]["action"] == "handshake", "newest first"
    assert pt.query(host="PORTAL-B")["total"] == 2, "the host filter is case-insensitive"
    assert pt.query(outcome="errors")["total"] == 3, "refused + failed + skipped"
    assert pt.query(outcome="ok")["total"] == 1
    assert pt.query(outcome="refused")["total"] == 1
    assert pt.query(outcome="failed")["total"] == 1
    assert pt.query(outcome="skipped")["total"] == 1
    assert pt.query(q="GET_ALL_CHANNELS")["total"] == 1
    # a text filter searches, it does not parse: `rate_limited` contains `limit`
    assert pt.query(q="limit")["total"] == 2
    assert pt.query(q="code=limit")["total"] == 0
    assert pt.query(host="portal-a", outcome="errors")["total"] == 1


def test_paging_and_the_facets_the_header_controls_are_filled_from():
    _seed()
    page2 = pt.query(page=2, per_page=3)
    assert page2["total"] == 4 and len(page2["items"]) == 1
    assert page2["per_page"] == 3 and page2["page"] == 2
    assert pt.query(per_page=1000)["per_page"] == 200, "the bound protects the response"
    stats = pt.stats()
    assert stats["hosts"][0] == {"host": "portal-a", "count": 2}
    assert {c["code"] for c in stats["codes"]} == {"limit", "timeout", RATE_LIMITED_CODE}


# --------------------------------------------------------------------------- #
# end to end: a real client against the real mock portal
# --------------------------------------------------------------------------- #
async def test_a_real_catalogue_fetch_lands_in_the_log_with_timestamps(monkeypatch):
    wired = Wired(monkeypatch)
    client = wired.client()
    try:
        await client.ensure_auth()
        await client.all_channels()
    finally:
        await client._aclose()
    rows = pt.query(per_page=100)["items"]
    actions = [r["action"] for r in rows]
    assert "handshake" in actions and "get_all_channels" in actions
    hs = next(r for r in rows if r["action"] == "handshake")
    assert hs["status"] == 200 and hs["outcome"] == "ok"
    assert "token issued" in hs["answer"] and "***" in hs["params"], \
        "the handshake shape is visible, its prehash is not"
    ch = next(r for r in rows if r["action"] == "get_all_channels")
    assert ch["outcome"] == "ok" and ch["bytes"] > 0 and ch["ms"] >= 0
    assert "item(s)" in ch["answer"]
    assert ch["t"] == pytest.approx(time.time(), abs=30)
    assert all(r["host"] == "test" for r in rows)
    # the ring is in the order the panel saw the requests
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)


async def test_a_429_is_a_refusal_and_the_requests_it_stopped_are_visible(monkeypatch):
    """Both halves matter: the answer that paused us, and what we then held back."""
    wired = Wired(monkeypatch)
    await wired.control(http_status=429)
    client = wired.client()
    try:
        with pytest.raises(PortalError):
            await client.handshake()
        with pytest.raises(PortalError):
            await client.all_channels()
    finally:
        await client._aclose()
        await wired.control(http_status=0)
    rows = pt.query(per_page=100)["items"]
    refused = [r for r in rows if r["outcome"] == "refused"]
    assert refused, "the 429 itself must be a row"
    assert refused[0]["status"] == 429 and refused[0]["code"] == RATE_LIMITED_CODE
    skipped = [r for r in rows if r["outcome"] == "skipped"]
    assert len(skipped) == 1, "the follow-up was never sent - and says so"
    assert skipped[0]["status"] == 0 and skipped[0]["action"] == "get_all_channels"
    assert "paused" in skipped[0]["error"]
    assert pt.stats()["counts"]["skipped"] == 1


async def test_a_handshake_we_held_back_is_a_row_too(monkeypatch):
    """`handshake()` bails out before opening a socket - and must still say so.

    Without this the log went quiet exactly when the proxy was refusing to ask,
    which reads as "we never tried" instead of "we tried and stopped".
    """
    wired = Wired(monkeypatch)
    client = wired.client()
    note_rate_limit(PORTAL, retry_after=30, reason="test")
    try:
        with pytest.raises(PortalError):
            await client.handshake()
    finally:
        await client._aclose()
    rows = pt.query(outcome="skipped")["items"]
    assert len(rows) == 1
    assert rows[0]["action"] == "handshake" and rows[0]["status"] == 0
    assert "paused" in rows[0]["error"]


async def test_an_unreachable_portal_is_recorded_as_no_answer(monkeypatch):
    wired = Wired(monkeypatch)
    client = wired.client()
    await wired.control(http_status=503)
    try:
        with pytest.raises(PortalError):
            await client.handshake()
    finally:
        await client._aclose()
        await wired.control(http_status=0)
    rows = pt.query(outcome="errors")["items"]
    assert rows and rows[0]["code"] == "http_503"
    assert rows[0]["status"] == 503


# --------------------------------------------------------------------------- #
# the API and the dashboard behind it
# --------------------------------------------------------------------------- #
async def test_the_endpoint_serves_the_rows_and_what_each_code_means():
    pt.record(portal_url=PORTAL_A, mac="AA", status=200, code="limit", error="limit",
              params={"type": "vod", "action": "create_link"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        body = (await c.get("/api/portal-traffic")).json()
        assert body["total"] == 1 and body["items"][0]["code"] == "limit"
        assert {"kept", "history", "counts", "hosts", "codes", "last_minute"} <= set(body)
        # the GUI explains a refusal in words instead of showing a bare code
        assert body["hints"]["limit"]
        assert (await c.get("/api/portal-traffic?host=portal-a&outcome=errors&q=create_link")
                ).json()["total"] == 1
        assert (await c.post("/api/portal-traffic/clear")).json() == {"ok": True}
        assert (await c.get("/api/portal-traffic")).json()["total"] == 0


def test_the_diagnostics_card_reports_the_log_it_links_to():
    _seed()
    diag = _diagnostics()
    assert diag["traffic"]["kept"] == 4
    assert diag["traffic"]["counts"]["refused"] == 1


async def test_the_dashboard_shows_panel_answers_first_and_the_traffic_log_below():
    """The swap the page was asked for, plus the table that backs it."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        page = (await c.get("/dashboard")).text
    assert page.index("Zapping &amp; panel answers") < page.index("API status</span>"), \
        "panel answers lead the right column now, API status follows"
    assert 'id="portal-traffic"' in page
    assert 'id="traffic-table"' in page and 'id="traffic-outcome"' in page
    assert "/api/portal-traffic?" in page, "the table has to ask the new endpoint"
    assert "Portal traffic — requests &amp; answers" in page
    # the swap is in the template, not only in the rendered page
    template = (Path(__file__).resolve().parents[1]
                / "app/templates/dashboard.html").read_text()
    assert template.index("Zapping &amp; panel answers") < template.index("API status")
