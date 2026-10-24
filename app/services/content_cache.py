"""Process-local invalidation generation for rendered output caches.

SPM deliberately runs as one application process because MAC leases and stream
locks are in memory. That lets database writes invalidate M3U/Enigma2 output
without re-querying counts/max(ids) on every cached read.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_generation = 0
_health_generation = 0


def generation() -> int:
    return _generation


def invalidate() -> int:
    global _generation
    with _lock:
        _generation += 1
        return _generation


def health_generation() -> int:
    """Generation for the playlist-health snapshot.

    Kept separate from the rendered-output generation on purpose: health
    also reflects tables whose writes must NOT bust the M3U/Enigma caches
    (MAC status rows change on account checks; source/episode rows churn
    during syncs without changing any rendered playlist text), and some
    rendered-output tables (EPG programmes) churn far too much to gate a
    health snapshot on. database.py bumps each counter for its own tables.
    """
    return _health_generation


def invalidate_health() -> int:
    global _health_generation
    with _lock:
        _health_generation += 1
        return _health_generation
