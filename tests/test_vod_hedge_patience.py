"""
The reported VOD regression: a slow movie start was hedge-cut to 2 s and marked
dead before its first byte.

The shape that failed (`nexus`, `Pressure - 2026`):

    fallback step 1/1: portal 'nexus' mac 00:1A:79:00:20:6F
    no data within 2s from nexus/00:1A:79:00:20:6F -> fallback
    ... (every other MAC, same 2 s) ...
    all fallbacks exhausted - 6 attempt(s) without data
    produced no data within 85s -> 502

`create_link` for VOD answers 200 within ~80 ms for every MAC, so all four
stayed "free" across the walk and every candidate - the first one included -
was hedge-cut to 2 s by the blanket live rule. Nothing was busy (six windows,
six silent MACs), so `wait_for_mac` / `preempt_own` never entered the picture:
the only gate that ran was the hedge, and it ran for the wrong kind of item.

Two changes are pinned here:

* VOD/episode/local are excluded from the fence entirely - their first byte is
  measured in seconds (a storage the panel picked, a cold CDN edge), not the
  ~550 ms a live start needs, so patience is the only thing that helps. The
  first candidate of a *live* walk is protected the same way (see
  tests/test_hedge_patience.py).
* `SPM_HEDGE_AFTER_S=0` still switches the fence off for every kind, and the
  fence threshold can no longer be met by double-counting one free MAC across
  several chain steps.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.database import SessionLocal
from app.models import (MacAddress, Portal, VodPlaylist, VodPlaylistSource,
                        VodSource)
from app.services import stream_manager as sm
from app.services.stream_manager import MANAGER
from tests.test_mac_availability import _FakeClient, _FakePool

MAC_ADDRS = ("00:1A:79:00:12:AD", "00:1A:79:01:6D:BF",
             "00:1A:79:00:20:6F", "00:1A:79:00:39:11")


async def _silent_for_window(self, command, url, *, title, pace,
                             first_byte_timeout=None):
    """ffmpeg 'lives' for exactly the window it was given, then reports silence."""
    await asyncio.sleep(float(first_byte_timeout or 0.0))
    return None, b"", {"rc": None, "tail": "", "stalled": True}


async def _vod_route():
    """One VOD item whose portal has the report's four MACs on it."""
    async with SessionLocal() as s:
        p = Portal(name="nexus", base_url="http://127.0.0.1:1/c/",
                   resolved_url="http://127.0.0.1:1/c/")
        s.add(p)
        await s.flush()
        mac_ids = []
        for i, mac in enumerate(MAC_ADDRS):
            m = MacAddress(portal_id=p.id, mac=mac, status="online", order=i)
            s.add(m)
            await s.flush()
            mac_ids.append(m.id)
        src = VodSource(portal_id=p.id, portal_item_id="1756499",
                        original_name="Pressure - 2026",
                        cmd="ffmpeg http://cdn/pressure.mkv",
                        link_flags="use_http_tmp_link", enabled=True)
        s.add(src)
        await s.flush()
        pl = VodPlaylist(vod_source_id=src.id, custom_name="Pressure - 2026",
                         enabled=True)
        s.add(pl)
        await s.flush()
        s.add(VodPlaylistSource(vod_playlist_id=pl.id, vod_source_id=src.id,
                                priority=1))
        await s.commit()
        return pl.id, mac_ids


def _wire(monkeypatch, *, window: float, hedge: float):
    monkeypatch.setattr("app.services.stream_manager.POOL",
                        _FakePool(_FakeClient(["http://cdn/x.mkv?play_token=t"])))
    monkeypatch.setattr("app.services.stream_manager.StreamManager._open_with_identity",
                        _silent_for_window)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT", window)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT_REST", window)
    monkeypatch.setattr(sm, "HEDGE_AFTER_S", hedge)
    monkeypatch.setattr(sm, "ZAP_RETRY", False)
    monkeypatch.setattr(sm, "ZAP_RETRY_DELAY", 0.01)


@pytest.fixture(autouse=True)
def _clean():
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()
    yield
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()


async def test_vod_is_not_hedge_cut(monkeypatch):
    """The regression: a slow movie must keep its window, not die after 2 s.

    The source declared silent only because the fence cut it; ffmpeg was still
    trying to open the file. Without the fix every MAC is asked with a 2 s
    window, so the whole chain fails in ~12 s with zero bytes.
    """
    pl, _macs = await _vod_route()
    _wire(monkeypatch, window=2.0, hedge=0.05)
    started = time.monotonic()
    handle, gen = await MANAGER.open("vod", pl, "bert")
    assert [c async for c in gen] == []
    took = time.monotonic() - started
    # four MACs, each given its FULL window - the hedge stays down for VOD
    assert took >= 4 * 2.0 * 0.85, \
        f"a slow VOD was fence-cut again ({took:.2f}s): {handle.trace}"
    assert all("silent 2s" in a for a in handle.attempts[:4]), handle.attempts


class _Proc:
    """A process stand-in: the first byte arrived, then the pipe ends."""

    returncode = None
    pid = 99999
    spm_stderr_tail: list = []

    def kill(self):
        pass

    async def wait(self):
        return 0

    class _Out:
        async def read(self, _n):
            return b""

    stdout = _Out()


async def test_a_slow_vod_plays_once_it_produces_a_byte(monkeypatch):
    """The fence off, a VOD that needs most of its window still plays.

    With the regression, the hedge cut the first candidate to 0.05 s and the
    movie's first byte (which arrives just before the real window would have
    ended) was lost - so the handle carries a "silent" attempt and zero bytes.
    """
    pl, _macs = await _vod_route()

    async def slow_then_data(self, command, url, *, title, pace,
                             first_byte_timeout=None):
        # arrives just before the window would have declared it silent
        await asyncio.sleep(float(first_byte_timeout or 0.0) - 0.05)
        return _Proc(), b"data", None

    monkeypatch.setattr("app.services.stream_manager.POOL",
                        _FakePool(_FakeClient(["http://cdn/x.mkv?play_token=t"])))
    monkeypatch.setattr("app.services.stream_manager.StreamManager._open_with_identity",
                        slow_then_data)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT", 0.5)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT_REST", 0.5)
    monkeypatch.setattr(sm, "HEDGE_AFTER_S", 0.05)
    monkeypatch.setattr(sm, "ZAP_RETRY", False)
    handle, gen = await MANAGER.open("vod", pl, "bert")
    got = [c async for c in gen]
    assert got, "a just-in-time VOD produced no bytes"
    assert not handle.attempts, f"a slow VOD was fence-cut: {handle.trace}"
