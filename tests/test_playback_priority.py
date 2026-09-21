"""
A play outranks the background work - the gate that keeps the panel's budget
available for the thing the user is waiting for.

Everything SPM does with a panel shares one budget: the account's connection
slots and the panel's rate limiter. A catalogue sync walking thousands of pages
retries happily; a play that gets `limit` or a 429 shows the user an error. So
background callers ask the gate between requests, and it sleeps a little while a
stream is live on *that* portal.

Two things the gate deliberately does not do: cancel or starve the job (the sync
still finishes, a few seconds later), and count a *parked* pipe as a viewer.
Nobody is watching a parked stream - the whole point of parking is that the zap
back is cheap - so it must not stop background work.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services import portal_pace
from app.services.fetch_jobs import _paged_upsert
from app.services.stream_manager import MANAGER, StreamHandle


def _handle(portal_id: int, *, parked: bool = False, dead: bool = False) -> StreamHandle:
    return StreamHandle(id=f"s{portal_id}", kind="live", item_name="Ch1",
                        user_name="box", template_name="t", command="ffmpeg",
                        portal_id=portal_id, parked=parked, dead=dead)


@pytest.fixture(autouse=True)
def _clean():
    MANAGER.streams.clear()
    portal_pace.reset()
    yield
    MANAGER.streams.clear()
    portal_pace.reset()


async def test_a_background_call_yields_while_a_stream_is_live(monkeypatch):
    monkeypatch.setattr(portal_pace, "PACE_S", 0.05)
    MANAGER.streams["a"] = _handle(1)
    started = time.monotonic()
    paced = await portal_pace.pace_for_playback(1)
    took = time.monotonic() - started
    assert paced is True
    assert took >= 0.04, "the gate must actually wait"
    assert portal_pace.stats()["paced"] == {1: 1}


async def test_a_parked_pipe_does_not_hold_the_portal_back(monkeypatch):
    monkeypatch.setattr(portal_pace, "PACE_S", 5.0)      # must not be spent
    MANAGER.streams["a"] = _handle(1, parked=True)
    started = time.monotonic()
    assert await portal_pace.pace_for_playback(1) is False
    assert time.monotonic() - started < 0.5


async def test_a_dead_handle_does_not_either(monkeypatch):
    monkeypatch.setattr(portal_pace, "PACE_S", 5.0)
    MANAGER.streams["a"] = _handle(1, dead=True)
    assert await portal_pace.pace_for_playback(1) is False


async def test_another_portal_keeps_full_speed(monkeypatch):
    monkeypatch.setattr(portal_pace, "PACE_S", 5.0)
    MANAGER.streams["a"] = _handle(1)
    assert await portal_pace.pace_for_playback(2) is False
    assert portal_pace.playback_portal_ids() == {1}


async def test_the_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(portal_pace, "PACE_ENABLED", False)
    monkeypatch.setattr(portal_pace, "PACE_S", 5.0)
    MANAGER.streams["a"] = _handle(1)
    assert await portal_pace.pace_for_playback(1) is False


class _Page:
    def __init__(self, items: list, total: int) -> None:
        self.items = items
        self.total = total


class _Job:
    def __init__(self, portal_id: int = 1) -> None:
        self.portal_id = portal_id
        self.detail = ""
        self.done_items = 0

        class _C:
            def is_set(self) -> bool:
                return False

        self._cancel = _C()


async def test_the_catalogue_sync_yields_between_page_batches(monkeypatch):
    """The heavier the job, the more it matters - pinned at the call site."""
    calls = []

    async def fake_pace(portal_id):
        calls.append(portal_id)
        return False

    monkeypatch.setattr("app.services.fetch_jobs.pace_for_playback", fake_pace)
    monkeypatch.setattr("app.services.runtime_settings.fetch_page_budget",
                        lambda: _const(4))
    pages = {1: _Page([{"id": i} for i in range(14)], 40),
             2: _Page([{"id": 100 + i} for i in range(14)], 40),
             3: _Page([{"id": 200 + i} for i in range(5)], 40)}

    async def fetch_page(pg):
        return pages[pg]

    async def upsert_many(items):
        return None

    job = _Job(portal_id=7)
    await _paged_upsert(job, fetch_page, upsert_many, "Live", "mock")
    assert calls == [7], "the sync must consult the gate once per page batch"


async def _const(value):
    return value
