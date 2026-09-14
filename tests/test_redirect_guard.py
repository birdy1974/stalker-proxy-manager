"""Redirect guard (features 1+2): open-time validation + reopen demotion.

Self-contained: if the feature is deleted, delete this file with it.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.database import SessionLocal
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal,
)
from app.services import redirect_guard
from app.services.redirect_guard import (
    ProbeResult, demote_recently_handed, link_is_alive, note_handed_out,
)
from app.services.stream_manager import MANAGER


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # conftest disables the guard suite-wide; this file is where it is ON.
    monkeypatch.setattr(redirect_guard, "VALIDATE_ENABLED", True)
    monkeypatch.setattr(redirect_guard, "DEMOTE_ENABLED", True)
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.streams.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()
    redirect_guard.reset()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.streams.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()
    redirect_guard.reset()


async def _two_portal_route():
    """One playlist item with two sources on two portals (two MACs), so one
    resolve's redirect lease never blocks the next resolve in these tests."""
    async with SessionLocal() as s:
        src_ids = []
        for i in (1, 2):
            portal = Portal(name=f"p{i}", base_url=f"http://127.0.0.{i}:1/c/",
                            resolved_url=f"http://127.0.0.{i}:1/c/")
            s.add(portal)
            await s.flush()
            s.add(MacAddress(portal_id=portal.id, mac=f"00:1A:79:00:00:0{i}",
                             status="online", order=0))
            src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                             original_name="Ch", cmd=f"ffmpeg http://cdn/{i}.ts",
                             enabled=True, link_flags="use_http_tmp_link")
            s.add(src)
            await s.flush()
            src_ids.append(src.id)
        pl = LivePlaylist(custom_name="Ch", enabled=True)
        s.add(pl)
        await s.flush()
        for prio, sid in enumerate(src_ids, 1):
            s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=sid,
                                     priority=prio))
        await s.commit()
        return pl.id


class _FakeClient:
    def __init__(self):
        self.calls = 0

    async def ensure_auth(self):
        return None

    def invalidate(self):
        pass

    async def close(self):
        return None

    async def create_link(self, cmd, kind="live", **kw):
        self.calls += 1
        return f"{cmd.split()[-1]}?play_token=t{self.calls}"


class _FakePool:
    def __init__(self, client):
        self._client = client

    async def get(self, session):
        return self._client


# ---------------------------------------------------------- feature 1: resolve
async def test_validation_skips_a_dead_first_link(monkeypatch):
    """The first candidate's link is dead: no 302 to black - the second
    candidate's link is handed out instead."""
    pid = await _two_portal_route()
    client = _FakeClient()
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))

    async def fake_alive(url, **kw):
        ok = "one" not in url and "/1.ts" not in url
        return ProbeResult(ok, "fake")

    monkeypatch.setattr("app.services.stream_manager.link_is_alive", fake_alive)
    url, _name = await MANAGER.resolve("live", pid)
    assert url is not None and "/2.ts" in url
    assert client.calls == 2           # both candidates were asked in order


async def test_validation_disabled_hands_out_untested(monkeypatch):
    monkeypatch.setattr("app.services.redirect_guard.VALIDATE_ENABLED", False)
    assert (await link_is_alive("http://cdn/x.ts", client=None)).alive is True


# --------------------------------------------------------- feature 2: resolve
async def test_rapid_reopen_demotes_the_last_handoff(monkeypatch):
    """Same route re-asked: the last-handed source moves last, so the reopen
    tries the other route first. A third reopen rotates back (each handoff
    refreshes the memory). Leases are cleared between opens: they are a
    separate mechanism, not under test here."""
    pid = await _two_portal_route()
    monkeypatch.setattr("app.services.stream_manager.POOL",
                        _FakePool(_FakeClient()))

    async def always_alive(url, **kw):
        return ProbeResult(True, "fake")

    monkeypatch.setattr("app.services.stream_manager.link_is_alive", always_alive)
    first, _ = await MANAGER.resolve("live", pid)
    assert first is not None and "/1.ts" in first
    MANAGER.redirect_leases.clear()
    second, _ = await MANAGER.resolve("live", pid)
    assert second is not None and "/2.ts" in second
    MANAGER.redirect_leases.clear()
    third, _ = await MANAGER.resolve("live", pid)
    assert third is not None and "/1.ts" in third


async def test_reopen_after_the_window_keeps_the_primary(monkeypatch):
    monkeypatch.setattr("app.services.redirect_guard.DEMOTE_WINDOW", -1.0)
    pid = await _two_portal_route()
    monkeypatch.setattr("app.services.stream_manager.POOL",
                        _FakePool(_FakeClient()))

    async def always_alive(url, **kw):
        return ProbeResult(True, "fake")

    monkeypatch.setattr("app.services.stream_manager.link_is_alive", always_alive)
    first, _ = await MANAGER.resolve("live", pid)
    MANAGER.redirect_leases.clear()
    second, _ = await MANAGER.resolve("live", pid)
    assert first is not None and second is not None
    assert "/1.ts" in first and "/1.ts" in second


# ------------------------------------------------------------------ demote unit
def _step(sid, mids):
    src = SimpleNamespace(id=sid)
    macs = [SimpleNamespace(id=m) for m in mids]
    return (src, SimpleNamespace(id=sid), macs)


def test_demote_moves_handed_step_and_mac_last():
    chain = [_step(1, (11, 12)), _step(2, (21,))]
    note_handed_out(("live", 7), chain[0][0], chain[0][2][0])
    out = demote_recently_handed(("live", 7), chain)
    assert [s.id for s, _, _ in out] == [2, 1]
    assert [m.id for m in out[1][2]] == [12, 11]


def test_demote_ignores_unknown_and_expired_routes(monkeypatch):
    chain = [_step(1, (11,)), _step(2, (21,))]
    assert demote_recently_handed(("live", 999), chain) is chain
    note_handed_out(("live", 7), chain[0][0], chain[0][2][0])
    monkeypatch.setattr("app.services.redirect_guard.DEMOTE_WINDOW", -1.0)
    assert demote_recently_handed(("live", 7), chain) == chain


def test_demote_without_mac_keeps_mac_order():
    chain = [_step(1, (11, 12)), _step(2, (21,))]
    note_handed_out(("live", 7), chain[0][0], None)   # adopted step: no MAC
    out = demote_recently_handed(("live", 7), chain)
    assert [s.id for s, _, _ in out] == [2, 1]
    assert [m.id for m in out[1][2]] == [11, 12]


def test_note_handed_out_disabled_records_nothing(monkeypatch):
    monkeypatch.setattr("app.services.redirect_guard.DEMOTE_ENABLED", False)
    chain = [_step(1, (11,)), _step(2, (21,))]
    note_handed_out(("live", 7), chain[0][0], chain[0][2][0])
    assert demote_recently_handed(("live", 7), chain) is chain


# --------------------------------------------------------------- validation unit
def _recording_client(handler):
    seen = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(h)), seen


async def test_alive_on_head_ok_without_get():
    client, seen = _recording_client(lambda r: httpx.Response(200))
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD 200"
        assert bool(result) is True
        assert [(r.method, r.url.path) for r in seen] == [("HEAD", "/x.ts")]
    finally:
        await client.aclose()


async def test_head_405_falls_back_to_ranged_get():
    def h(request):
        if request.method == "HEAD":
            return httpx.Response(405)
        assert request.headers["Range"] == "bytes=0-0"
        return httpx.Response(206)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD 405 -> GET 206"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_head_502_falls_back_to_ranged_get():
    """A gateway error on HEAD is NOT proof the stream is dead: several
    panels' stream origins answer HEAD with 502 while serving the very same
    URL to GET. Confirm with the ranged GET before vetoing."""
    def h(request):
        if request.method == "HEAD":
            return httpx.Response(502)
        assert request.headers["Range"] == "bytes=0-0"
        return httpx.Response(206)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD 502 -> GET 206"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_dead_when_head_and_get_both_502():
    """Both probes 502: the stream side really is down -> veto, with the
    full trace in the detail for the operator."""
    client, seen = _recording_client(lambda r: httpx.Response(502))
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is False
        assert result.detail == "HEAD 502 -> GET 502"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_dead_when_get_also_fails():
    def h(request):
        return httpx.Response(405 if request.method == "HEAD" else 404)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is False
        assert result.detail == "HEAD 405 -> GET 404"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_dead_on_head_404_without_get():
    client, seen = _recording_client(lambda r: httpx.Response(404))
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is False
        assert result.detail == "HEAD 404"
        assert [r.method for r in seen] == ["HEAD"]
    finally:
        await client.aclose()


async def test_dead_on_head_error_without_get():
    """Connect-level only: nothing is listening, so a GET would fail the same
    way and the ladder stops at the first rung. Read-level errors are a shrug
    and climb instead - see the tests below."""
    def h(request):
        raise httpx.ConnectError("refused")

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is False
        assert result.detail == "HEAD ConnectError"
        assert [r.method for r in seen] == ["HEAD"]
    finally:
        await client.aclose()


async def test_non_http_urls_are_never_vetoed():
    client, seen = _recording_client(lambda r: httpx.Response(500))
    try:
        result = await link_is_alive("rtmp://cdn/x", client=client)
        assert result.alive is True
        assert seen == []
    finally:
        await client.aclose()


def test_referer_is_the_origin_root():
    assert (redirect_guard._referer_of("https://cdn.example.com:8080/a/b.ts?x=1")
            == "https://cdn.example.com:8080/")


def test_probe_ua_is_the_stb_ua():
    from app.portal.identity import STB_UA

    assert redirect_guard.STB_UA is STB_UA


# --------------------------------------- validation: a transport error is a shrug
# The shape a live-TS origin produces when it cannot answer a probe at all: it
# takes the connection and hangs up mid-response. That is "I do not do HEAD",
# not "the channel is off air", and treating it as the latter vetoed every
# fresh link of a working portal (nexusconnects-style: 3 MACs x 2 passes, then
# a 502 after 13-33 s, while the very same URLs played fine).
async def test_head_read_error_falls_back_to_ranged_get():
    def h(request):
        if request.method == "HEAD":
            raise httpx.ReadError("peer closed connection without a complete body")
        assert request.headers["Range"] == "bytes=0-0"
        return httpx.Response(200)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/play/live.php?stream=1", client=client)
        assert result.alive is True
        assert result.detail == "HEAD ReadError -> GET 200"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_head_protocol_error_is_a_shrug_too():
    def h(request):
        if request.method == "HEAD":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(206)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD RemoteProtocolError -> GET 206"
    finally:
        await client.aclose()


async def test_head_read_timeout_is_a_shrug_too():
    """A live origin that takes longer than the probe timeout to answer a HEAD
    is thinking, not dead - the player would simply buffer."""
    def h(request):
        if request.method == "HEAD":
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200)

    client, _seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD ReadTimeout -> GET 200"
    finally:
        await client.aclose()


async def test_a_range_shy_live_stream_is_alive_on_416():
    """A live stream has no byte 0 to range over: 416 proves the origin is
    there and answering, which is all the guard asks."""
    def h(request):
        if request.method == "HEAD":
            return httpx.Response(502)
        return httpx.Response(416 if "Range" in request.headers else 200)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD 502 -> GET 416"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_the_plain_get_is_the_last_opinion_when_the_ranged_one_shrugs():
    """Some origins choke on the Range header itself. The third rung asks
    without it - byte-for-byte the request a player makes."""
    def h(request):
        if request.method == "HEAD":
            raise httpx.ReadError("hung up")
        if "Range" in request.headers:
            raise httpx.ReadError("hung up")
        return httpx.Response(200)

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == "HEAD ReadError -> GET ReadError -> GET-no-range 200"
        assert [r.method for r in seen] == ["HEAD", "GET", "GET"]
        assert "Range" not in seen[-1].headers
    finally:
        await client.aclose()


async def test_an_origin_that_shrugs_at_every_rung_is_not_vetoed():
    """Nothing was proved, so nothing may veto: a false 'dead' is a certain
    502 on a channel that may well be on air, a false 'alive' costs one 302
    the player can recover from."""
    def h(request):
        raise httpx.ReadError("hung up")

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is True
        assert result.detail == ("HEAD ReadError -> GET ReadError -> "
                                 "GET-no-range ReadError -> no verdict, not vetoing")
        assert [r.method for r in seen] == ["HEAD", "GET", "GET"]
    finally:
        await client.aclose()


async def test_dead_when_the_origin_stops_listening_midway():
    """A connect-level failure IS proof - nothing is at the other end, and
    asking again in another shape would not change that."""
    def h(request):
        if request.method == "HEAD":
            return httpx.Response(405)
        raise httpx.ConnectError("refused")

    client, seen = _recording_client(h)
    try:
        result = await link_is_alive("http://cdn/x.ts", client=client)
        assert result.alive is False
        assert result.detail == "HEAD 405 -> GET ConnectError"
        assert [r.method for r in seen] == ["HEAD", "GET"]
    finally:
        await client.aclose()


async def test_the_probe_budget_ends_in_alive_not_in_a_veto():
    """A slow origin must not turn one play into seconds of probing per
    candidate: running out of budget is 'unproven', and unproven is alive."""
    client, seen = _recording_client(lambda r: httpx.Response(502))
    try:
        result = await link_is_alive("http://cdn/x.ts", timeout=0.0, client=client)
        assert result.alive is True
        assert result.detail == "HEAD 502 -> probe budget spent"
        assert [r.method for r in seen] == ["HEAD"]
    finally:
        await client.aclose()


def test_shrug_note_only_speaks_when_the_ladder_was_climbed():
    from app.services.redirect_guard import shrug_note

    assert shrug_note("http://cdn/x.ts", ProbeResult(True, "HEAD 200")) == ""
    assert shrug_note("http://cdn/x.ts", None) == ""
    note = shrug_note("http://cdn/x.ts", ProbeResult(True, "HEAD 405 -> GET 206"))
    assert "probe-shy" in note and "HEAD 405 -> GET 206" in note and "cdn" in note


def test_shrug_note_is_reported_once_per_origin_and_trace():
    """The fact matters, the repetition on every play does not: a panel that
    never answers HEAD must not add a line to every channel's every play."""
    from app.services.redirect_guard import shrug_note

    probe = ProbeResult(True, "HEAD ReadError -> GET-no-range 200")
    assert shrug_note("http://panel/play/live.php?stream=1", probe) != ""
    assert shrug_note("http://panel/play/live.php?stream=2", probe) == ""
    # another origin, or another trace from the same one, is news again
    assert shrug_note("http://other/play/live.php?stream=1", probe) != ""
    assert shrug_note("http://panel/x.ts", ProbeResult(True, "HEAD 502 -> GET 206")) != ""
