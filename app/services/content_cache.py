"""Process-local invalidation generation for rendered output caches.

SPM deliberately runs as one application process because MAC leases and stream
locks are in memory. That lets database writes invalidate M3U/Enigma2 output
without re-querying counts/max(ids) on every cached read.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_generation = 0


def generation() -> int:
    return _generation


def invalidate() -> int:
    global _generation
    with _lock:
        _generation += 1
        return _generation
