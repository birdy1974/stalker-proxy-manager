"""
In-memory API activity counters.

The GUI needs to show "is the API actually doing anything right now", and the
container log needs one line per request. Both are derived from the same
middleware hook (see app/main.py). Nothing here touches the database on
purpose: this is a hot path hit by every request including stream byte pumps,
and the `logs` table is a bounded ring meant for meaningful events.
"""

from __future__ import annotations

import re
import time
from collections import Counter, deque

_STARTED = time.time()
_WINDOW = 60.0                      # "requests per minute" window
_RECENT_MAX = 25                    # rows kept for the dashboard list
_DIGITS = re.compile(r"\d+")

_total = 0
_errors = 0
_slowest_ms = 0.0
_hits: deque[float] = deque()       # timestamps inside the window
_recent: deque[dict] = deque(maxlen=_RECENT_MAX)
_by_path: Counter = Counter()


def _shape(path: str) -> str:
    """
    Collapse item ids so the per-endpoint counter cannot grow forever:
    /play/vod/4711.ts -> /play/vod/#.ts, /live/u1/p2/9.ts -> /live/u#/p#/#.ts.
    """
    return _DIGITS.sub("#", path)[:80]


def record(method: str, path: str, status: int, ms: float, client: str = "") -> None:
    """Called by the access middleware for every request."""
    global _total, _errors, _slowest_ms
    now = time.time()
    _total += 1
    if status >= 500:
        _errors += 1
    if ms > _slowest_ms:
        _slowest_ms = ms
    _hits.append(now)
    while _hits and now - _hits[0] > _WINDOW:
        _hits.popleft()
    key = _shape(path)
    _by_path[key] += 1
    _recent.append({"t": now, "method": method, "path": key, "raw_path": path[:200],
                    "status": status, "ms": round(ms, 1), "client": client})


def snapshot() -> dict:
    """
    What the dashboard renders. Cheap: no I/O, bounded work.

    The request currently being served is not counted yet - the middleware
    records in its `finally`, after the response body is built - so a snapshot
    always describes completed traffic.
    """
    now = time.time()
    while _hits and now - _hits[0] > _WINDOW:
        _hits.popleft()
    return {
        "running": True,
        "uptime_s": int(now - _STARTED),
        "requests_total": _total,
        "requests_last_minute": len(_hits),
        "errors_total": _errors,
        "slowest_ms": round(_slowest_ms, 1),
        "top_paths": [{"path": p, "hits": n} for p, n in _by_path.most_common(8)],
        "recent": list(reversed(_recent)),
    }


def reset() -> None:
    """Tests only."""
    global _total, _errors, _slowest_ms
    _total = _errors = 0
    _slowest_ms = 0.0
    _hits.clear()
    _recent.clear()
    _by_path.clear()
