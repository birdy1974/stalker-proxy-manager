"""Keep SPM's own bookkeeping from growing for as long as it runs.

SPM is meant to live for months on a NAS, and most of its state is bounded by
*configuration*: the portal pool by the portal/MAC rows, the UA ladder by 512
origins, the link cache by 256 entries, parked pipes by `LINGER_S`, the probe
cache by `SPM_LINK_PROBE_CACHE_MAX`. A few structures are bounded by *history*
instead, and history grows forever:

  * the finished job registry (`fetch_jobs.JOBS`) - one entry per sync, per
    genre fetch, per month of use, each holding its detail strings;
  * the route-affinity / source-breaker tables (`MANAGER.route_health`) - one
    entry per (route, source) pair ever played or ever failed;
  * the log table, which is trimmed at boot but never again while the process
    stays up.

None of these leaks fast enough to notice in a week, which is exactly why it is
worth a janitor rather than a bug report in a year. One pass an hour
(`SPM_JANITOR_MINUTES`, 0 disables) drops what has expired, keeps the newest
`SPM_JOB_HISTORY` jobs, and reports what it did - the counters also feed the
diagnostics view.
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger("spm.janitor")

JANITOR_MINUTES = float(os.environ.get("SPM_JANITOR_MINUTES", "60"))
#: How many finished jobs stay visible in the GUI.
JOB_HISTORY = int(os.environ.get("SPM_JOB_HISTORY", "100"))
#: Log rows kept by the periodic trim (the boot trim uses the same number).
LOG_ROWS = int(os.environ.get("SPM_LOG_ROWS", "20000"))

LAST_SWEEP: dict = {}


async def sweep_once() -> dict:
    """One pass. Returns what was dropped (and never raises)."""
    report: dict = {}
    try:
        from .fetch_jobs import prune_jobs
        report["jobs"] = prune_jobs(JOB_HISTORY)
    except Exception:  # noqa: BLE001 - a janitor that can break the app is worse
        log.exception("job registry prune failed")
    try:
        from .stream_manager import MANAGER
        report["routes"] = MANAGER.route_health.prune()
    except Exception:  # noqa: BLE001
        log.exception("route table prune failed")
    try:
        from . import redirect_guard
        report["handoffs"] = redirect_guard.prune()
    except Exception:  # noqa: BLE001
        log.exception("redirect guard prune failed")
    try:
        from .db_logging import cleanup_logs
        await cleanup_logs(LOG_ROWS)
        report["logs"] = "trimmed"
    except Exception:  # noqa: BLE001
        log.exception("log trim failed")
    dropped = {k: v for k, v in report.items() if isinstance(v, int) and v}
    if dropped:
        log.info("janitor: %s", ", ".join(f"{k}={v}" for k, v in dropped.items()))
    LAST_SWEEP.clear()
    LAST_SWEEP.update(report)
    return report


async def janitor_scheduler(interval: float | None = None) -> None:
    """Run `sweep_once` forever, once per interval (cancelled at shutdown)."""
    minutes = JANITOR_MINUTES if interval is None else interval
    if minutes <= 0:
        return
    while True:
        try:
            await asyncio.sleep(minutes * 60)
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must outlive any single pass
            log.exception("janitor sweep failed")


def stats() -> dict:
    return {"minutes": JANITOR_MINUTES, "job_history": JOB_HISTORY,
            "log_rows": LOG_ROWS, "last": dict(LAST_SWEEP)}
