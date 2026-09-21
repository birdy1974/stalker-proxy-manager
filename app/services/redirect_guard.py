"""Redirect hardening for the 302 path: self-contained and removable.

Two independent, env-gated behaviours for ``StreamManager.resolve()``
("redirect, bypass ffmpeg"):

1. **Open-time link validation** (`link_is_alive`). Before the 302 is issued,
   the candidate URL gets a cheap liveness check, a ladder of at most three
   rungs that ends in "alive" unless something was *proved*:

   * **HEAD** - settles it for most CDNs (2xx = alive, 404/410 = dead).
   * **ranged GET for byte 0** - only if HEAD was inconclusive: 403/405/501
     (alive but method-shy), 500/502/503/504 (gateway error, which on HEAD may
     be the upstream refusing the *method*, not the stream being dead), or a
     transport error that is not "nothing is listening" (a live-TS origin that
     hangs up mid-response is answering "I do not do HEAD", not "I am dead").
     The connection is closed before any body arrives.
   * **plain GET, no Range** - only if the ranged GET shrugged too: this is
     byte-for-byte the request a player makes, so it is the last opinion worth
     having before we either veto or hand the link out.

   Dead links are skipped for the next chain candidate instead of 302-ing the
   player into a black screen.

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

from . import stream_identity

VALIDATE_ENABLED = os.environ.get("SPM_REDIRECT_VALIDATE", "1") == "1"
VALIDATE_TIMEOUT = float(os.environ.get("SPM_REDIRECT_VALIDATE_TIMEOUT", "2.0"))
#: How long a probe verdict is trusted (0 disables the cache).
#:
#: The probe sits on the hottest path we have - the round trip the player waits
#: for before it is sent to the CDN - and it costs a full TLS handshake plus RTT
#: to a *media host*, not to our own server. A verdict is a fact about the URL
#: right now, and the zap-back case replays exactly the same URL (LINK_CACHE_S
#: hands the cached link back), so without a cache the cheapest zap we can serve
#: still pays a probe. Dead verdicts get a much shorter window: a link that is
#: dead now may be re-minted by the panel a moment later.
PROBE_TTL_S = float(os.environ.get("SPM_LINK_PROBE_TTL", "20.0"))
PROBE_DEAD_TTL_S = float(os.environ.get("SPM_LINK_PROBE_DEAD_TTL", "3.0"))
PROBE_CACHE_MAX = int(os.environ.get("SPM_LINK_PROBE_CACHE_MAX", "500"))
DEMOTE_ENABLED = os.environ.get("SPM_REOPEN_DEMOTE", "1") == "1"
DEMOTE_WINDOW = float(os.environ.get("SPM_REOPEN_DEMOTE_WINDOW", "300.0"))

# route_key -> (source_key, mac_id, monotonic time of the last 302)
_handed: dict[tuple, tuple[tuple, int | None, float]] = {}

# url -> (monotonic time, verdict); see PROBE_TTL_S
_probe_cache: dict[str, tuple[float, ProbeResult]] = {}
_PROBE_STATS = {"probes": 0, "cache_hits": 0}


def probe_stats() -> dict:
    """Counting probes vs cache hits - the diagnostics view reads this."""
    return dict(_PROBE_STATS)


def _cached_probe(url: str) -> ProbeResult | None:
    """The verdict for this URL, while it is still inside its window."""
    entry = _probe_cache.get(url)
    if not entry:
        return None
    at, verdict = entry
    ttl = PROBE_TTL_S if verdict.alive else PROBE_DEAD_TTL_S
    if ttl <= 0 or (time.monotonic() - at) > ttl:
        _probe_cache.pop(url, None)
        return None
    _PROBE_STATS["cache_hits"] += 1
    return ProbeResult(verdict.alive,
                       f"{verdict.detail} (probed {time.monotonic() - at:.1f}s ago)")


def _remember_probe(url: str, verdict: ProbeResult) -> None:
    if PROBE_TTL_S <= 0:
        return
    now = time.monotonic()
    if len(_probe_cache) >= max(16, PROBE_CACHE_MAX):
        # Cheap bound: drop expired entries, oldest first, until we are back
        # under the cap. The cache only ever holds one entry per URL, so the
        # worst case is a busy proxy touching many distinct links an hour.
        for key, (at, v) in sorted(_probe_cache.items(), key=lambda kv: kv[1][0]):
            if len(_probe_cache) < max(16, PROBE_CACHE_MAX):
                break
            if now - at > (PROBE_TTL_S if v.alive else PROBE_DEAD_TTL_S) or \
                    len(_probe_cache) >= max(16, PROBE_CACHE_MAX):
                _probe_cache.pop(key, None)
    _probe_cache[url] = (now, verdict)


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


def prune() -> int:
    """Drop expired handoffs and stale probe verdicts (see services/janitor.py).

    Both are pruned lazily on access too, but a route nobody asks for again
    keeps its entry forever that way - and a big playlist produces plenty of
    routes.
    """
    now = time.monotonic()
    gone = 0
    for key, (_source, _mac, at) in list(_handed.items()):
        if not DEMOTE_ENABLED or now - at > DEMOTE_WINDOW:
            _handed.pop(key, None)
            gone += 1
    for url, (at, verdict) in list(_probe_cache.items()):
        ttl = PROBE_TTL_S if verdict.alive else PROBE_DEAD_TTL_S
        if ttl <= 0 or now - at > ttl:
            _probe_cache.pop(url, None)
            gone += 1
    return gone


def reset() -> None:
    """Tests only."""
    _handed.clear()
    _shrug_reported.clear()
    _probe_cache.clear()
    _PROBE_STATS["probes"] = _PROBE_STATS["cache_hits"] = 0


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
# Transport errors that are not connect-level join this set by `_is_a_shrug`
# below - same reasoning, different way for the origin to say "not like this".
_HEAD_STATUSES_TRY_GET = frozenset({403, 405, 429, 456, 500, 501, 502, 503, 504})

# A transport error on a probe is not a verdict - except one. ConnectError /
# ConnectTimeout mean nothing listened at the other end (DNS, refused, no
# route): a GET would fail identically, so that IS the proof a veto needs.
# Everything else is the origin misbehaving towards OUR request shape:
#   ReadError / RemoteProtocolError - it took the connection and hung up
#     mid-response. This is how a live-TS endpoint that cannot answer HEAD at
#     all shows up (the script writes a body HEAD must not send, the front end
#     closes early), and it is the same shrug as a 502 on HEAD - observed on
#     the nexusconnects-style portal, where every fresh create_link token
#     "died" on the HEAD probe while the links themselves played fine.
#   ReadTimeout - it is still thinking, which on a live stream is normal.
# Those get the next rung instead of a veto. New httpx error types land here by
# default, which is the point: the ladder may only end in a veto on proof.
_CONNECT_LEVEL_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)

# GET statuses that answer "I am here, but not for that request shape": 405
# method-shy, 416 range-shy - a live stream has no byte 0 to range over, so
# `Range: bytes=0-0` can earn a 416 from an origin that serves the very same
# URL to a plain GET. Both prove the origin is alive and answering, which is
# all this guard ever asks.
_GET_STATUSES_INCONCLUSIVE = frozenset({405, 416})


def _is_a_shrug(exc: BaseException) -> bool:
    """Whether a transport error fails to prove the stream is dead."""
    return not isinstance(exc, _CONNECT_LEVEL_ERRORS)


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
    counts as alive). `client` is a test seam; production builds its own and
    walks the media-UA ladder (player identity first, one browser-UA retry).

    Each client attempt is HEAD -> ranged GET -> plain GET, and each rung is
    only climbed when the one before it failed to *prove* anything. Two rungs
    are normally enough and cost ~150 ms; the third exists for streaming
    origins that shrug at both a HEAD and a Range they cannot honour.
    """
    if not VALIDATE_ENABLED:
        return ProbeResult(True, "validation disabled")
    if str(url or "").split("://", 1)[0].lower() not in ("http", "https"):
        return ProbeResult(True, "non-HTTP url, not probed")
    if client is not None:
        # Explicit test client: one attempt with whatever identity it carries.
        return await _probe(url, timeout, client)
    hit = _cached_probe(url)
    if hit is not None:
        return hit                      # a zap back to a link we just proved
    _PROBE_STATS["probes"] += 1
    verdict = ProbeResult(True, "")
    uas = stream_identity.ladder(url)
    # The URL is handed to the END PLAYER after the 302, so the probe must
    # look like a media player, not the portal browser: play/live.php origins
    # answer the browser UA here with HTTP 456/403 and would be vetoed as
    # "dead" while TiviMate/VLC play the same link. Learned origin first.
    for idx, ua in enumerate(uas):
        async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True,
                headers={"User-Agent": ua, "Referer": _referer_of(url)}) as own:
            verdict = await _probe(url, timeout, own)
        if verdict.alive:
            stream_identity.remember(url, ua)
            break
        # A policy 4xx as the FINAL verdict may be identity-shaped (the 456
        # case above); spend the one other identity the ladder offers. A
        # proved 410, a connect-level error or an inconclusive "alive" verdict
        # ends the probe.
        tail = verdict.detail.rsplit(" ", 1)[-1]
        if tail.isdigit() and int(tail) in stream_identity.UA_POLICY_4XX \
                and idx + 1 < len(uas):
            continue
        break
    _remember_probe(url, verdict)
    return verdict


async def _probe(url: str, timeout: float, client: httpx.AsyncClient) -> ProbeResult:
    """One identity's HEAD -> ranged GET -> plain GET ladder."""
    started = time.monotonic()
    trace: list[str] = []
    # Rung 1: HEAD. Cheap, and the whole answer for a well-behaved CDN.
    try:
        head = await client.head(url)
    except httpx.HTTPError as exc:
        if not _is_a_shrug(exc):
            return ProbeResult(False, f"HEAD {type(exc).__name__}")
        trace.append(f"HEAD {type(exc).__name__}")
    else:
        if 200 <= head.status_code < 300:
            return ProbeResult(True, f"HEAD {head.status_code}")
        if head.status_code not in _HEAD_STATUSES_TRY_GET:
            return ProbeResult(False, f"HEAD {head.status_code}")
        trace.append(f"HEAD {head.status_code}")
    # Rungs 2+3: GET, for actual bytes. Ranged first (polite to a CDN that
    # honours it), then the plain GET a player sends. Neither reads a body -
    # the status line alone is the verdict, so even a 200 that ignored the
    # Range and started the full stream costs us one aborted connection.
    for label, headers in (("GET", {"Range": "bytes=0-0"}), ("GET-no-range", {})):
        if time.monotonic() - started >= timeout * 2:
            # A slow origin must not turn one play into a multi-second wait
            # per candidate; running out of budget is "unproven", not "dead".
            return ProbeResult(True, _trace(trace, "probe budget spent"))
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                code = resp.status_code
        except httpx.HTTPError as exc:
            if not _is_a_shrug(exc):
                return ProbeResult(False, _trace(trace, f"{label} {type(exc).__name__}"))
            trace.append(f"{label} {type(exc).__name__}")
            continue
        if code < 400 or code in _GET_STATUSES_INCONCLUSIVE:
            # 2xx/3xx: it serves the bytes. 405/416: it is alive and only
            # objects to the shape of our request - a player asks differently.
            return ProbeResult(True, _trace(trace, f"{label} {code}"))
        return ProbeResult(False, _trace(trace, f"{label} {code}"))
    # Every rung shrugged. That is not proof of death and only proof may
    # veto: the player's own request may be the one shape this origin likes,
    # and a false "dead" costs a 502 on a channel that is actually on air.
    return ProbeResult(True, _trace(trace, "no verdict, not vetoing"))


#: (origin, trace) pairs already reported - see `shrug_note`
_shrug_reported: set[tuple[str, str]] = set()


def shrug_note(url: str, result: ProbeResult | None) -> str:
    """The note for a handout whose probe had to climb the ladder, or "".

    Empty for the common one-rung case, so a healthy portal's log stays as
    quiet as before. Otherwise it names the shape the origin objected to
    ("HEAD ReadError -> GET-no-range 200" says "this panel does not answer
    HEAD"), which is worth knowing once: from then on a veto against that
    origin means much less than it looks, and the next operator who sees
    `fresh link dead` there has the reason in the log already.

    Once per (origin, trace) per process - the fact matters, the repetition on
    every play of every channel does not.
    """
    detail = str(getattr(result, "detail", "") or "")
    if "->" not in detail:
        return ""
    host = str(url or "").split("://", 1)[-1].split("/", 1)[0]
    if (host, detail) in _shrug_reported:
        return ""
    _shrug_reported.add((host, detail))
    return (f"the origin at {host} is probe-shy ({detail}) - it refused the "
            "probe's request shape but answered a player-shaped one, so the "
            "link was handed out")


def _trace(trace: list[str], last: str) -> str:
    """The probe trace for the logs: "HEAD ReadError -> GET 200"."""
    return " -> ".join([*trace, last])
