"""
The two windows a zap is measured against: how long we wait for a busy panel
slot, and how long a silent pipe is tolerated before it counts as over.

Both are numbers taken from a real panel's behaviour rather than taste:

* a slot stays counted for ~6.5 s after the previous connection dies, so a
  shorter wait is spent for nothing and the user gets a channel error on a zap
  that would have played;
* once a live stream has been flowing, silence means "the source dropped", and
  the response can be continued by re-resolving - so tolerating 25 s of it just
  freezes the picture for 25 s.

Pinned here because both are easy to "tidy" back to a smaller/generic number
without noticing what they were tuned against.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.services import stream_manager as _sm
from app.services.stream_manager import MANAGER, StreamHandle


def _handle(kind: str, name: str = "Ch1") -> StreamHandle:
    return StreamHandle(id=kind, kind=kind, item_name=name, user_name="box",
                        template_name="t", command="ffmpeg")


def test_the_busy_wait_covers_the_panel_s_slot_handoff():
    """The wait must outlast the panel's own handoff, or it is theatre."""
    if os.environ.get("SPM_BUSY_WAIT_S"):
        pytest.skip("the operator set SPM_BUSY_WAIT_S explicitly")
    assert _sm.BUSY_WAIT_S >= 6.5, (
        "a real panel kept the slot counted for ~6.5 s; waiting less means the "
        "wait is spent AND the player still gets a 503")


def test_a_live_stream_gets_the_short_stall_window_and_vod_does_not(monkeypatch):
    monkeypatch.setattr(_sm, "MIDSTREAM_RESTARTS", 3)
    live = _handle("live")
    vod = _handle("vod")
    assert MANAGER._stall_window(live) == _sm.STREAM_STALL_TIMEOUT_LIVE
    assert MANAGER._stall_window(vod) == _sm.STREAM_STALL_TIMEOUT
    assert _sm.STREAM_STALL_TIMEOUT_LIVE < _sm.STREAM_STALL_TIMEOUT


def test_the_stall_window_is_generous_when_a_restart_is_not_possible(monkeypatch):
    """No restart capability (or a kind that cannot be restarted) -> old window.

    The short window is only safe because the response continues by
    re-resolving. With that off, a silent live stream must keep the long
    tolerance - there is nothing to replace it with.
    """
    monkeypatch.setattr(_sm, "MIDSTREAM_RESTARTS", 0)
    live = _handle("live")
    assert MANAGER._stall_window(live) == _sm.STREAM_STALL_TIMEOUT
    monkeypatch.setattr(_sm, "MIDSTREAM_RESTARTS", 3)
    monkeypatch.setattr(_sm, "MIDSTREAM_RESTART_KINDS", {"vod"})
    assert MANAGER._stall_window(live) == _sm.STREAM_STALL_TIMEOUT


class _HangingProc:
    """A process whose stdout never produces a byte (a source that went away)."""

    def __init__(self) -> None:
        self.returncode = None
        self.stdout = self

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(5)
        return b""


async def test_a_silent_live_pipe_gives_up_inside_its_window(monkeypatch):
    """The read loop must end on the window, not on the 5 s read."""
    monkeypatch.setattr(_sm, "STREAM_STALL_TIMEOUT_LIVE", 0.15)
    h = _handle("live")
    started = time.monotonic()
    chunks = [c async for c in MANAGER._read_proc(h, _HangingProc())]
    took = time.monotonic() - started
    assert chunks == []
    assert took < 1.5, f"stall detection waited {took:.2f}s for a 0.15s window"
