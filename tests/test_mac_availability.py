"""
"Can I see whether a MAC is available before I connect to it?"

Three things decide that question, and they are all tested here:

  * **our occupancy** - an ffmpeg pipe (`mac_locks`) or a post-302 redirect
    lease. The lease now remembers *who* took it (`lease_meta`), because a
    lease is a time-bounded guess: a zap by the same user must take over the
    channel it just left, while a different user's MAC stays off-limits.
  * **the engine's budget** - the fallback chain takes
    `candidates x STREAM_START_TIMEOUT x passes` seconds, so the output guard
    has to follow that number instead of firing at a fixed 25s while the pump
    is still walking (the "produced no data within 25s -> 502" of the report).
  * **the panel itself** - `app/services/mac_probe.py`. A Stalker panel has no
    "who is using this MAC" call: the refusal code of a real `create_link`
    (limit / account is in use / access denied) is the answer, and a link that
    then streams bytes is the proof that nobody else holds the slot.
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.database import SessionLocal
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal,
)
from app.portal.mock_portal import _STATE
from app.routers.output import FIRST_CHUNK_TIMEOUT, _guard_wait
from app.services import mac_probe, stream_identity
from app.services.stream_manager import (
    MANAGER, START_BUDGET_SLACK, STREAM_START_BUDGET, STREAM_START_TIMEOUT,
    StreamHandle, StreamManager,
)
from mockclient import GOOD, PORTAL, Wired


@pytest.fixture(autouse=True)
def _clean_manager():
    """MANAGER is a process-global singleton: leases and locks from one test
    must not decide the next one's answers."""
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()


# =========================================================================== #
# our own view: who holds the lease, and who may take it over
# =========================================================================== #
def test_a_redirect_lease_remembers_who_took_it():
    MANAGER.lease_mac(7, seconds=180, holder="bert", item="Npo 1",
                      kind="live", ref=2)
    assert MANAGER.lease_holder(7) == "bert"
    info = MANAGER.mac_occupancy(7)
    assert info["busy"] is True and info["reason"] == "lease"
    assert info["holder"] == "bert" and info["item"] == "Npo 1"
    assert info["remaining_s"] > 170 and info["ref"] == 2
    assert MANAGER.lease_remaining(7) > 170


def test_the_same_user_may_take_over_the_lease_a_zap_left_behind():
    """The reported failure: /play/live/5.ts 302'd the player to the CDN and
    leased the MAC for 180s; the zap to channel 2 then skipped that MAC
    ("mac ... busy -> skip") and fell to MACs that produced no data."""
    MANAGER.lease_mac(6, seconds=180, holder="bert", item="Npo 1")
    assert MANAGER.is_mac_busy(6) is True                       # health sweeps
    assert MANAGER.is_mac_busy(6, requester="anna") is True      # another user
    assert MANAGER.is_mac_busy(6, requester="bert") is False     # the zap


def test_an_anonymous_lease_is_never_taken_over():
    """A caller that did not say who it plays for (GUI quick play, a probe)
    must not let any user walk onto the MAC."""
    MANAGER.lease_mac(6, seconds=180)                            # holder None
    assert MANAGER.lease_holder(6) is None
    assert MANAGER.is_mac_busy(6, requester="bert") is True


def test_an_ffmpeg_pipe_is_never_taken_over_even_by_its_own_user():
    """A lock is a real concurrent stream, not a guess - the same user opening
    a second channel must still be told to wait."""
    MANAGER.mac_locks[6] = "stream-a"
    assert MANAGER.is_mac_busy(6, requester="bert") is True
    info = MANAGER.mac_occupancy(6)
    assert info["reason"] == "pipe" and info["stream_id"] == "stream-a"


def test_expired_leases_report_free_and_drop_their_holder():
    MANAGER.lease_mac(6, seconds=30, holder="bert")
    MANAGER.redirect_leases[6] = time.monotonic() - 1
    assert MANAGER.is_mac_busy(6) is False
    assert 6 not in MANAGER.redirect_leases
    assert 6 not in MANAGER.lease_meta
    assert MANAGER.mac_occupancy(6) == {"busy": False, "reason": "free",
                                        "remaining_s": 0.0}


def test_occupancy_map_lists_only_what_is_busy_now():
    MANAGER.lease_mac(6, seconds=60, holder="bert", item="Npo 1")
    MANAGER.mac_locks[9] = "stream-b"
    MANAGER.streams["stream-b"] = StreamHandle(id="stream-b", kind="live",
                                               item_name="Ch", user_name="anna",
                                               template_name="Copy",
                                               command="ffmpeg {url}")
    occ = MANAGER.occupancy_map()
    assert set(occ) == {6, 9}
    assert occ[9]["reason"] == "pipe" and occ[9]["holder"] == "anna"
    assert occ[6]["reason"] == "lease"


def test_the_all_busy_log_says_which_mac_and_why():
    MANAGER.lease_mac(6, seconds=42, holder="bert", item="Npo 1")
    note = MANAGER._occupied_note(type("M", (), {"id": 6, "mac": "00:1A:79:00:20:6D"}))
    assert "00:1A:79:00:20:6D" in note and "42s left" in note and "bert" in note
    assert "Npo 1" in note


# =========================================================================== #
# the budget: the engine's deadline and the guard that must outlive it
# =========================================================================== #
def _chain(n_macs: int):
    macs = [type("M", (), {"id": i + 1}) for i in range(n_macs)]
    return [("src", "portal", macs)]


def test_start_budget_covers_every_candidate_and_the_zap_retry():
    two = StreamManager.start_budget(_chain(2))
    assert two == pytest.approx(2 * 2 * STREAM_START_TIMEOUT + 2.5)
    assert two > 25, "a two-MAC chain needs longer than the old fixed guard"
    # ...and the cap keeps a six-source playlist from hanging a player
    assert StreamManager.start_budget(_chain(6)) == STREAM_START_BUDGET
    assert StreamManager.start_budget([]) == 0.0
    assert StreamManager.start_budget([("local", "/x.ts")], kind="local") \
        == STREAM_START_TIMEOUT


def test_guard_follows_the_engine_instead_of_racing_it():
    handle = StreamHandle(id="h", kind="live", item_name="Npo 1", user_name="bert",
                          template_name="Copy", command="ffmpeg {url}",
                          start_budget=50.5)
    assert _guard_wait(handle) == pytest.approx(50.5 + START_BUDGET_SLACK)
    # no handle (preview, local file) and a zero budget keep the old floor
    assert _guard_wait(None) == FIRST_CHUNK_TIMEOUT
    assert _guard_wait(StreamHandle(id="h", kind="live", item_name="x",
                                    user_name=None, template_name="Copy",
                                    command="ffmpeg {url}")) == FIRST_CHUNK_TIMEOUT


def test_the_502_names_what_was_tried():
    """The reported log line said only "produced no data within 25s". The
    handle now carries the attempts, so the same failure reads
    "6F: silent 12s; 39:11: silent 12s"."""
    handle = StreamHandle(id="h", kind="live", item_name="Npo 1", user_name="bert",
                          template_name="Copy", command="ffmpeg {url}")
    handle.note_attempt("nexus/00:1A:79:00:20:6F: silent 12s")
    handle.note_attempt("nexus/00:1A:79:00:39:11: silent 12s")
    handle.fail_note = "start budget of 75s spent after 2 attempt(s)"
    assert "6F" in handle.trace and "39:11" in handle.trace
    assert "start budget" in handle.fail_note
    # bounded: one line per candidate, oldest dropped
    for i in range(20):
        handle.note_attempt(f"m{i}")
    assert len(handle.attempts) == 6 and handle.trace.startswith("m14")


# =========================================================================== #
# the pump: budget spent, attempts recorded, ffmpeg's tail reported
# =========================================================================== #
async def _live_route(macs=("00:1A:79:00:00:01", "00:1A:79:00:00:02")):
    async with SessionLocal() as s:
        p = Portal(name="nexus", base_url="http://127.0.0.1:1/c/",
                   resolved_url="http://127.0.0.1:1/c/")
        s.add(p)
        await s.flush()
        rows = []
        for i, mac in enumerate(macs):
            m = MacAddress(portal_id=p.id, mac=mac, status="online", order=i)
            s.add(m)
            await s.flush()
            rows.append(m.id)
        src = LiveSource(portal_id=p.id, portal_channel_id="1359",
                         original_name="Npo 1", cmd="ffmpeg http://cdn/x.ts",
                         enabled=True, link_flags="use_http_tmp_link")
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Npo 1", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id, rows


async def _never_data(self, command, url, *, title, pace,
                      first_byte_timeout=None):
    """A spawn that never produces a byte: stalled, with a stderr tail."""
    return None, b"", {"rc": None, "tail": "[vaapi @ 0x1] Failed to initialise",
                       "stalled": True}


def _fake_portal(monkeypatch):
    """A portal session that answers every create_link (so the pump reaches
    ffmpeg), for chains whose failure must come from the media side."""
    client = _FakeClient(["http://cdn/x.ts?play_token=t"])
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY_DELAY", 0.01)
    return client


async def test_a_silent_open_records_every_mac_it_tried(monkeypatch):
    """The user's chain: several MACs, each silent. The 502 has to name them."""
    pl, _macs = await _live_route()
    _fake_portal(monkeypatch)
    monkeypatch.setattr(StreamManager, "_open_with_identity", _never_data)
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_TIMEOUT", 0.1)
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY", False)
    handle, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []
    assert len(handle.attempts) == 2, handle.trace
    assert all("silent" in a for a in handle.attempts)
    assert "00:00:01" in handle.trace and "00:00:02" in handle.trace


async def test_the_pump_stops_at_its_budget_and_says_so(monkeypatch):
    pl, _macs = await _live_route()
    _fake_portal(monkeypatch)
    monkeypatch.setattr(StreamManager, "_open_with_identity", _never_data)
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_BUDGET", 0.05)
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_TIMEOUT", 0.01)
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY_DELAY", 0.3)
    handle, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []
    assert handle.start_budget == pytest.approx(0.05)
    assert "start budget of 0.05s spent" in handle.fail_note
    assert handle.attempts, "the report names at least the first MAC"


async def test_a_stalled_attempt_logs_the_stderr_tail(monkeypatch):
    """`no data within 12s` alone is not a diagnosis: the tail is where ffmpeg
    says *why* (VAAPI init, a 4xx, a slot check)."""
    from sqlalchemy import select

    from app.models import Log
    from app.services.db_logging import flush_logs

    pl, _macs = await _live_route()
    _fake_portal(monkeypatch)
    monkeypatch.setattr(StreamManager, "_open_with_identity", _never_data)
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_BUDGET", 0.3)
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_TIMEOUT", 0.1)
    handle, gen = await MANAGER.open("live", pl, "bert")
    [c async for c in gen]
    await flush_logs()
    async with SessionLocal() as s:
        rows = (await s.execute(select(Log)
                                .where(Log.message.contains("last words")))).scalars().all()
    assert rows, "the ffmpeg stderr tail must appear in the stream log"
    assert "Failed to initialise" in rows[-1].message


# =========================================================================== #
# the panel's answer: the probe
# =========================================================================== #
class _FakeClient:
    """A portal session whose create_link answers follow a script (see
    tests/test_zap_retry.py - the same pattern, reused here for the pump)."""

    def __init__(self, script):
        self._script = list(script)
        self.portal_url = "http://127.0.0.1:1/c/"

    async def ensure_auth(self):
        return None

    def invalidate(self):
        pass

    async def close(self):
        return None

    async def create_link(self, cmd, kind="live", **kw):
        return self._script[-1]


class _FakePool:
    def __init__(self, client):
        self._client = client

    async def get(self, session):
        return self._client


async def _mock_route(*, macs=(GOOD,)):
    async with SessionLocal() as s:
        p = Portal(name="mockportal", base_url="http://test/mock/c/",
                   resolved_url=PORTAL, enabled=True)
        s.add(p)
        await s.flush()
        ids = []
        for i, mac in enumerate(macs):
            m = MacAddress(portal_id=p.id, mac=mac, order=i, status="online",
                           online=True)
            s.add(m)
            await s.flush()
            ids.append(m.id)
        src = LiveSource(portal_id=p.id, portal_channel_id="1002",
                         original_name="NPO 1", cmd="ffmpeg http://mock/ts/1002.ts",
                         enabled=True)
        s.add(src)
        await s.commit()
        return p.id, ids


@pytest.fixture(autouse=True)
def _fresh_mock_state():
    saved = dict(_STATE)
    _STATE.update({"create_links": 0, "create_link_error": None, "usage": {},
                   "max_per_mac": 1, "offline": False, "slow": False})
    yield
    _STATE.clear()
    _STATE.update(saved)


async def test_probe_reports_available_when_the_link_streams(monkeypatch):
    w = Wired(monkeypatch)
    await _mock_route()
    monkeypatch.setattr(mac_probe, "_first_bytes", _fake_first_bytes(4096))
    pid, (mid,) = await _mock_route()
    rep = await mac_probe.probe_mac(pid, mid)
    assert rep["available"] is True and rep["reason"] == "free"
    assert "4096 bytes" in rep["detail"]
    state = await w.state()
    assert state["counters"]["create_links"] == 1, "one ask, then the read"


async def test_probe_reports_in_use_when_the_panel_refuses_the_mac(monkeypatch):
    """`limit` / `account is in use` is the panel saying "this MAC already has a
    stream open" - the answer local state cannot give."""
    Wired(monkeypatch)
    pid, (mid,) = await _mock_route()
    _STATE["create_link_error"] = "account is in use"
    monkeypatch.setattr(mac_probe, "_first_bytes", _fake_first_bytes(0))
    rep = await mac_probe.probe_mac(pid, mid)
    assert rep["available"] is False and rep["reason"] == "in-use"
    assert "already has a stream open" in rep["detail"]


async def test_probe_reports_an_unusable_mac_separately(monkeypatch):
    """A banned MAC is not "in use": the fix is in Portals, not patience."""
    Wired(monkeypatch)
    pid, (mid,) = await _mock_route()
    _STATE["create_link_error"] = "access_denied"
    monkeypatch.setattr(mac_probe, "_first_bytes", _fake_first_bytes(0))
    rep = await mac_probe.probe_mac(pid, mid)
    assert rep["reason"] == "unusable"
    assert "not enrolled" in rep["detail"]


async def test_probe_reports_no_data_when_the_link_stays_silent(monkeypatch):
    Wired(monkeypatch)
    pid, (mid,) = await _mock_route()
    monkeypatch.setattr(mac_probe, "_first_bytes", _fake_first_bytes(0))
    rep = await mac_probe.probe_mac(pid, mid)
    assert rep["reason"] == "no-data"
    assert "no data" in rep["detail"] or "no bytes" in rep["detail"]


async def test_probe_answers_from_local_state_without_touching_the_panel(monkeypatch):
    """If this proxy is streaming through the MAC, that IS the answer - and the
    panel must not be contacted for it."""
    w = Wired(monkeypatch)
    pid, (mid,) = await _mock_route()
    MANAGER.lease_mac(mid, seconds=120, holder="bert", item="Npo 1")
    _STATE.update({"create_links": 0})
    rep = await mac_probe.probe_mac(pid, mid)
    assert rep["reason"] == "busy-ours" and rep["available"] is False
    assert rep["holder"] == "bert"
    state = await w.state()
    assert state["counters"]["create_links"] == 0


def _fake_first_bytes(n: int):
    async def fake(url, *, timeout=10.0, uas=None):
        return {"bytes": n, "ua": stream_identity.PLAYER_UA, "status": 200,
                "detail": "ok" if n else "no bytes within 10s", "elapsed_s": 0.2}
    return fake


# ------------------------------------------------------------------ reading
class _Resp:
    def __init__(self, status, chunks):
        self.status_code = status
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _Client:
    """A media client that answers a script of (status, chunks) per attempt.

    The script list is shared between clients (one client per ladder rung), so
    the second rung pops the *next* entry instead of replaying the first."""

    def __init__(self, script):
        self._script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        return _Resp(*self._script.pop(0))


async def test_first_bytes_walks_the_media_ua_ladder(monkeypatch):
    """A 456 to the player identity is about the identity, not the MAC: the
    browser rung must be tried before the probe reports "no data"."""
    scripts = [(456, []), (200, [b"x" * 512])]
    monkeypatch.setattr(mac_probe, "_media_client",
                        lambda **kw: _Client(scripts))
    stream_identity.reset()
    got = await mac_probe._first_bytes("http://cdn.example/live.php?t=1")
    assert got["bytes"] == 512 and got["status"] == 200
    assert got["ua"] == stream_identity.STB_UA


async def test_first_bytes_reports_a_timeout_without_lying(monkeypatch):
    monkeypatch.setattr(mac_probe, "_media_client",
                        lambda **kw: _Client([(200, [])]))
    stream_identity.reset()
    got = await mac_probe._first_bytes("http://cdn2.example/live.php?t=1")
    assert got["bytes"] == 0 and "no bytes" in got["detail"]


# =========================================================================== #
# end to end: the zap that skipped its own MAC
# =========================================================================== #
async def _redirect_route(n_macs: int = 2, *, name: str = "Ch2"):
    """A live playlist item on a portal whose channels need a fresh link
    (`use_http_tmp_link`), i.e. the redirect path always asks the panel."""
    async with SessionLocal() as s:
        p = Portal(name="nexus", base_url="http://test/mock/c/",
                   resolved_url=PORTAL, enabled=True, direct_links=True)
        s.add(p)
        await s.flush()
        ids = []
        # the mock portal only enrols these two "valid" MACs - which is also
        # the shape of the real report: one portal, a couple of MACs on it
        for i, mac in enumerate(["00:1A:79:AA:AA:01", "00:1A:79:AA:AA:02"][:n_macs]):
            m = MacAddress(portal_id=p.id, mac=mac, order=i, status="online",
                           online=True)
            s.add(m)
            await s.flush()
            ids.append(m.id)
        src = LiveSource(portal_id=p.id, portal_channel_id="1002",
                         original_name=name, cmd="ffmpeg http://mock/ts/1002.ts",
                         enabled=True, link_flags="use_http_tmp_link")
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name=name, enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id, ids


async def test_a_zap_reuses_the_mac_it_left_instead_of_a_worse_one(monkeypatch):
    """Reported chain, verbatim: Npo 1 was playing via a 302 (mac 6D leased),
    the box zapped to Ch2, and the log said "mac 00:1A:79:00:20:6D busy -> skip"
    before falling to mac 6F, which produced no data.

    This is the *opt-out* mode (`prefer_free_mac` off): the MAC that just played
    is taken back instead of the free one, which is right when the portal's
    other MACs are the unreliable ones. The default is the opposite walk - see
    tests/test_zap_no_veto.py::test_a_redirect_zap_prefers_the_untouched_mac.
    """
    Wired(monkeypatch)
    monkeypatch.setattr("app.config.PREFER_FREE_MAC", False)
    pl, (first, second) = await _redirect_route()
    MANAGER.lease_mac(first, seconds=180, holder="bert", item="Npo 1")

    url, name = await MANAGER.resolve("live", pl, requester="bert")
    assert url and name == "Ch2"
    assert MANAGER.lease_meta[first]["item"] == "Ch2", "the zap took its own MAC"
    assert MANAGER.lease_meta[first]["holder"] == "bert"
    assert second not in MANAGER.redirect_leases, "no reason to touch the worse MAC"


async def test_another_users_mac_is_still_skipped(monkeypatch):
    Wired(monkeypatch)
    pl, (first, second) = await _redirect_route()
    MANAGER.lease_mac(first, seconds=180, holder="bert", item="Npo 1")

    url, _name = await MANAGER.resolve("live", pl, requester="anna")
    assert url
    assert first not in MANAGER.redirect_leases or \
        MANAGER.lease_meta[first]["holder"] == "bert", "not taken over"
    assert MANAGER.lease_meta[second]["holder"] == "anna"


async def test_the_ffmpeg_path_takes_over_its_own_lease_too(monkeypatch):
    """The same rule on the transcode path - the pump's busy check is where the
    reported skip happened. Opt-out mode, as above."""
    Wired(monkeypatch)
    monkeypatch.setattr("app.config.PREFER_FREE_MAC", False)
    pl, (first, second) = await _redirect_route()
    MANAGER.lease_mac(first, seconds=180, holder="bert", item="Npo 1")
    monkeypatch.setattr(StreamManager, "_open_with_identity", _never_data)
    monkeypatch.setattr("app.services.stream_manager.STREAM_START_TIMEOUT", 0.05)
    monkeypatch.setattr("app.services.stream_manager.ZAP_RETRY", False)
    handle, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []
    assert handle.took_over_lease is True
    assert "AA:AA:01" in handle.attempts[0], \
        f"the owned MAC must be tried first, not skipped: {handle.trace}"


# =========================================================================== #
# the API: what the Portals table shows and what the Probe button calls
# =========================================================================== #
async def test_portal_rows_carry_runtime_occupancy(monkeypatch):
    from app.main import app

    Wired(monkeypatch)
    pid, (mid,) = await _mock_route(macs=("00:1A:79:AA:AA:01",))
    MANAGER.lease_mac(mid, seconds=120, holder="bert", item="Npo 1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        rows = (await c.get("/api/portals")).json()["items"]
    row = next(r for r in rows if r["id"] == pid)
    runtime = row["macs"][0]["runtime"]
    assert runtime["busy"] is True and runtime["reason"] == "lease"
    assert runtime["holder"] == "bert" and runtime["remaining_s"] > 100


async def test_probe_endpoint_reports_the_panel_verdict(monkeypatch):
    from app.main import app

    Wired(monkeypatch)
    pid, (mid,) = await _mock_route(macs=("00:1A:79:AA:AA:01",))
    monkeypatch.setattr(mac_probe, "_first_bytes", _fake_first_bytes(2048))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        rep = (await c.post(f"/api/portals/{pid}/macs/{mid}/probe", json={})).json()
    assert rep["available"] is True
    assert rep["reason"] == "free" and rep["mac"] == GOOD
