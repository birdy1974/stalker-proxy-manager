"""
Bounded parallel pagination in the fetch jobs.

crispy-stalker advertises "parallel 4-page pagination" (DEFAULT_CONCURRENCY = 4)
but its batch loop awaits each page one at a time - the comment even says
"Fetch pages in this batch sequentially but with concurrent intent". The idea is
right though: page 1 reports the total, so the remaining pages are independent
and can go out concurrently.

Measured here with a 100ms stand-in per portal round trip over 24 pages:

    concurrency=1: 2.41s
    concurrency=4: 0.70s     (3.4x)
    concurrency=8: 0.40s     (6.0x)

with the same 336 items collected in every case.
"""

from __future__ import annotations

import asyncio
import time

import app.config as cfg
from app.portal.client import Page
from app.services.fetch_jobs import _paged_upsert

PAGES = 24
LATENCY = 0.05


class _Job:
    """Just enough of the Job shape for _paged_upsert."""

    def __init__(self) -> None:
        self._cancel = asyncio.Event()
        self.detail = ""
        self.done_items = 0


def _fetcher(state: dict, pages: int = PAGES, latency: float = LATENCY):
    async def fetch_page(p: int) -> Page:
        # Count in-flight BEFORE the sleep, otherwise the window we are trying
        # to observe has already closed by the time we increment.
        state["calls"] = state.get("calls", 0) + 1
        state["live"] = state.get("live", 0) + 1
        state["peak"] = max(state.get("peak", 0), state["live"])
        try:
            await asyncio.sleep(latency)
            n = 14 if p <= pages else 0
            return Page(items=[{"id": f"i{p}_{k}"} for k in range(n)], total=pages * 14)
        finally:
            state["live"] -= 1
    return fetch_page


async def _collect(pages: int, concurrency: int) -> tuple[list, dict, int]:
    """Run _paged_upsert at a given concurrency, returning (ids, state, total)."""
    state: dict = {}
    got: list = []

    async def upsert(items):
        got.extend(i["id"] for i in items)

    old = cfg.FETCH_PAGE_CONCURRENCY
    cfg.FETCH_PAGE_CONCURRENCY = concurrency
    try:
        job = _Job()
        n, total = await _paged_upsert(job, _fetcher(state, pages), upsert,
                                       "Action", "test", budget=40)
    finally:
        cfg.FETCH_PAGE_CONCURRENCY = old
    assert n == len(got)
    return got, state, total


async def test_parallel_fetch_collects_the_same_items_as_serial():
    serial, s_state, s_total = await _collect(PAGES, 1)
    parallel, p_state, p_total = await _collect(PAGES, 4)
    assert s_total == p_total == PAGES * 14
    assert sorted(serial) == sorted(parallel), "concurrency must not lose or duplicate items"
    assert len(set(parallel)) == len(parallel)
    assert s_state["calls"] == p_state["calls"] == PAGES


async def test_concurrency_is_actually_bounded():
    _, state, _ = await _collect(PAGES, 4)
    assert state["peak"] <= 4, f"in-flight pages peaked at {state['peak']}, limit is 4"
    assert state["peak"] >= 2, "expected real overlap, got none - the fetch is still serial"


async def test_parallel_is_faster_than_serial():
    t0 = time.perf_counter()
    await _collect(PAGES, 1)
    serial_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    await _collect(PAGES, 4)
    parallel_s = time.perf_counter() - t0
    assert parallel_s < serial_s * 0.6, (
        f"parallel {parallel_s:.2f}s should beat serial {serial_s:.2f}s by a clear margin")


async def test_short_page_stops_early():
    """A page returning fewer than 14 items means the list ended."""
    ids, state, _ = await _collect(5, 4)
    assert len(ids) == 5 * 14
    assert state["calls"] <= 8, f"kept fetching past the end: {state['calls']} calls"


async def test_a_failing_page_is_skipped_not_fatal():
    state: dict = {}
    got: list = []

    async def upsert(items):
        got.extend(i["id"] for i in items)

    async def flaky(p: int) -> Page:
        await asyncio.sleep(0.01)
        state["calls"] = state.get("calls", 0) + 1
        if p == 3:
            raise RuntimeError("portal hiccup")
        n = 14 if p <= 6 else 0
        return Page(items=[{"id": f"i{p}_{k}"} for k in range(n)], total=6 * 14)

    old = cfg.FETCH_PAGE_CONCURRENCY
    cfg.FETCH_PAGE_CONCURRENCY = 4
    try:
        job = _Job()
        n, _total = await _paged_upsert(job, flaky, upsert, "Action", "test", budget=40)
    finally:
        cfg.FETCH_PAGE_CONCURRENCY = old
    assert n == 5 * 14, "one bad page must not take the whole genre down"
    assert not any(i.startswith("i3_") for i in got)


async def test_cancellation_stops_the_walk():
    state: dict = {}

    async def upsert(items):
        pass

    job = _Job()

    async def slow(p: int) -> Page:
        await asyncio.sleep(0.02)
        state["calls"] = state.get("calls", 0) + 1
        if state["calls"] >= 2:
            job._cancel.set()
        return Page(items=[{"id": f"i{p}_{k}"} for k in range(14)], total=100 * 14)

    old = cfg.FETCH_PAGE_CONCURRENCY
    cfg.FETCH_PAGE_CONCURRENCY = 4
    try:
        n, _ = await _paged_upsert(job, slow, upsert, "Action", "test", budget=100)
    finally:
        cfg.FETCH_PAGE_CONCURRENCY = old
    assert state["calls"] < 20, f"cancellation ignored: {state['calls']} pages fetched"
