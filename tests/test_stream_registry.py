"""
Connection-slot / MAC bookkeeping.

Reported symptom:

    WARNING [spm] [output] user user1 exceeded max_connections

...for a user who was not playing anything. `user_counts` was a separate
counter incremented in `_register` and decremented in `_deregister`; every
missed decrement (a generator parked at `yield` that never got finalised, a
watchdog task garbage-collected because nothing held a reference) permanently
consumed one of the user's slots, so the count only ever grew.
"""

from __future__ import annotations

import asyncio

from app.services import stream_manager as sm
from app.services.stream_manager import MANAGER, StreamHandle


class _Proc:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, returncode=None) -> None:
        self.returncode = returncode

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def _handle(id_: str, user: str = "user1") -> StreamHandle:
    return StreamHandle(id=id_, kind="live", item_name=f"item {id_}", user_name=user,
                        template_name="Copy", command="ffmpeg {url}")


def _reset() -> None:
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER._proc_gone_since.clear()


async def test_user_count_is_derived_from_the_registry():
    _reset()
    a, b, c = _handle("a"), _handle("b"), _handle("c", user="user2")
    for h in (a, b, c):
        MANAGER.streams[h.id] = h

    assert MANAGER.user_stream_count("user1") == 2
    assert MANAGER.user_stream_count("user2") == 1
    assert MANAGER.user_stream_count("nobody") == 0

    # the count follows the registry, it cannot drift away from it
    MANAGER.streams.pop("a")
    assert MANAGER.user_stream_count("user1") == 1


async def test_deregister_frees_the_slot_exactly_once():
    _reset()
    h = _handle("solo")
    MANAGER.streams[h.id] = h
    MANAGER.mac_locks[1] = h.id
    assert MANAGER.can_open_for("user1", 1) is False

    await MANAGER._deregister(h)
    assert MANAGER.user_stream_count("user1") == 0
    assert MANAGER.mac_locks == {}
    assert MANAGER.can_open_for("user1", 1) is True

    # calling it again must not drive the count negative / below reality
    await MANAGER._deregister(h)
    assert MANAGER.user_stream_count("user1") == 0
    other = _handle("other")
    MANAGER.streams[other.id] = other
    assert MANAGER.user_stream_count("user1") == 1
    _reset()


async def test_max_connections_is_not_consumed_by_a_lost_teardown(monkeypatch):
    """
    The reported failure: a handle left in the registry (teardown lost) must not
    keep the user locked out forever.
    """
    _reset()
    monkeypatch.setattr(sm, "REAP_GRACE", 0.05)

    stuck = _handle("stuck")
    stuck.proc = _Proc(returncode=1)          # ffmpeg is long gone
    MANAGER.streams[stuck.id] = stuck
    MANAGER.mac_locks[7] = stuck.id
    assert MANAGER.can_open_for("user1", 1) is False, "precondition: slot is occupied"

    reaper = asyncio.create_task(MANAGER.reap_dead(interval=0.02))
    try:
        for _ in range(200):
            if MANAGER.can_open_for("user1", 1):
                break
            await asyncio.sleep(0.02)
    finally:
        reaper.cancel()

    assert MANAGER.user_stream_count("user1") == 0, "slot was never released"
    assert MANAGER.mac_locks == {}, "MAC stayed locked after the reaper ran"
    assert stuck.id not in MANAGER.streams
    _reset()


async def test_reaper_leaves_a_live_stream_alone(monkeypatch):
    """A running ffmpeg process must never be reaped, however long it plays."""
    _reset()
    monkeypatch.setattr(sm, "REAP_GRACE", 0.05)

    live = _handle("live")
    live.proc = _Proc(returncode=None)        # still running
    MANAGER.streams[live.id] = live

    reaper = asyncio.create_task(MANAGER.reap_dead(interval=0.02))
    try:
        await asyncio.sleep(0.3)
    finally:
        reaper.cancel()

    assert live.id in MANAGER.streams, "the reaper killed a healthy stream"
    assert MANAGER.user_stream_count("user1") == 1
    _reset()


async def test_watch_keeps_a_strong_reference_to_the_watchdog():
    """
    `create_task` alone leaves only a weak reference, so the watchdog - the thing
    that releases the MAC and the slot when a player vanishes - could be
    garbage-collected mid-flight.
    """
    _reset()
    import gc

    handle = _handle("watched")

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    task = MANAGER.watch(_Req(), handle)
    gc.collect()

    assert task in MANAGER._watchers, "watchdog task is not referenced anywhere"
    assert not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task not in MANAGER._watchers, "finished watchdog was not dropped"
    _reset()
