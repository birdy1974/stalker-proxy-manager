"""
How long a silent candidate is tolerated - the fence drops when there is a
fallback, and only then.

A candidate that is going to answer answers in well under a second (a live start
on the demo instance: ~550 ms end to end, panel RTT included). A candidate that
is silent at 2 s is nearly always a dead edge or a refused media path, and a
two-MAC chain that sat on the full 12 s window per MAC kept the screen black for
up to 24 s before the player saw anything - the symptom this whole stability pass
is about (`SPM_HEDGE_AFTER_S`).

The rule's halves are pinned here:

* the fence drops *only* while another MAC is genuinely free (nothing free to
  fall back to means patience is the only thing that helps, so the patient
  windows stay), and *only* after the first candidate has had its full window -
  the first candidate is the source the engine believes in, and hedge-cutting it
  to 2 s is what made a slow VOD start fail with six 2 s windows and zero bytes;
* the dropped window is what the log and the 502 name - a report that says
  "silent 12s" when we waited 2 s is a lie that costs somebody an afternoon;
* the fence is a *live* tool (zap speed) - VOD/episode/local first bytes are
  measured in seconds, not milliseconds, so the fence stays down for them
  (see tests/test_vod_hedge_patience.py for the reported regression).

A parallel race over two candidates was considered and rejected: it holds two
panel slots for one zap, and on the panels this was measured against a second
slot is exactly what answers `limit`.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services import stream_manager as sm
from app.services.stream_manager import MANAGER, StreamHandle
from tests.test_mac_availability import _FakeClient, _FakePool, _live_route


async def _silent_for_window(self, command, url, *, title, pace,
                             first_byte_timeout=None):
    """ffmpeg 'lives' for exactly the window it was given, then reports silence."""
    await asyncio.sleep(float(first_byte_timeout or 0.0))
    return None, b"", {"rc": None, "tail": "", "stalled": True}


def _wire(monkeypatch, *, window: float, hedge: float, min_candidates: int = 2):
    client = _FakeClient(["http://cdn/x.ts?play_token=t"])
    monkeypatch.setattr("app.services.stream_manager.POOL", _FakePool(client))
    monkeypatch.setattr("app.services.stream_manager.StreamManager._open_with_identity",
                        _silent_for_window)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT", window)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT_REST", window)
    monkeypatch.setattr(sm, "HEDGE_AFTER_S", hedge)
    monkeypatch.setattr(sm, "HEDGE_MIN_CANDIDATES", min_candidates)
    monkeypatch.setattr(sm, "ZAP_RETRY", False)
    monkeypatch.setattr(sm, "ZAP_RETRY_DELAY", 0.01)
    return client


@pytest.fixture(autouse=True)
def _clean():
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()
    yield
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.mac_limits.clear()


async def test_a_silent_candidate_is_dropped_early_when_a_mac_is_free(monkeypatch):
    pl, _macs = await _live_route()
    _wire(monkeypatch, window=5.0, hedge=0.15, min_candidates=1)
    started = time.monotonic()
    handle, gen = await MANAGER.open("live", pl, "bert")
    chunks = [c async for c in gen]
    took = time.monotonic() - started
    assert chunks == []
    assert took < 5.5, f"two silent candidates took {took:.2f}s despite a free MAC"
    assert handle.attempts, handle.trace
    # the FIRST candidate is never fence-cut (it is the one the engine trusts);
    # every candidate after it is
    assert "silent 5s" in handle.attempts[0], handle.attempts
    assert all("silent 0.15s" in a for a in handle.attempts[1:]), handle.attempts


async def test_a_single_free_mac_keeps_the_patient_window(monkeypatch):
    """Nothing to fall back to: patience is the only thing that can help."""
    pl, macs = await _live_route()
    _wire(monkeypatch, window=0.5, hedge=0.05, min_candidates=2)
    other = StreamHandle(id="other", kind="live", item_name="Other",
                         user_name="anna", template_name="t", command="ffmpeg")
    MANAGER.streams[other.id] = other
    MANAGER.lock_mac(macs[1], other.id)      # only one candidate stays free
    started = time.monotonic()
    handle, gen = await MANAGER.open("live", pl, "bert")
    # Drain it: an async generator only starts on the first __anext__().
    assert [c async for c in gen] == []
    took = time.monotonic() - started
    assert took >= 0.45, f"the fence dropped with no fallback ({took:.2f}s)"
    # the first candidate is the one that waited; the second was our own lock
    assert "silent 0.5s" in handle.attempts[0], handle.attempts


async def test_hedging_can_be_switched_off(monkeypatch):
    pl, _macs = await _live_route()
    _wire(monkeypatch, window=0.4, hedge=0.0)
    started = time.monotonic()
    handle, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []
    took = time.monotonic() - started
    assert took >= 0.35, f"hedging was off but the window was cut ({took:.2f}s)"
    assert all("silent 0.4s" in a for a in handle.attempts), handle.attempts


async def test_the_first_candidate_is_never_fence_cut(monkeypatch):
    """The engine trusts its first candidate: hedge applies from the second on.

    Cutting the FIRST candidate to 2 s was half of the reported VOD regression:
    the movie the engine most believed in was given 2 s to open, so every other
    candidate never got a real turn either. The first window stays patient even
    with plenty of free MACs around.
    """
    pl, _macs = await _live_route()
    _wire(monkeypatch, window=2.0, hedge=0.05, min_candidates=1)
    started = time.monotonic()
    handle, gen = await MANAGER.open("live", pl, "bert")
    assert [c async for c in gen] == []
    took = time.monotonic() - started
    assert 2.0 <= took < 3.0, f"first candidate got {took:.2f}s, not its full window"
    assert "silent 2s" in handle.attempts[0], handle.attempts
