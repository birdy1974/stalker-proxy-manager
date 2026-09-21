"""Playback gets the panel's attention first; background jobs wait their turn.

Everything SPM does with a panel shares one budget, and it is not our budget:
the account's connection slots, and whatever rate limiter the panel runs. A
catalogue sync walking 4,000 channels, a MAC health sweep, an EPG refresh that
falls back to a short-EPG call per channel - each is a stream of requests to the
same host that a play is trying to start on. When the panel answers the *play*
with `limit` or a 429, the job that caused it does not care: it retries. The
user does.

So there is one small gate, deliberately not a queue or a scheduler:

    await pace_for_playback(portal_id)      # between background requests

While a stream is live on that portal, a background caller sleeps a little
longer between requests (`SPM_PLAYBACK_PACE_S`, default 0.4 s). Nothing is
starved and nothing is cancelled - the sync still finishes, a few seconds later,
and the play gets the panel to itself. `SPM_PLAYBACK_PACE=0` turns it off.

The gate reads the live stream registry rather than keeping its own state, so a
parked pipe (see LINGER_S) does *not* hold a portal back: nobody is watching it,
and the point of parking is that a zap back is cheap, not that background work
stops.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

PACE_ENABLED = os.environ.get("SPM_PLAYBACK_PACE", "1") not in ("0", "false", "no", "")
#: Extra delay, per background request, while a stream is live on that portal.
PACE_S = float(os.environ.get("SPM_PLAYBACK_PACE_S", "0.4"))

#: portal_id -> how many background requests were slowed down (diagnostics).
_paced: dict[Any, int] = {}
#: portal_id -> how many were asked to wait at all (paced + already fine).
_asked: dict[Any, int] = {}


def playback_portal_ids() -> set:
    """Portal ids with a stream that somebody is actually watching right now."""
    from .stream_manager import MANAGER           # local: avoid an import cycle
    ids = set()
    for h in MANAGER.streams.values():
        if h.dead or h.parked:
            continue
        pid = getattr(h, "portal_id", None)
        if pid is not None:
            ids.add(pid)
    return ids


def playback_active(portal_id: Any) -> bool:
    if portal_id is None:
        return False
    return portal_id in playback_portal_ids()


async def pace_for_playback(portal_id: Any) -> bool:
    """Yield to a live play on this portal. Returns True when it did.

    Called between background requests (catalogue pages, MAC health rows, EPG
    channels). Cheap by design: no lock, no queue, one dict read and - only when
    a play is actually running - one short sleep.
    """
    if not PACE_ENABLED or portal_id is None:
        return False
    _asked[portal_id] = _asked.get(portal_id, 0) + 1
    if not playback_active(portal_id):
        return False
    _paced[portal_id] = _paced.get(portal_id, 0) + 1
    await asyncio.sleep(max(0.0, PACE_S))
    return True


def stats() -> dict:
    """What the gate did, for the diagnostics view."""
    return {"enabled": PACE_ENABLED, "pace_s": PACE_S,
            "asked": dict(_asked), "paced": dict(_paced)}


def reset() -> None:
    _paced.clear()
    _asked.clear()
