"""Media-endpoint user-agent ladder (the HTTP 456 / "ffmpeg templates don't
play while redirect/direct does" fix).

A real MAG box contains two HTTP clients:

  * the stbapp browser, which talks portal.php with the QtEmbedded WebKit
    UA (``STB_UA``);
  * the embedded libav player, which fetches the resolved play/live.php URL
    announcing ``Lavf53.32.100`` (``PLAYER_UA``).

play/live.php-style origins answer the *browser* UA on the media endpoint with
HTTP 456 (or 403) and zero bytes while the same fresh play_token plays for the
player UA - and other panels do exactly the reverse. The media path therefore
tries player first, retries once with the browser UA on a pre-first-byte HTTP
4xx, and remembers the winner per origin host. These tests pin all of that.
"""

from __future__ import annotations

import asyncio

import pytest

from app.database import SessionLocal
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal,
)
from app.services import stream_identity
from app.services.stream_manager import MANAGER, StreamManager
from app.services.stream_identity import (
    PLAYER_UA, STB_UA, http_open_error, ladder, learned, origin_of, remember,
    reset,
)


@pytest.fixture(autouse=True)
def _clean_state():
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.streams.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()
    # a leaked instance-level _spawn override shadows class patches forever
    MANAGER.__dict__.pop("_spawn", None)
    reset()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.streams.clear()
    MANAGER.route_health.failures.clear()
    MANAGER.route_health.success.clear()
    MANAGER.__dict__.pop("_spawn", None)
    reset()


# ----------------------------------------------------------------- pure logic
def test_http_open_error_classifies_ffmpegs_answers():
    err456 = "[https @ 0x55d] HTTP error 456 Server returned 4XX Client Error"
    assert http_open_error(8, err456) == 456
    assert http_open_error(8, "[http @ x] HTTP error 403 Forbidden") == 403
    assert http_open_error(8, "[http @ x] HTTP error 404 Not Found") == 404
    assert http_open_error(8, "[http @ x] HTTP error 429 Too Many") == 429
    # ffmpeg 7 wording for an unrecognised 4xx: no numeric line, rc=1
    wrapper = ("[in#0:0] Error opening input: Server returned 4XX Client "
               "Error, but not one of 40{0,1,3,4}")
    assert http_open_error(1, wrapper) == 456


def test_http_open_error_ignores_non_identity_failures():
    assert http_open_error(8, "[http @ x] HTTP error 500 Internal") is None
    # 400 is inside ffmpeg's own auto-retry set, not an identity answer here
    assert http_open_error(8, "[http @ x] HTTP error 400 Bad Request") is None
    assert http_open_error(1, "Invalid data found when processing input") is None
    assert http_open_error(1, "") is None
    # silent stall: killed, no status, nothing to retry an identity on
    assert http_open_error(None, "") is None


def test_http_open_error_distinguishes_fast_waf_from_slow_slot_refusal():
    """Real panel trace (backup.xp1.tv): a 456 from the backend's connection
    table arrived after ~6.5 s and was UA-independent (the second MAC played
    the same browser-UA request in ~2 s); a WAF/client-shape refusal answers
    in milliseconds. The slow answer must not spend the UA retry."""
    err = "[http @ x] HTTP error 456"
    assert http_open_error(8, err, elapsed=0.4) == 456      # WAF: fast refusal
    assert http_open_error(8, err, elapsed=6.5) is None     # slot: too slow
    assert http_open_error(8, err, elapsed=None) == 456     # no timing: trust it
    # boundary uses the configurable constant
    assert stream_identity.FAST_REFUSAL_S == 5.0


def test_ladder_order_and_disable_switch(monkeypatch):
    assert ladder("http://cdn/live.php?t=1") == [PLAYER_UA, STB_UA]
    monkeypatch.setattr(stream_identity, "LADDER_ENABLED", False)
    assert ladder("http://cdn/live.php?t=1") == [STB_UA]


def test_learning_is_keyed_to_origin_not_token_or_path():
    assert origin_of("http://h.example:8043/p/a/live.php?t=abc") == "h.example:8043"
    remember("http://h.example:8043/p/a/live.php?t=abc", STB_UA)
    # different path AND a freshly-resolved token, same front end -> known
    assert learned("http://h.example:8043/p/b/live.php?t=xyz") == STB_UA
    assert learned("http://other.example/live.ts") is None
    # a learned origin tries the winner first (one request in the steady
    # state), but keeps the other identity queued for a changed policy
    assert ladder("http://h.example:8043/p/a/live.php?t=new") == \
        [STB_UA, PLAYER_UA]
    reset()
    assert learned("http://h.example:8043/p/a/live.php?t=abc") is None


# ---------------------------------------------------------------- argv render
_TPL_COPY = "ffmpeg -i <url> -c copy -f mpegts pipe:1"
_URL = "http://cdn.example.com/play/live.php?stream=1&token=abc"


def test_argv_defaults_to_the_mag_player_ua():
    argv = StreamManager._ffmpeg_argv(_TPL_COPY, _URL)
    assert argv is not None
    assert argv[argv.index("-user_agent") + 1] == PLAYER_UA
    assert STB_UA not in argv


def test_argv_accepts_a_ladder_chosen_ua():
    argv = StreamManager._ffmpeg_argv(_TPL_COPY, _URL, user_agent=STB_UA)
    assert argv is not None
    assert argv[argv.index("-user_agent") + 1] == STB_UA


def test_argv_template_pinned_ua_is_never_overridden():
    tpl = "ffmpeg -user_agent MyPlayer/9 -i <url> -c copy -f mpegts pipe:1"
    argv = StreamManager._ffmpeg_argv(tpl, _URL, user_agent=PLAYER_UA)
    assert argv is not None
    assert argv.count("-user_agent") == 1
    assert argv[argv.index("-user_agent") + 1] == "MyPlayer/9"
    assert PLAYER_UA not in argv and STB_UA not in argv


def test_argv_local_files_announce_nothing():
    argv = StreamManager._ffmpeg_argv(_TPL_COPY, "/media/movie.mp4")
    assert argv is not None
    assert "-user_agent" not in argv and "-referer" not in argv


# ------------------------------------------------------------- opener (ladder)
class _FakeOut:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProc:
    """A finished-or-finishing ffmpeg: optional first chunks, exit rc, and
    the stderr lines _drain_stderr would have parked on the process."""

    def __init__(self, chunks=(), rc=0, tail=()):
        self.stdout = _FakeOut(chunks)
        self.returncode = rc
        self.pid = 4242
        if isinstance(tail, bytes):
            tail = (tail,) if tail else ()
        self.spm_stderr_tail = list(tail)

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class _StallProc:
    """A process that opens but never sends a byte and never exits."""

    def __init__(self):
        self._go = asyncio.Event()
        self.stdout = self
        self.returncode = None
        self.pid = 4243
        self.spm_stderr_tail = []

    async def read(self, n):
        await self._go.wait()
        return b""

    async def wait(self):
        await self._go.wait()
        return self.returncode

    def kill(self):
        self.returncode = -9
        self._go.set()


_456 = b"[https @ 0x9] HTTP error 456 Server returned 4XX Client Error\n"
_403 = b"[https @ 0x9] HTTP error 403 Forbidden\n"
_TS = b"G" * 188


def _install_spawn(monkeypatch, policy):
    """policy(ua) -> _FakeProc; records (url, ua) per ffmpeg spawn.

    Patched on the CLASS (never on the MANAGER instance): instance-level
    monkeypatching makes teardown reinstate the method as a shadowing
    instance attribute, which then hides every later class-level patch.
    """
    calls = []

    async def fake_spawn(self, cmd_template, url, title=None, pace=False,
                         user_agent=None):
        calls.append((url, user_agent))
        return policy(user_agent)

    monkeypatch.setattr(StreamManager, "_spawn", fake_spawn)
    return calls


async def test_opener_plays_on_the_player_ua_and_learns_it(monkeypatch):
    calls = _install_spawn(monkeypatch, lambda ua: _FakeProc([_TS], rc=0))
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert first == _TS and fail is None and proc is not None
    assert calls == [(_URL, PLAYER_UA)]
    assert learned(_URL) == PLAYER_UA


async def test_opener_456s_player_then_plays_with_browser_ua(monkeypatch):
    """Origin Z (blocks the Lavf substring): the exact panels the browser-UA
    injection used to serve; a single-UA swap would regress them."""
    def policy(ua):
        if ua == PLAYER_UA:
            return _FakeProc([], rc=8, tail=_456)
        return _FakeProc([_TS], rc=0)

    calls = _install_spawn(monkeypatch, policy)
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert first == _TS and fail is None and proc is not None
    # the ladder reopens the SAME resolved link - no fresh create_link
    assert calls == [(_URL, PLAYER_UA), (_URL, STB_UA)]
    assert learned(_URL) == STB_UA


async def test_opener_gives_up_after_both_identities_refused(monkeypatch):
    calls = _install_spawn(
        monkeypatch, lambda ua: _FakeProc([], rc=8, tail=_403))
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert proc is None and first == b""
    assert fail is not None and fail["rc"] == 8 and "403" in fail["tail"]
    assert calls == [(_URL, PLAYER_UA), (_URL, STB_UA)]
    assert learned(_URL) is None          # nothing the origin accepted


async def test_opener_silent_stall_does_not_spend_the_browser_retry(monkeypatch):
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_TIMEOUT", 0.15)
    calls = _install_spawn(monkeypatch, lambda ua: _StallProc())
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert proc is None and fail is not None and fail["stalled"] is True
    assert calls == [(_URL, PLAYER_UA)]  # a timeout is not an identity answer


async def test_opener_slow_456_skips_the_ua_rung_and_goes_to_mac_fallback(monkeypatch):
    """Panel connection-slot refusal: the 456 arrives after the fast-refusal
    window (real trace: 6.5 s). Swapping the UA cannot free a slot, so the
    opener must NOT spend the browser rung - it hands back the failure and
    the pump walks the next MAC/source immediately."""
    monkeypatch.setattr(stream_identity, "FAST_REFUSAL_S", 0.0)
    calls = _install_spawn(
        monkeypatch, lambda ua: _FakeProc([], rc=8, tail=_456))
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert proc is None and fail is not None and fail["rc"] == 8
    assert calls == [(_URL, PLAYER_UA)]   # one attempt, straight to MAC fallback


async def test_opener_fast_456_walks_player_to_browser_even_with_tight_window(monkeypatch):
    """Sanity counterpart of the slow case: with the normal window a WAF that
    answers a 456 immediately (the fakes answer in ~0 ms) still walks."""
    monkeypatch.setattr(stream_identity, "FAST_REFUSAL_S", 5.0)
    calls = _install_spawn(
        monkeypatch,
        lambda ua: _FakeProc([], rc=8, tail=_456) if ua == PLAYER_UA
        else _FakeProc([_TS], rc=0))
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert first == _TS and fail is None
    assert calls == [(_URL, PLAYER_UA), (_URL, STB_UA)]


async def test_opener_local_file_skips_the_ladder(monkeypatch):
    calls = _install_spawn(
        monkeypatch, lambda ua: _FakeProc([], rc=8, tail=b"No such file\n"))
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, "/media/movie.mp4", title="Ch", pace=True)
    assert proc is None and fail is not None
    assert calls == [("/media/movie.mp4", None)]   # one attempt, no UA swap


async def test_opener_template_pinned_ua_skips_the_ladder(monkeypatch):
    tpl = "ffmpeg -user_agent MyPlayer/9 -i <url> -c copy -f mpegts pipe:1"
    calls = _install_spawn(
        monkeypatch, lambda ua: _FakeProc([], rc=8, tail=_456))
    proc, first, fail = await MANAGER._open_with_identity(
        tpl, _URL, title="Ch", pace=False)
    assert proc is None and fail is not None
    assert calls == [(_URL, None)]      # explicit choice, even on 456 no swap


async def test_opener_learned_origin_is_asked_once_with_the_winner(monkeypatch):
    remember(_URL, STB_UA)
    calls = _install_spawn(monkeypatch, lambda ua: _FakeProc([_TS], rc=0))
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert first == _TS and fail is None
    assert calls == [(_URL, STB_UA)]


async def test_opener_relearns_when_a_learned_winner_changes_policy(monkeypatch):
    """The origin used to accept the browser UA; its WAF now 456s it and only
    plays the player UA. The learned-first ladder must fall through and
    re-learn within the same play, not hand the viewer a black screen."""
    remember(_URL, STB_UA)

    def policy(ua):
        if ua == STB_UA:
            return _FakeProc([], rc=8, tail=_456)
        return _FakeProc([_TS], rc=0)

    calls = _install_spawn(monkeypatch, policy)
    proc, first, fail = await MANAGER._open_with_identity(
        _TPL_COPY, _URL, title="Ch", pace=False)
    assert first == _TS and fail is None
    assert calls == [(_URL, STB_UA), (_URL, PLAYER_UA)]
    assert learned(_URL) == PLAYER_UA


# ------------------------------------------------------------- full pump path
async def _live_route():
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/",
                        resolved_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        s.add(MacAddress(portal_id=portal.id, mac="00:1A:79:00:00:09",
                         status="online", order=0))
        src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                         original_name="Ch", cmd="ffmpeg http://cdn/x.ts",
                         enabled=True, link_flags="use_http_tmp_link")
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
        return self._script[min(self.calls - 1, len(self._script) - 1)]


class _FakePool:
    def __init__(self, client):
        self._client = client

    async def get(self, session):
        return self._client


async def test_pump_ladder_does_not_re_ask_the_panel_then_learning_makes_it_one_spawn(
        monkeypatch):
    """End to end through the real pump: an origin that 403s the player UA
    and plays the browser UA must play after one same-link respawn (the
    resolved play_token is not re-resolved), and the next play uses the
    learned identity with a single spawn."""
    pid = await _live_route()
    links = ["http://cdn/play/live.php?t=one",
             "http://cdn/play/live.php?t=two"]
    client = _FakeClient(links)
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY", False)
    spawns = []

    def policy(ua):
        if ua == PLAYER_UA:
            return _FakeProc([], rc=8, tail=_403)
        return _FakeProc([_TS], rc=0)

    async def fake_spawn(self, cmd_template, url, title=None, pace=False,
                         user_agent=None):
        spawns.append((url, user_agent))
        return policy(user_agent)

    monkeypatch.setattr(StreamManager, "_spawn", fake_spawn)

    _h, gen = await MANAGER.open("live", pid, "zapbox")
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()
    assert first == _TS
    # ladder rungs reopen the SAME create_link result - one panel call
    assert spawns == [(links[0], PLAYER_UA), (links[0], STB_UA)]
    assert client.calls == 1
    assert learned(links[0]) == STB_UA

    # second play: fresh token from the panel, one spawn with the winner
    _h2, gen2 = await MANAGER.open("live", pid, "zapbox")
    try:
        first2 = await gen2.__anext__()
    finally:
        await gen2.aclose()
    assert first2 == _TS
    assert spawns[2:] == [(links[1], STB_UA)]
    assert client.calls == 2
