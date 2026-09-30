"""Start-up latency of the pass-through proxy (and the zap around it).

The pass-through template (`@passthrough`) is an async byte pipe, so nothing in
it is supposed to cost seconds. These tests pin the places where it used to:

* the fallback engine called ``.strip()`` on a *list* tail, so every
  pass-through failure was "pump crashed" - no MAC/source fallback at all;
* a slow HTTP 456 (the panel's connection-slot check) was retried with the other
  user-agent, doubling a ~6.5 s wait;
* the response headers were waited for without any window;
* httpx re-chunked the body to 64 KB, holding the first bytes back;
* the busy-slot ladder (0.5 + 1 + 2 s) ran even when another MAC was free;
* a MAC whose pipe had just left ranked first again, while the panel still
  counted it;
* a *starting* play did not make background jobs yield;
* the upstream socket of a killed pass-through pipe was closed in a task nobody
  awaited, and the max-connections retry always slept its full delay.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.portal.client import PortalError
from app.routers import output
from app.services import portal_pace, stream_identity
from app.services import stream_manager as sm
from app.services.ffmpeg_templates import PASSTHROUGH_COMMAND
from app.services.stream_manager import MANAGER, PassthroughStream, StreamHandle
from tests.test_fallback_reconnect import _RecordingClient, _RecordingPool
from tests.test_mac_availability import _live_route

MAC_A = "00:1A:79:00:00:01"
MAC_B = "00:1A:79:00:00:02"
URL = "http://cdn.example.com/live/ch1.ts?play_token=t"


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        MANAGER.streams.clear()
        MANAGER.mac_locks.clear()
        MANAGER.mac_limits.clear()
        MANAGER._released.clear()
        MANAGER.starting.clear()
        MANAGER.route_health.failures.clear()
        MANAGER.route_health.success.clear()
        stream_identity.reset()
    wipe()
    yield
    wipe()


class _Resp:
    def __init__(self, status=200, chunks=(b"TS",)):
        self.status_code = status
        self._chunks = list(chunks)

    async def aiter_bytes(self, *args, **kwargs):
        _Resp.last_iter_args = (args, kwargs)
        for c in self._chunks:
            yield c

    async def aclose(self):
        pass


def _patch_send(monkeypatch, handler):
    calls: list[dict] = []

    async def fake_send(self, req, *a, **kw):
        calls.append({"ua": req.headers.get("user-agent"),
                      "enc": req.headers.get("accept-encoding"), "at": time.monotonic()})
        return await handler(len(calls), req)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return calls


# --------------------------------------------------------------------------- #
#  the pass-through opener
# --------------------------------------------------------------------------- #
async def test_a_failed_passthrough_reports_a_string_tail(monkeypatch):
    """The engine does `(tail or "").strip()` - a list there crashed the pump."""
    async def handler(n, req):
        return _Resp(500)
    _patch_send(monkeypatch, handler)

    proc, first, fail = await MANAGER._open_passthrough(URL, title="Ch", first_byte_timeout=1)

    assert proc is None and first == b""
    assert isinstance(fail["tail"], str) and "500" in fail["tail"]
    (fail["tail"] or "").strip()                       # must not raise


async def test_a_fast_refusal_still_tries_the_other_user_agent(monkeypatch):
    async def handler(n, req):
        return _Resp(456) if n == 1 else _Resp(200, [b"DATA"])
    calls = _patch_send(monkeypatch, handler)

    proc, first, fail = await MANAGER._open_passthrough(URL, title="Ch", first_byte_timeout=1)

    assert fail is None and first == b"DATA"
    assert len(calls) == 2 and calls[0]["ua"] != calls[1]["ua"]
    await proc._close()


async def test_a_slow_refusal_is_the_slot_not_the_user_agent(monkeypatch):
    """A 456 that took seconds is the panel's slot check: no second identical wait."""
    monkeypatch.setattr(stream_identity, "FAST_REFUSAL_S", 0.05)

    async def handler(n, req):
        await asyncio.sleep(0.15)                      # "slow"
        return _Resp(456)
    calls = _patch_send(monkeypatch, handler)

    proc, _first, fail = await MANAGER._open_passthrough(URL, title="Ch", first_byte_timeout=5)

    assert proc is None and fail["rc"] == 456
    assert len(calls) == 1, "the slow refusal must not be retried with the other UA"


async def test_silent_response_headers_are_bounded_by_the_start_window(monkeypatch):
    async def handler(n, req):
        await asyncio.sleep(30)                        # origin accepts, never answers
    _patch_send(monkeypatch, handler)

    started = time.monotonic()
    proc, _first, fail = await MANAGER._open_passthrough(URL, title="Ch", first_byte_timeout=0.2)

    assert proc is None and fail["stalled"] is True
    assert time.monotonic() - started < 2.0
    assert isinstance(fail["tail"], str)


async def test_the_request_does_not_ask_for_compression(monkeypatch):
    async def handler(n, req):
        return _Resp(200, [b"DATA"])
    calls = _patch_send(monkeypatch, handler)

    proc, _first, _fail = await MANAGER._open_passthrough(URL, title="Ch", first_byte_timeout=1)

    assert calls[0]["enc"] == "identity"
    await proc._close()


async def test_bytes_are_handed_on_as_they_arrive_not_in_64k_blocks():
    """`aiter_bytes(chunk_size=N)` holds data back until N bytes have piled up."""
    seen = {}

    class R:
        def aiter_bytes(self, *a, **kw):
            seen["args"], seen["kwargs"] = a, kw

            async def gen():
                yield b"x"
            return gen()

        async def aclose(self):
            pass

    stream = PassthroughStream(httpx.AsyncClient(), R())
    assert "chunk_size" not in seen["kwargs"] and not seen["args"]
    await stream._close()


async def test_a_real_socket_delivers_the_first_bytes_without_waiting_for_64k():
    """Against a real paced origin (~500 kbit/s): first bytes in well under 64 KB's time."""
    async def handle(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")
        try:
            while True:
                pkt = b"\x47" * 1316
                w.write(f"{len(pkt):x}\r\n".encode() + pkt + b"\r\n")
                await w.drain()
                await asyncio.sleep(1316 / 62_500)      # 500 kbit/s: 64 KB would take 1 s
        except Exception:  # noqa: BLE001
            pass

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        started = time.monotonic()
        proc, first, fail = await MANAGER._open_passthrough(
            f"http://127.0.0.1:{port}/live.ts", title="Ch", first_byte_timeout=5)
        took = time.monotonic() - started
        assert fail is None and proc is not None
        assert len(first) < 65536
        assert took < 0.5, f"first bytes took {took:.2f}s (the 64 KB re-chunker needs ~1 s)"
        await proc._close()
    finally:
        srv.close()


async def test_killing_a_passthrough_pipe_waits_for_its_upstream_socket():
    closed = asyncio.Event()

    class R:
        def aiter_bytes(self, *a, **kw):
            async def gen():
                yield b"x"
            return gen()

        async def aclose(self):
            await asyncio.sleep(0.05)
            closed.set()

    stream = PassthroughStream(httpx.AsyncClient(), R())
    h = StreamHandle(id="pt1", kind="live", item_name="Ch", user_name="u",
                     template_name="t", command=PASSTHROUGH_COMMAND, proc=stream)
    MANAGER.streams[h.id] = h

    assert await MANAGER.kill("pt1") is True

    assert closed.is_set(), "kill() returned before the upstream socket was closed"


# --------------------------------------------------------------------------- #
#  the fallback engine around it
# --------------------------------------------------------------------------- #
async def test_passthrough_failure_walks_the_next_mac_instead_of_crashing(monkeypatch):
    pl, _rows = await _live_route(macs=(MAC_A, MAC_B))
    events: list = []
    client = _RecordingClient(events)
    monkeypatch.setattr(sm, "POOL", _RecordingPool(client, events))

    async def tpl(self, item, *, kind=None, user_name=None):
        return "Pass-through", PASSTHROUGH_COMMAND
    monkeypatch.setattr(sm.StreamManager, "_template_for", tpl)

    async def handler(n, req):
        return _Resp(500)
    _patch_send(monkeypatch, handler)
    monkeypatch.setattr(sm, "ZAP_RETRY", False)
    monkeypatch.setattr(sm, "BUSY_BACKOFF", ())

    logged: list[str] = []
    real = sm.db_log

    async def spy(level, module, message):
        logged.append(message)
        await real(level, module, message)
    monkeypatch.setattr(sm, "db_log", spy)

    _h, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []

    assert [m for ev, m in events if ev == "create_link"] == [MAC_A, MAC_B]
    assert not any("pump crashed" in m for m in logged), logged


# --------------------------------------------------------------------------- #
#  the busy-slot ladder only runs when there is nothing else to try
# --------------------------------------------------------------------------- #
class _BusyClient:
    def __init__(self):
        self.calls = 0

    async def create_link(self, cmd, kind="live", **kw):
        self.calls += 1
        raise PortalError("limit", code="limit")


class _Plan:
    cmd = "x"

    def request_kwargs(self):
        return {}


async def test_the_ladder_is_skipped_when_another_mac_can_be_tried(monkeypatch):
    monkeypatch.setattr(sm, "BUSY_BACKOFF", (0.3, 0.3, 0.3))
    client = _BusyClient()
    started = time.monotonic()
    with pytest.raises(PortalError):
        await MANAGER._create_link_with_backoff(client, _Plan(), "live", "Ch", None,
                                                patient=False)
    assert client.calls == 1
    assert time.monotonic() - started < 0.2


async def test_the_ladder_still_runs_for_the_only_candidate(monkeypatch):
    monkeypatch.setattr(sm, "BUSY_BACKOFF", (0.01, 0.01, 0.01))
    client = _BusyClient()
    with pytest.raises(PortalError):
        await MANAGER._create_link_with_backoff(client, _Plan(), "live", "Ch", None)
    assert client.calls == 4


async def test_has_alternative_counts_only_macs_nothing_holds():
    pl, (a, b) = await _live_route(macs=(MAC_A, MAC_B))
    chain, _name, _item = await MANAGER._live_chain(pl)
    _src, _portal, macs = chain[0]
    ma, mb = macs

    assert MANAGER._has_alternative(chain, 1, macs, ma, "box") is True
    assert MANAGER._has_alternative(chain, 1, macs, mb, "box") is False   # nothing after it
    MANAGER.lock_mac(mb.id, "other")
    assert MANAGER._has_alternative(chain, 1, macs, ma, "box") is False   # busy is not free
    MANAGER.mac_locks.clear()


# --------------------------------------------------------------------------- #
#  a MAC the panel still counts goes behind the ones nobody touched
# --------------------------------------------------------------------------- #
async def test_a_just_released_mac_ranks_behind_an_untouched_one():
    pl, (a, b) = await _live_route(macs=(MAC_A, MAC_B))
    chain, _n, _i = await MANAGER._live_chain(pl)
    src, _p, macs = chain[0]
    ma, mb = macs

    MANAGER.lock_mac(ma.id, "s1")
    MANAGER.unlock_mac(ma.id, "s1")                    # the pipe left A

    assert MANAGER.cooling_down(ma.id) is True
    order = MANAGER.order_by_free(("live", pl), src, [ma, mb], "box")
    assert [m.id for m in order] == [mb.id, ma.id]
    assert MANAGER.is_mac_busy(ma.id) is False, "ordering only - never a veto"


async def test_the_cooldown_ends_and_can_be_switched_off(monkeypatch):
    MANAGER.lock_mac(7, "s1")
    MANAGER.unlock_mac(7, "s1")
    assert MANAGER.cooling_down(7) is True
    MANAGER._released[7] -= sm.SLOT_COOLDOWN_S + 1
    assert MANAGER.cooling_down(7) is False

    monkeypatch.setattr(sm, "SLOT_COOLDOWN_S", 0.0)
    MANAGER.lock_mac(8, "s2")
    MANAGER.unlock_mac(8, "s2")
    assert MANAGER.cooling_down(8) is False


async def test_a_deregistered_stream_starts_the_cooldown_too():
    h = StreamHandle(id="d1", kind="live", item_name="Ch", user_name="u",
                     template_name="t", command="x")
    MANAGER.streams[h.id] = h
    MANAGER.lock_mac(9, h.id)
    await MANAGER._deregister(h)
    assert MANAGER.cooling_down(9) is True


# --------------------------------------------------------------------------- #
#  a starting play outranks background work
# --------------------------------------------------------------------------- #
async def test_a_starting_play_makes_background_jobs_yield(monkeypatch):
    monkeypatch.setattr(portal_pace, "PACE_S", 0.05)
    portal_pace.reset()
    assert await portal_pace.pace_for_playback(3) is False

    MANAGER.starting["s"] = 3                          # looking for its first byte
    started = time.monotonic()
    assert await portal_pace.pace_for_playback(3) is True
    assert time.monotonic() - started >= 0.04
    assert await portal_pace.pace_for_playback(4) is False, "another portal is unaffected"

    MANAGER.starting.clear()
    assert await portal_pace.pace_for_playback(3) is False


async def test_the_starting_marker_is_cleared_when_the_walk_ends(monkeypatch):
    pl, _rows = await _live_route(macs=(MAC_A,))
    events: list = []
    monkeypatch.setattr(sm, "POOL", _RecordingPool(_RecordingClient(events), events))

    async def no_data(self, command, url, *, title, pace, first_byte_timeout=None):
        assert MANAGER.starting, "the play must be marked as starting while it looks"
        return None, b"", {"rc": 8, "tail": "x", "stalled": False}
    monkeypatch.setattr(sm.StreamManager, "_open_with_identity", no_data)
    monkeypatch.setattr(sm, "ZAP_RETRY", False)
    monkeypatch.setattr(sm, "BUSY_BACKOFF", ())

    _h, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []
    assert MANAGER.starting == {}


# --------------------------------------------------------------------------- #
#  the max-connections retry does not sleep longer than it has to
# --------------------------------------------------------------------------- #
async def test_ensure_slot_returns_the_moment_the_slot_frees(monkeypatch):
    monkeypatch.setattr(output, "MAXCONN_RETRY_DELAY", 2.0)
    free = {"now": False}
    monkeypatch.setattr(MANAGER, "can_open_for", lambda *a, **k: free["now"])

    class U:
        name = "box"
        max_connections = 1

    async def release():
        await asyncio.sleep(0.1)
        free["now"] = True
    task = asyncio.create_task(release())

    started = time.monotonic()
    await output._ensure_slot(U())
    await task
    assert time.monotonic() - started < 0.5, "it slept the whole 2 s delay"


async def test_ensure_slot_still_refuses_an_honest_overload(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(output, "MAXCONN_RETRY_DELAY", 0.1)
    monkeypatch.setattr(MANAGER, "can_open_for", lambda *a, **k: False)

    class U:
        name = "box"
        max_connections = 1

    with pytest.raises(HTTPException) as err:
        await output._ensure_slot(U())
    assert err.value.status_code == 429
