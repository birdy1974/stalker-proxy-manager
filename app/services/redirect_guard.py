"""Redirect hardening for the 302 path: self-contained and removable.

Two independent, env-gated behaviours for ``StreamManager.resolve()``
("redirect, bypass ffmpeg"):

1. **Open-time link validation** (`link_is_alive`). Before the 302 is issued,
   the candidate URL gets a cheap liveness check: HEAD first, then - only if
   HEAD is inconclusive (403/405/501: alive but method-shy; 500/502/503/504:
   gateway error, which on HEAD may be the upstream refusing the *method*,
   not the stream being dead) - one ranged GET for byte 0 whose connection
   is closed before any body arrives. Dead links are skipped for the next
   chain candidate instead of 302-ing the player into a black screen.

   Deliberately conservative, two ways: anything inconclusive (non-HTTP URL,
   unexpected status) counts as ALIVE - the check may only veto a link it
   positively proved dead. And a veto never feeds route health: our probe
   vantage (server IP) is not the player's, so a failed probe must not poison
   the breaker - the next open simply re-validates (~150 ms).

2. **Reopen demotion** (`note_handed_out` / `demote_recently_handed`).
   Remembers which (source, MAC) each route's last 302 pointed at. When the
   same route is re-asked inside `DEMOTE_WINDOW`, that source's steps move to
   the back and its MAC moves last inside its step, so the reopen tries a
   different route first. Pure reorder: single-step chains and single-MAC
   steps are unaffected (half-open by construction).

   The window deliberately outlasts the redirect lease (180 s): inside the
   lease the MAC is skipped anyway, so demotion's job is the post-lease
   shadow where route affinity would otherwise re-hand the same dead link for
   up to 30 minutes. A false positive (innocent zap-back) costs nothing - the
   fallback plays the same channel - while a true positive avoids a black
   screen, so the window errs long. It also spreads concurrent same-channel
   viewers across routes instead of stacking them on one MAC slot.

Runtime kill switches (restart after changing):
    SPM_REDIRECT_VALIDATE=0     disable validation, keep demotion
    SPM_REOPEN_DEMOTE=0         disable demotion, keep validation

REMOVAL: delete this file and tests/test_redirect_guard.py, then delete every
block between ">>> redirect-guard" and "<<< redirect-guard" markers in
app/services/stream_manager.py (one import + three hooks). Nothing else
references this module.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from ..portal.identity import STB_UA

VALIDATE_ENABLED = os.environ.get("SPM_REDIRECT_VALIDATE", "1") == "1"
VALIDATE_TIMEOUT = float(os.environ.get("SPM_REDIRECT_VALIDATE_TIMEOUT", "2.0"))
DEMOTE_ENABLED = os.environ.get("SPM_REOPEN_DEMOTE", "1") == "1"
DEMOTE_WINDOW = float(os.environ.get("SPM_REOPEN_DEMOTE_WINDOW", "300.0"))

# route_key -> (source_key, mac_id, monotonic time of the last 302)
_handed: dict[tuple, tuple[tuple, int | None, float]] = {}


def _source_key(source) -> tuple:
    return type(source).__name__, int(getattr(source, "id", 0) or 0)


def note_handed_out(route, source, mac) -> None:
    """Remember what a route's last 302 pointed at (for the next reopen)."""
    if not DEMOTE_ENABLED:
        return
    _handed[route] = (_source_key(source), getattr(mac, "id", None),
                      time.monotonic())


def demote_recently_handed(route, chain: list) -> list:
    """Move the route's last-handed (source, MAC) to the back of the walk.

    Runs after the health ordering on purpose: a reopen inside the window
    means "the last link didn't play", which outranks affinity ("it played
    last time" - recorded at handoff, never verified). Stable within groups,
    so every other preference survives.
    """
    if not DEMOTE_ENABLED or not chain:
        return chain
    mem = _handed.get(route)
    if mem is None:
        return chain
    source_key, mac_id, at = mem
    if time.monotonic() - at > DEMOTE_WINDOW:
        _handed.pop(route, None)
        return chain
    out = []
    for src, portal, macs in sorted(chain, key=lambda step: _source_key(step[0]) == source_key):
        if macs and _source_key(src) == source_key and mac_id is not None:
            macs = sorted(macs, key=lambda m: getattr(m, "id", None) == mac_id)
        out.append((src, portal, macs))
    return out


def reset() -> None:
    """Tests only."""
    _handed.clear()


# HEAD answers that do NOT settle the question on HEAD alone: confirm with a
# ranged GET instead of vetoing.
#   403/405/501 - server alive, method-shy.
#   500/502/503/504 - gateway error. On a *streaming* endpoint a 5xx on HEAD
#     is not proof the stream is dead: several panels' stream origins answer
#     HEAD with 502/503 while serving the very same URL to GET fine (observed
#     on the nexusconnects-style portal: every fresh create_link token died
#     on the HEAD probe while the links were usable). The ranged GET - which
#     asks for actual bytes - is the verdict; if IT answers 5xx the link is
#     genuinely dead and still vetoed. A 404/410 stays a hard veto: the
#     token/channel is gone regardless of method.
_HEAD_STATUSES_TRY_GET = frozenset({403, 405, 500, 501, 502, 503, 504})


@dataclass(frozen=True)
class ProbeResult:
    """Verdict of `link_is_alive`. `bool(result)` is `alive`, so
    `if not await link_is_alive(url)` keeps working unchanged; `detail` is
    the probe trace ("HEAD 200", "HEAD 502 -> GET 206", ...) for the logs."""

    alive: bool
    detail: str

    def __bool__(self) -> bool:
        return self.alive


def _referer_of(url: str) -> str:
    """Origin-root referer, the same shape the ffmpeg path sends (see
    StreamManager._copy_fallback_command): some CDNs refuse a bare request."""
    scheme, _, rest = str(url).partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}/"


async def link_is_alive(url: str, *, timeout: float = VALIDATE_TIMEOUT,
                        client: httpx.AsyncClient | None = None) -> ProbeResult:
    """Whether a redirect candidate URL answers (conservative: inconclusive
    counts as alive). `client` is a test seam; production builds its own."""
    if not VALIDATE_ENABLED:
        return ProbeResult(True, "validation disabled")
    if str(url or "").split("://", 1)[0].lower() not in ("http", "https"):
        return ProbeResult(True, "non-HTTP url, not probed")
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                   headers={"User-Agent": STB_UA,
                                            "Referer": _referer_of(url)})
    try:
        try:
            head = await client.head(url)
        except httpx.HTTPError as exc:
            return ProbeResult(False, f"HEAD {type(exc).__name__}")
        if 200 <= head.status_code < 300:
            return ProbeResult(True, f"HEAD {head.status_code}")
        if head.status_code not in _HEAD_STATUSES_TRY_GET:
            return ProbeResult(False, f"HEAD {head.status_code}")
        # Inconclusive on HEAD (method-shy or gateway error): one ranged GET,
        # closed before any body. The status line alone is the verdict - even
        # a 200-with-ignored-Range (full stream) costs us nothing, because we
        # never read the body.
        try:
            async with client.stream("GET", url,
                                     headers={"Range": "bytes=0-0"}) as resp:
                return ProbeResult(200 <= resp.status_code < 300,
                                   f"HEAD {head.status_code} -> GET {resp.status_code}")
        except httpx.HTTPError as exc:
            return ProbeResult(False,
                               f"HEAD {head.status_code} -> GET {type(exc).__name__}")
    finally:
        if own:
            await client.aclose()
