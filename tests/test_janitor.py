"""
Long-run hygiene: the structures that grow with history, not with config.

SPM is meant to run for months on a NAS. Most of its state is bounded by
configuration (the pool by the portal rows, the UA ladder by 512 origins, the
link cache by 256 entries, parked pipes by LINGER_S, probe verdicts by
SPM_LINK_PROBE_CACHE_MAX). These three are bounded by *history* instead - one
entry per sync ever run, per (route, source) pair ever played or failed, per log
line ever written - and nothing else ever removes the stale ones.
"""

from __future__ import annotations

import time

from app.services import fetch_jobs, janitor, redirect_guard
from app.services.fetch_jobs import Job, prune_jobs
from app.services.stream_manager import MANAGER


def _job(job_id: str, status: str, ended: float) -> Job:
    j = Job(id=job_id, kind="vod", portal_id=1, status=status, started=ended - 1,
            ended=ended)
    return j


def test_the_job_registry_keeps_the_newest_and_never_touches_running_ones():
    fetch_jobs.JOBS.clear()
    try:
        for i in range(8):
            fetch_jobs.JOBS[f"done{i}"] = _job(f"done{i}", "done", 100 + i)
        fetch_jobs.JOBS["running"] = _job("running", "running", 0)
        dropped = prune_jobs(keep=3)
        assert dropped == 5
        left = set(fetch_jobs.JOBS)
        assert "running" in left, "a running job must not be pruned"
        assert left == {"running", "done5", "done6", "done7"}
    finally:
        fetch_jobs.JOBS.clear()


def test_the_route_tables_forget_what_expired():
    rh = MANAGER.route_health
    rh.success.clear()
    rh.failures.clear()
    now = time.monotonic()
    rh.success[("route", 1)] = (now - 10_000, ("LiveSource", 1), 7)   # older than TTL
    rh.success[("route", 2)] = (now, ("LiveSource", 2), 8)
    rh.failures[("LiveSource", 3)] = (3, now - 10_000)
    rh.failures[("LiveSource", 4)] = (1, now)
    gone = rh.prune()
    assert gone == 2
    assert set(rh.success) == {("route", 2)}
    assert set(rh.failures) == {("LiveSource", 4)}


def test_the_redirect_guard_forgets_expired_handoffs_and_probe_verdicts(monkeypatch):
    # conftest disables the guard suite-wide; this test is about its pruning.
    monkeypatch.setattr(redirect_guard, "DEMOTE_ENABLED", True)
    redirect_guard.reset()
    now = time.monotonic()
    redirect_guard._handed[("route", 1)] = (("LiveSource", 1), 1, now - 10_000)
    redirect_guard._handed[("route", 2)] = (("LiveSource", 2), 2, now)
    redirect_guard._probe_cache["http://cdn/old.ts"] = (
        now - 10_000, redirect_guard.ProbeResult(True, "HEAD 200"))
    redirect_guard._probe_cache["http://cdn/new.ts"] = (
        now, redirect_guard.ProbeResult(True, "HEAD 200"))
    gone = redirect_guard.prune()
    assert gone == 2
    assert set(redirect_guard._handed) == {("route", 2)}
    assert list(redirect_guard._probe_cache) == ["http://cdn/new.ts"]


async def test_a_sweep_reports_what_it_dropped(monkeypatch):
    fetch_jobs.JOBS.clear()
    redirect_guard.reset()
    try:
        fetch_jobs.JOBS["old"] = _job("old", "error", 1.0)
        fetch_jobs.JOBS["new"] = _job("new", "done", 2.0)
        monkeypatch.setattr(janitor, "JOB_HISTORY", 1)
        report = await janitor.sweep_once()
        assert report.get("jobs") == 1
        assert set(fetch_jobs.JOBS) == {"new"}
        assert janitor.stats()["last"]["jobs"] == 1
    finally:
        fetch_jobs.JOBS.clear()
        redirect_guard.reset()


def test_the_scheduler_is_off_when_the_interval_is_zero():
    import asyncio

    async def run() -> None:
        await asyncio.wait_for(janitor.janitor_scheduler(0), timeout=1)

    asyncio.run(run())        # returns immediately instead of sweeping forever
