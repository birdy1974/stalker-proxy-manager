"""
Stream manager - the actual PROXY core.

Responsibilities
  * MAC occupancy: a MAC is locked while one of our ffmpeg pipes uses it, so a
    portal never sees more concurrent streams than allowed (spec: "portal/mac
    already used for another stream" -> fall back).
  * Fallback chain: live/vod/serie playlist items carry ORDERED source lists
    (priority). Chain walk tries sources (and the portal's MACs per the global
    strategy) until stream bytes actually flow - not just until create_link
    succeeded: dead links that yield no data also trigger fallback.
  * ffmpeg supervision: every stream (transcode AND copy) runs as one ffmpeg
    process with stdout piped to the HTTP client. Mid-stream EOF/stalls move
    to the next fallback within the SAME client response, so players keep
    playing. Client disconnect => process killed, MAC released instantly.
  * Runtime mirror in `active_streams` for the dashboard (kill/stop buttons).

The byte generator is deliberately defensive: NOTHING raised inside escapes
unlogged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import logging
import shlex
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select, update

from ..config import FFMPEG_BIN, STREAM_START_TIMEOUT
from ..database import SessionLocal, run_uncancelled
from ..models import (
    ActiveStream, FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource,
    LocalFile, LocalPlaylist, LocalSource, MacAddress, Portal, SerieEpisode, SeriePlaylist,
    SeriePlaylistSource, SerieSeason, SerieSource, VodPlaylist, VodPlaylistSource,
    VodSource,
)
from ..portal.account import mac_is_usable
from ..portal.pool import POOL, PortalSession
from ..portal.client import SLOT_BUSY_CODES, PortalError, is_hls
from ..portal.links import plan_adopted, plan_for
from . import stream_identity
from .channel_translations import attach_overrides
from .db_logging import db_log
from .ffmpeg_templates import (COPY_PRESET_NAME, HLS_ALLOWED_EXTENSIONS,
                               HLS_PROTOCOL_WHITELIST, REDIRECT_COMMAND,
                               URL_PLACEHOLDER, argv_validation_errors,
                               mpegts_copy_command, serves_original_file)
from .probe import media_codecs, prime_local_startup_cache, subtitle_streams
from .item_info import local_file_path
# >>> redirect-guard (features 1+2; delete with app/services/redirect_guard.py)
from .redirect_guard import (demote_recently_handed, link_is_alive, note_handed_out,
                             shrug_note)
# <<< redirect-guard

log = logging.getLogger("spm.stream")


class FFmpegTemplateError(RuntimeError):
    """The stored/generated command cannot be used for this stream.

    This is deliberately distinct from a source/MAC failure: retrying the same
    malformed argv against every MAC only delays the useful error and can hold
    portal connection slots while a command that can never work is retried.
    """


_OUTPUT_FAILURE_MARKERS = (
    "unable to choose an output format",
    "unable to find a suitable output format",
    "error initializing the muxer",
    "error initializing output stream",
    "error opening output file",
    "could not write header for output file",
    "output file #0 does not contain any stream",
    "unknown encoder",
    "no such filter",
    "invalid filtergraph",
    "error reinitializing filters",
    "error while opening encoder",
)


def _is_template_output_failure(fail: dict | None) -> bool:
    """True when FFmpeg opened/parsed the input but failed at output setup."""
    if not fail or fail.get("stalled") or fail.get("rc") in (None, 0):
        return False
    text = str(fail.get("tail") or "").lower()
    if any(marker in text for marker in _OUTPUT_FAILURE_MARKERS):
        return True
    # FFmpeg sometimes only leaves the final "Invalid argument" line. Require
    # output-shaped context so a bad input URL can still use source/MAC fallback.
    return (fail.get("rc") == 234 and "invalid argument" in text
            and any(word in text for word in ("output", "pipe:", "scale_", "map ")))


async def _store_resolved_portal(portal_id: int, portal_url: str, path: str | None = None) -> None:
    """Persist endpoint discovery performed on the playback fast path.

    Chain rows are intentionally loaded in a short-lived session, so mutating
    the detached ``Portal`` object after discovery used to be lost.  That made
    every first play of an unresolved portal repeat all resolver probes.  The
    resolver is normally the slowest part of startup; remember its answer for
    the next play (the portal settings endpoints already clear these fields
    when the base URL or TLS policy changes).
    """
    if not portal_id or not portal_url:
        return
    async with SessionLocal() as session:
        values = {"resolved_url": portal_url}
        if path:
            values["resolved_path"] = path
        await session.execute(update(Portal).where(Portal.id == portal_id).values(**values))
        await session.commit()


async def _store_media_cmd(src, repair, item_name: str = "") -> None:
    """Persist the cmd FORM the panel actually answered for (S-B), and say so.

    The same reasoning as `_store_resolved_portal`: chain rows are loaded in a
    short-lived session, so a form learned during a play has to be written by id
    or it is gone - and gone means paying the refusal plus the movie resolution
    on EVERY play of that item, forever.

    Only `media_cmd` is written, never `cmd`: the catalogue value is the panel's
    truth, it is what the GUI shows, and a re-fetch must not have to guess which
    half of the row we edited. Writing NULL is a real action too - when a stored
    form was refused and the catalogue one worked, the learned form is stale
    (a re-ingested movie gets a new file id) and has to go.
    """
    model = type(src)
    # LiveSource has no storage form to learn, and an adopted Xtream row has no
    # portal cmd at all; both are "nothing to persist", not an error.
    if not hasattr(model, "media_cmd") or getattr(src, "id", None) is None:
        return
    worked = str(getattr(repair, "worked", "") or "").strip()
    asked = str(getattr(repair, "asked", "") or "")
    new = worked if worked and worked != str(getattr(src, "cmd", "") or "") else None
    if (getattr(src, "media_cmd", None) or None) == new:
        return                               # the row already says exactly this
    async with SessionLocal() as session:
        await session.execute(update(model).where(model.id == src.id).values(media_cmd=new))
        await session.commit()
    tag = f"[{item_name}] " if item_name else ""
    if new:
        await db_log("INFO", "stream",
                     f"{tag}this panel wants another cmd form for this item: "
                     f"{asked!r} -> {new!r} - remembered on the source row, so the "
                     "next play asks with it directly")
    else:
        await db_log("INFO", "stream",
                     f"{tag}the remembered cmd form {asked!r} was refused and the "
                     "catalogue cmd worked - cleared it from the source row")


# Seconds without a single byte before the pipe counts as finished. 25 s is the
# tolerance for a source that is *starting* (panel-side buffering, a slow CDN
# edge, a NAS that spins up). Once a live stream has been flowing, the same
# silence means something different - the source dropped - and the response can
# be continued by re-resolving (MIDSTREAM_RESTARTS), so waiting 25 s only makes
# the picture freeze for 25 s. See _stall_window().
STREAM_STALL_TIMEOUT = float(os.environ.get("SPM_STREAM_STALL_TIMEOUT", "25.0"))
STREAM_STALL_TIMEOUT_LIVE = float(os.environ.get(
    "SPM_STREAM_STALL_TIMEOUT_LIVE", "10.0"))
CHUNK = 64 * 1024
# input options that only exist for network protocols (stripped for file://)
_NETONLY_OPTS = re.compile(
    r"-(?:reconnect\w*|-?rw_timeout|timeout|user_agent|headers|http_proxy"
    r"|seekable|multiple_requests|referer)\b\s*(?:\S+)?")
_NET_SCHEMES = ("http://", "https://")
# Subtitle codecs a transcoded MPEG-TS pipe can keep WITHOUT a re-encode.
# Only native DVB rides MPEG-TS as a copy. PGS/DVD -> dvbsub is a CPU-heavy
# convert that regularly emits no output before the 25s start timeout (the
# Enigma2 VOD "502, 0.0 MB" failure). Text subs cannot enter MPEG-TS at all.
_TS_NATIVE_SUB_CODECS = {"dvb_subtitle"}
# The other half of the story: a MATROSKA output (subs="keep", the Enigma2 VOD
# path) carries text subtitles too, so nothing has to be dropped there - the
# tracks are copied byte for byte beside a video pipeline that stays fully
# hardware. Only these few codecs have no place in a Matroska file and would
# abort the muxer, so the gate maps around them.
_MKV_UNSUPPORTED_SUB_CODECS = {"dvb_teletext", "eia_608", "eia_708", "cea_608",
                               "arib_caption", "hdmv_text_subtitle"}
# Subtitle language tags that count as "Dutch or English" for the Matroska
# output check (ISO-639-1 + ISO-639-2/B + the spellings ffmpeg may print;
# the region part of BCP47 like en-GB / nl-NL is split off before comparing).
MKV_PREF_SUB_LANGS = frozenset({"nl", "nld", "dut", "dutch", "en", "eng", "english"})
# Audio codecs MPEG-TS can carry as a bare copy (and Enigma2 can decode).
# Anything else in a copy remux (Vorbis/FLAC/PCM/ALAC/Opus - common in MKV)
# aborts ffmpeg at output init with zero bytes, so the remux gate re-encodes
# the audio alone to AC3 instead (the video pipeline stays copy).
_TS_AUDIO_CODECS = {"mp1", "mp2", "mp3", "aac", "aac_latm", "ac3", "eac3", "dts"}
# Length-prefixed video layouts the mpegts muxer needs start codes for, and
# the bitstream filter that performs the conversion. Anything NOT in this
# table (MPEG-1/2, MPEG-4 part 2, VC-1) carries start codes natively: a
# h264_mp4toannexb filter applied to one of those aborts ffmpeg with rc=234
# before the first output byte ("produced no data for local file").
_TS_VIDEO_BSF = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb",
                 "vvc": "vvc_mp4toannexb"}
# How long the ffmpeg CLI may buffer packets waiting for a lagging stream
# before it flushes anyway (mux-level, microseconds). ffmpeg's own default is
# 10 s: a file whose audio starts 30-60 s into the video (typical re-authored
# captures) then sits silent for the full 10 s, which is exactly the
# "no data within N s -> 502" failure for anything that starts slow (network
# VOD on top of a slow portal, a hard disk that has to spin up). Two seconds
# is plenty for normal A/V skew and keeps stream start responsive.
MAX_INTERLEAVE_DELTA_US = os.environ.get("SPM_MAX_INTERLEAVE_DELTA_US", "2000000")
ROUTE_AFFINITY_TTL = float(os.environ.get("SPM_ROUTE_AFFINITY_TTL", "1800"))
SOURCE_BREAKER_FAILURES = max(1, int(os.environ.get("SPM_SOURCE_BREAKER_FAILURES", "2")))
SOURCE_BREAKER_COOLDOWN = float(os.environ.get("SPM_SOURCE_BREAKER_COOLDOWN", "45"))


class _RouteHealth:
    """Process-local route affinity and a short source circuit breaker.

    Playlist priority remains the fallback order; only a route that actually
    produced bytes is promoted on its next play. Repeated source-specific
    failures temporarily suppress that source when another choice exists.
    """

    def __init__(self) -> None:
        self.success: dict[tuple, tuple[float, tuple, int | None]] = {}
        self.failures: dict[tuple, tuple[int, float]] = {}

    @staticmethod
    def source_key(source) -> tuple:
        return type(source).__name__, int(getattr(source, "id", 0) or 0)

    def ordered_chain(self, route: tuple | None, chain: list) -> list:
        now = time.monotonic()
        preferred = self.success.get(route) if route else None
        if preferred and now - preferred[0] > ROUTE_AFFINITY_TTL:
            self.success.pop(route, None)
            preferred = None
        healthy, open_ = [], []
        for step in chain:
            state = self.failures.get(self.source_key(step[0]))
            (open_ if state and state[0] >= SOURCE_BREAKER_FAILURES
             and now - state[1] < SOURCE_BREAKER_COOLDOWN else healthy).append(step)
        # Keep one half-open candidate if every configured route is cooling down.
        ordered = healthy if healthy else open_[:1]
        if preferred:
            source_key = preferred[1]
            ordered.sort(key=lambda step: self.source_key(step[0]) != source_key)
        return ordered

    def ordered_macs(self, route: tuple | None, source, macs: list) -> list:
        preferred = self.success.get(route) if route else None
        if not preferred or preferred[1] != self.source_key(source):
            return macs
        mac_id = preferred[2]
        return sorted(macs, key=lambda mac: getattr(mac, "id", None) != mac_id)

    def prune(self) -> int:
        """Forget what has expired: stale route affinity and cooled-down breakers.

        Both tables are keyed by (route, source) pairs that history keeps
        producing; nothing else ever removes an entry whose key is never asked
        for again (an item deleted from the playlist, a source removed, a
        one-off route). See services/janitor.py.
        """
        now = time.monotonic()
        gone = 0
        for route, entry in list(self.success.items()):
            if now - entry[0] > ROUTE_AFFINITY_TTL:
                self.success.pop(route, None)
                gone += 1
        for key, state in list(self.failures.items()):
            if now - state[1] > SOURCE_BREAKER_COOLDOWN:
                self.failures.pop(key, None)
                gone += 1
        return gone

    def failed(self, source) -> None:
        from .playlist_health import record_playback
        key = self.source_key(source)
        count, _ = self.failures.get(key, (0, 0.0))
        self.failures[key] = (count + 1, time.monotonic())
        if count + 1 >= SOURCE_BREAKER_FAILURES:
            record_playback(source, failed=True, ttl=SOURCE_BREAKER_COOLDOWN)

    def succeeded(self, route: tuple | None, source, mac, *, verified_media: bool = False) -> None:
        from .playlist_health import record_playback
        key = self.source_key(source)
        # A successful redirect only handed out a URL, not verified media.
        if verified_media:
            record_playback(source)
        self.failures.pop(key, None)
        if route:
            self.success[route] = (time.monotonic(), key, getattr(mac, "id", None))


class _WithTemplate:
    """Wrap a source so _template_for picks an explicitly chosen template."""

    def __init__(self, src, template_id: int) -> None:
        self._src = src
        self.ffmpeg_template_id = template_id

    def __getattr__(self, item):
        return getattr(self._src, item)


@dataclass
class StreamHandle:
    """One client-visible stream (dashboard row + kill target)."""
    id: str
    kind: str                      # live | vod | episode | local | preview
    item_name: str
    user_name: str | None
    template_name: str
    command: str                   # rendered ffmpeg command with url placeholder
    started: float = field(default_factory=time.time)
    portal_name: str = ""
    #: Which portal row this stream is playing from. The dashboard does not need
    #: it (the name is readable); the playback gate does - see portal_pace.py.
    portal_id: int | None = None
    mac: str = ""
    url: str = ""
    bytes_sent: int = 0
    proc: asyncio.subprocess.Process | None = None
    dead: bool = False
    route_key: tuple | None = None
    #: Seconds the fallback engine may spend looking for a first byte (see
    #: STREAM_START_BUDGET). The output guard waits at least this long + slack,
    #: so the engine is never cut off mid-chain by a fixed 25s timer.
    start_budget: float = 0.0
    #: One line per candidate MAC/source that did not produce data, oldest
    #: first. This is what turns "[Npo 1] produced no data within 25s" into a
    #: report that names the MACs and the reason each one failed.
    attempts: list[str] = field(default_factory=list)
    #: Set when the engine stops early (budget spent) - merged into the 502.
    fail_note: str = ""
    #: Set when a redirect lease held by a *different* play of the same user was
    #: taken over (a zap), so the log can say why the "busy" MAC was used.
    took_over_lease: bool = False
    #: Set when the reason this stream could not start is *our own* occupancy or
    #: the panel's connection slot - not a dead source. The output route turns
    #: that into 503 + Retry-After instead of 404, which is the honest answer to
    #: "come back in a second" and the one players retry (STB-Proxy answers 503
    #: for the same case; a 404 tells the player the channel does not exist).
    busy: bool = False
    #: Set while the pipe is being HELD after its client left (see LINGER_S):
    #: the process keeps running, its MAC stays locked (the panel still counts
    #: the connection), and the bytes keep being drained into `ring` so a zap
    #: back can attach to a live stream instead of starting a new one.
    parked: bool = False
    #: Bytes of the parked stream, oldest dropped (LINGER_BUFFER_KB). Dropped on
    #: attach, handed to the returning client first so it sees no gap.
    ring: bytearray = field(default_factory=bytearray)
    #: The task draining the parked pipe - cancelled when a client attaches.
    parker: asyncio.Task | None = None
    #: How often this pipe was re-used by a returning client (park/attach wins).
    reattaches: int = 0

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "item_name": self.item_name,
                "user_name": self.user_name, "portal_name": self.portal_name,
                "mac": self.mac, "template_name": self.template_name,
                "started": self.started, "bytes_sent": self.bytes_sent,
                "url": self.url, "pid": self.proc.pid if self.proc else None}

    def note_attempt(self, text: str) -> None:
        """Remember one candidate outcome (bounded, oldest dropped)."""
        if not text:
            return
        self.attempts.append(str(text))
        del self.attempts[:-ATTEMPT_TRACE]

    @property
    def trace(self) -> str:
        """The attempt trace as one log-friendly line, '' when nothing failed."""
        return "; ".join(self.attempts)



# How long a handle may sit in the registry with its ffmpeg process gone and no
# new bytes before the reaper concludes the teardown was lost and frees it.
# Must exceed STREAM_START_TIMEOUT + the stall window, otherwise the reaper
# would kill a stream that is legitimately walking its fallback chain.
REAP_GRACE = 45.0


# --------------------------------------------------------------------------- #
#  per-process zap memory: (kind, item, MAC) -> link, and -> recent failure
# --------------------------------------------------------------------------- #
#: (kind, ref_id, mac_id, user) -> (url, monotonic when resolved)
_LINK_CACHE: dict[tuple, tuple[str, float]] = {}
#: (route_key, source_key, mac_id) -> monotonic of the last failed attempt
_CANDIDATE_FAILURES: dict[tuple, float] = {}


def note_link(kind: str, ref_id: int, mac_id: int | None, url: str,
              user: str | None = None) -> None:
    """Remember a link that just worked well enough to hand out (LINK_CACHE_S)."""
    if LINK_CACHE_S <= 0 or not url or mac_id is None or kind not in LINK_CACHE_KINDS:
        return
    _LINK_CACHE[_link_key(kind, ref_id, mac_id, user)] = (str(url), time.monotonic())
    if len(_LINK_CACHE) > 256:
        oldest = min(_LINK_CACHE, key=lambda k: _LINK_CACHE[k][1])
        _LINK_CACHE.pop(oldest, None)


def _link_key(kind: str, ref_id: int, mac_id: int, user: str | None) -> tuple:
    """One cached link: item + MAC + the user it was resolved for.

    The user belongs in the key. The URL itself carries only the panel's MAC
    session, so it *would* work for somebody else - but handing user B the link
    user A asked for is a sharing decision this cache has no business making,
    and the case the cache exists for (a zap away and back) is always the same
    user anyway. `user or ""` keeps the admin/preview plays (no user) together.
    """
    return (kind, int(ref_id), int(mac_id), user or "")


def cached_link(kind: str, ref_id: int, mac_id: int | None,
                *, user_name: str | None = None) -> tuple[str, float] | None:
    """(url, age in seconds) for a link resolved recently enough to replay.

    A zap back inside the window costs one 302 and no portal call at all (see
    `_link_key` for why the user is part of the key).
    """
    if LINK_CACHE_S <= 0 or mac_id is None or kind not in LINK_CACHE_KINDS:
        return None
    got = _LINK_CACHE.get(_link_key(kind, ref_id, mac_id, user_name))
    if not got:
        return None
    url, at = got
    age = time.monotonic() - at
    if age > LINK_CACHE_S:
        _LINK_CACHE.pop(_link_key(kind, ref_id, mac_id, user_name), None)
        return None
    return url, age


def drop_link(kind: str, ref_id: int, mac_id: int | None,
              user: str | None = None) -> None:
    """Forget a cached link (it just failed, or the MAC changed hands)."""
    if mac_id is None:
        return
    _LINK_CACHE.pop(_link_key(kind, ref_id, mac_id, user), None)


def note_candidate_failure(route, source, mac) -> None:
    """Remember that (route, source, MAC) just failed, for the demotion below."""
    if route is None or mac is None:
        return
    _CANDIDATE_FAILURES[(route, _RouteHealth.source_key(source),
                         getattr(mac, "id", None))] = time.monotonic()
    if len(_CANDIDATE_FAILURES) > 512:
        for key, at in sorted(_CANDIDATE_FAILURES.items(), key=lambda kv: kv[1])[:128]:
            _CANDIDATE_FAILURES.pop(key, None)


def clear_candidate_failures(route) -> None:
    """Forget every recent failure for a route (called when a play succeeds)."""
    if route is None:
        return
    for key in [k for k in _CANDIDATE_FAILURES if k[0] == route]:
        _CANDIDATE_FAILURES.pop(key, None)


def _failed_recently(route, source, mac) -> bool:
    if route is None or mac is None:
        return False
    at = _CANDIDATE_FAILURES.get((route, _RouteHealth.source_key(source),
                                  getattr(mac, "id", None)))
    return at is not None and (time.monotonic() - at) <= FAILURE_DEMOTE_S


def demote_macs(route, source, macs: list) -> list:
    """Push MACs that recently failed on this route behind the others."""
    if not macs or FAILURE_DEMOTE_S <= 0 or route is None:
        return macs
    return sorted(macs, key=lambda m: _failed_recently(route, source, m))


def demote_failed_candidates(route, chain: list) -> list:
    """Reorder one chain so a (source, MAC) that just failed is tried last.

    Runs after the health ordering on purpose: route affinity says "this worked
    last time" (recorded at handoff), a failure says "this did not work now" -
    and now outranks last time. Stable within groups, so playlist priority and
    the breaker's half-open choice both survive.
    """
    if not chain or FAILURE_DEMOTE_S <= 0 or route is None:
        return chain
    out = []
    for src, portal, macs in chain:
        if macs:
            macs = demote_macs(route, src, list(macs))
        out.append((src, portal, macs))
    out.sort(key=lambda step: bool(step[2]) and all(
        _failed_recently(route, step[0], m) for m in step[2]))
    return out


#: env var every spawned ffmpeg carries (see `_spawn` and `sweep_orphans`)
_STREAM_ENV_MARKER = "SPM_STREAM_ID"


def _orphan_ffmpeg_pids(root: str = "/proc") -> list[int]:
    """PIDs of ffmpeg pipes spawned by an SPM that is no longer running.

    Matched on two things together: the executable is our configured
    `FFMPEG_BIN`, and the process carries `_STREAM_ENV_MARKER` in its
    environment. A user's own ffmpeg (a manual transcode, another container on
    the same host) fails at least one of those, so this cannot kill work it did
    not start. Linux-only by nature; on a platform without `/proc` it returns
    nothing and the caller does nothing.
    """
    if not os.path.isdir(root):
        return []
    want = os.path.basename(FFMPEG_BIN)
    me = os.getpid()
    found: list[int] = []
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        base = os.path.join(root, entry)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as fh:
                cmdline = fh.read().split(b"\0")
        except OSError:
            continue
        if not cmdline or not cmdline[0]:
            continue
        exe = os.path.basename(cmdline[0].decode("utf-8", "replace"))
        if exe != want:
            continue
        try:
            with open(os.path.join(base, "environ"), "rb") as fh:
                environ = fh.read()
        except OSError:
            continue
        if _STREAM_ENV_MARKER.encode() + b"=" in environ:
            found.append(pid)
    return found


#: First-byte windows per candidate. The first candidate gets the full
#: STREAM_START_TIMEOUT - it is the source the engine believes in, and a panel
#: can legitimately take its time to open the media path. After that, a silent
#: candidate is much more likely to be a dead edge than a slow one, and waiting
#: the full window for each of them is what turns a two-MAC chain into 24 s of
#: black screen before the player sees an error. Measured on the demo instance:
#: a candidate that produces bytes does so in ~550 ms, so a 5 s second window
#: costs nothing real and caps the walk at a fraction of the old worst case.
STREAM_START_TIMEOUT_REST = float(os.environ.get("SPM_STREAM_START_TIMEOUT_REST", "5"))

#: When the chain has enough free candidates, a silent one is given only
#: `SPM_HEDGE_AFTER_S` (2 s) before the walk moves on.
#:
#: The measured reality is that a candidate which is going to answer does so in
#: well under a second (a live start on the demo instance: ~550 ms end to end,
#: panel RTT included), while a candidate that is silent at 2 s is nearly always
#: a dead edge or a refused media path. The old shape gave every candidate the
#: full window, so a two-MAC chain could sit black for up to 24 s before the
#: player saw anything - the single most-visible symptom the stability work is
#: about. With an alternative in the chain there is no reason to pay that: the
#: fence is only lowered while candidates are actually free to play, so a walk
#: with nothing free keeps the patient windows.
#:
#: That fence used one blanket rule for every kind of item, and the VOD logs
#: made the hole in it obvious: `create_link` answers 200 in ~80 ms for every
#: MAC, so all 4 stay "free" for a whole VOD walk, every candidate gets hedge-cut
#: to 2 s, and a movie that needs longer than 2 s to open (a slow storage the
#: panel picked, a cold CDN edge; live was measured at ~550 ms but VOD can need
#: several seconds) was declared dead before its first byte - six 2 s windows,
#: zero bytes, 502. The fence must skip at least the first candidate (the one
#: the engine believes in - by *this* play's order, not by the playlist's),
#: and `SPM_HEDGE_KINDS` excludes VOD/episode/local so a start that is genuinely
#: slow is waited for. Higher thresholds stay available for live zapping, where
#: the 550 ms measurement came from.
#:
#: 0 disables it. Deliberately *not* a parallel race: two simultaneous
#: create_links would hold two panel slots for one zap, and on the panels this
#: was measured against a second slot is exactly what answers `limit`.
HEDGE_AFTER_S = float(os.environ.get("SPM_HEDGE_AFTER_S", "2.0"))
#: Hedge fence: never before the 2nd candidate (the first one is the engine's
#: best guess and gets a patient window), and never for sorts of items whose
#: first byte is measured in seconds, not milliseconds (VOD/episode/local).
#: `0` (the new default) expects the whole chain to be free; thresholds higher
#: than 1 still ask for that many free candidates. The common false positive
#: is a slow VOD or slow storage, so users are expected to lower a tuned value
#: *back to 0* rather than raise it, which is what a field tuned for live only
#: leaves behind.
HEDGE_MIN_CANDIDATES = max(0, int(os.environ.get("SPM_HEDGE_MIN_CANDIDATES", "0")))
HEDGE_KINDS = {k.strip() for k in os.environ.get("SPM_HEDGE_KINDS", "live").split(",")
               if k.strip()}

#: A live stream that dies mid-play is restarted inside the SAME client response.
#: The usual cause is not a dead channel but a dead *link*: panels invalidate the
#: per-session URL after a while (and CDNs drop long-lived connections), so the
#: pipe ends while the channel is healthy. Without this the chain walk stops at
#: the first candidate that fails to produce data again and the response ends -
#: players either freeze or reconnect, and the user reports "it stops after a
#: while". 0 disables the restarts.
MIDSTREAM_RESTARTS = int(os.environ.get("SPM_MIDSTREAM_RESTARTS", "3"))
MIDSTREAM_RESTART_DELAY = float(os.environ.get("SPM_MIDSTREAM_RESTART_DELAY", "1.5"))
#: kinds allowed to restart (a VOD that ended was *finished*, not dropped)
MIDSTREAM_RESTART_KINDS = {k.strip() for k in
                           os.environ.get("SPM_MIDSTREAM_RESTART_KINDS", "live").split(",")
                           if k.strip()}

#: How long a live pipe is kept alive after its client disappeared, so a zap
#: away-and-back attaches to the stream that is still running instead of asking
#: the panel for a new link and starting ffmpeg again (measured: ~560 ms cold on
#: this instance, vs ~20-50 ms to attach). This is what makes channel flipping
#: feel instant in the reference proxy - the difference being that STB-Proxy
#: keeps nothing at all and pays the cold start every time.
#:
#: The parked pipe keeps its MAC lock (the panel is still sending us that
#: stream), it is NOT counted against the user's connection limit, and it is
#: preemptible: the moment anybody actually wants that MAC (the same user
#: zapping to another channel, or another user with nothing else free) it is
#: killed immediately. 0 disables parking entirely.
LINGER_S = float(os.environ.get("SPM_LINGER_S", "8"))
LINGER_BUFFER_KB = int(os.environ.get("SPM_LINGER_BUFFER_KB", "2048"))
LINGER_KINDS = {k.strip() for k in os.environ.get("SPM_LINGER_KINDS", "live").split(",")
                if k.strip()}

#: portal ids already told about `portal_first` (once per process, not per play)
_PORTAL_FIRST_NOTED: set[int] = set()


def _warn_portal_first_once(portal_id: int, macs: int) -> None:
    if portal_id in _PORTAL_FIRST_NOTED:
        return
    _PORTAL_FIRST_NOTED.add(portal_id)
    log.warning("fallback strategy 'portal_first' uses 1 of this portal's %d MAC(s) "
                "(%s): a zap then asks the same MAC every time, which a panel that "
                "counts connections per MAC refuses until its slot frees. Use "
                "'macs_first' to walk them all", macs, f"portal {portal_id}")


def reset_zap_state() -> None:
    """Tests only: forget the link cache and the failure demotions."""
    _LINK_CACHE.clear()
    _CANDIDATE_FAILURES.clear()
    _PORTAL_FIRST_NOTED.clear()


# After a 302 redirect we no longer hold the socket, so we cannot know when the
# player stops. create_link itself often opens a panel slot though, and a health
# handshake on that same MAC mid-play can kick the viewer (or burn the slot).
# A soft lease covers the typical live-zap window; VOD leases outlive a movie
# only if the operator re-asks within the window, which is rare and harmless.
# Overridable: zap-heavy Enigma2 setups on a single MAC can lower this
# (e.g. 45) so a zap never waits out a lease held by the channel it left.
REDIRECT_LEASE_S = float(os.environ.get("SPM_REDIRECT_LEASE_S", "180.0"))


# Second-chance delay when a fresh open finds every route busy or failing.
# Enigma2 zaps fast: the box often asks for the new channel while the panel
# still counts the old one against the MAC's single slot (or while our own
# disconnect watchdog, <=0.5s, is still tearing the old ffmpeg pipe down).
# One delayed retry turns "zap to black" into "zap takes ~3s".
ZAP_RETRY = os.environ.get("SPM_ZAP_RETRY", "1") == "1"
ZAP_RETRY_DELAY = float(os.environ.get("SPM_ZAP_RETRY_DELAY", "2.5"))

#: How long the fallback engine may keep walking MACs and sources before it
#: gives up and lets the caller answer 502.
#:
#: Without a cap the chain time is `candidates x STREAM_START_TIMEOUT x passes`
#: - which for two MACs and the zap retry is 50s, for three 74s - and the output
#: guard (SPM_FIRST_CHUNK_TIMEOUT, 25s) fired long before the engine was done.
#: The player then got a 502 while the engine was still working: the retry pass
#: never ran at all. The guard is a backstop now, this is the real budget.
#: 0 = no cap (the old, unbounded behaviour).
STREAM_START_BUDGET = float(os.environ.get("SPM_STREAM_START_BUDGET", "75"))
#: Slack added to the engine's budget before the output guard calls the pipe
#: dead. Portal handshakes, create_link round trips and killing a stalled ffmpeg
#: all happen outside the STREAM_START_TIMEOUT windows.
START_BUDGET_SLACK = float(os.environ.get("SPM_START_BUDGET_SLACK", "10"))
#: How many per-candidate outcomes to keep on the handle for the final "no data"
#: report. Enough to name every MAC of a normal portal, bounded so a 40-source
#: chain cannot fill the log with one message.
ATTEMPT_TRACE = 6


# --------------------------------------------------------------------------- #
#  zap robustness: our own occupancy is a hint, never a veto
# --------------------------------------------------------------------------- #
# The rule these four constants encode came from the reference implementation
# whose zaps nobody complains about (STB-Proxy): it keeps no session, no link
# and no lock across requests, so a zap can never lose against its own
# bookkeeping - at worst it answers 503 after rotating. SPM keeps state on
# purpose (pooled sessions, per-user quotas, redirect leases), so it has to
# *behave* as if it did not: wait instead of refusing, take back our own
# previous stream, retry the same MAC when the panel is the busy one, and never
# let a candidate that just failed be the first one tried again.
#: How long a start waits for a MAC that our own bookkeeping says is busy.
#: The old shape answered 404 in ~13 ms when every MAC was occupied - and a
#: player that zaps fast (Enigma2 stops the old service as it opens the new
#: one) hits exactly that: our own pipe or lease on the channel it just left.
#:
#: 7 s, not 3: on the real panel measured for this, a slot is still counted for
#: ~6.5 s after the previous connection dies. Waiting less than that buys
#: nothing - the wait is spent and the player still gets 503 - while the user
#: sees a *channel error* on a zap that would have played 3 s later. Waiting
#: longer than the panel needs is free by comparison: `start_budget` still caps
#: the whole start, and the wait ends the moment the slot frees.
BUSY_WAIT_S = float(os.environ.get("SPM_BUSY_WAIT_S", "7.0"))
BUSY_POLL_S = float(os.environ.get("SPM_BUSY_POLL_S", "0.25"))
#: Retry ladder for a panel that answers "this MAC is already streaming"
#: (`limit`, `account_is_in_use`, 456). The panel frees the slot seconds after
#: the previous connection dies, so the same MAC is worth re-asking before
#: walking to a worse one - and on a single-MAC portal it is the only candidate
#: there is. Values are the waits *between* attempts.
BUSY_BACKOFF = tuple(float(x) for x in os.environ.get(
    "SPM_BUSY_BACKOFF", "0.5,1.0,2.0").split(",") if str(x).strip())
#: How many stream starts the diagnostics view keeps (see note_timing).
TIMING_HISTORY = int(os.environ.get("SPM_TIMING_HISTORY", "200"))

#: A candidate that just failed (no data, panel refusal) is pushed behind the
#: ones that did not, for this long - STB-Proxy's `moveMac`, without persisting
#: a global order. Applied AFTER route affinity, so a fresh failure always
#: outranks "this one worked 20 minutes ago".
FAILURE_DEMOTE_S = float(os.environ.get("SPM_FAILURE_DEMOTE_S", "120"))
#: How long a resolved link may be replayed for the next zap (0 = off). Live
#: zapping is the case this exists for: away and back inside the window costs
#: one 302 and no create_link at all. Only the redirect path uses it - a stale
#: token handed to ffmpeg would burn a whole STREAM_START_TIMEOUT before the
#: chain moves on, while the redirect path's liveness probe answers in ~150 ms.
LINK_CACHE_S = float(os.environ.get("SPM_LINK_CACHE_S", "90"))
#: Which kinds may replay a cached link. Live only, deliberately: the cache is
#: the zap-back fast path, and a VOD/episode link is held for a whole movie -
#: nobody re-opens one inside the window, while the redirect path's liveness
#: probe would have to vet it first anyway.
LINK_CACHE_KINDS = tuple(x.strip() for x in os.environ.get(
    "SPM_LINK_CACHE_KINDS", "live").split(",") if x.strip())
#: Default concurrent streams per MAC for portals whose row says nothing
#: (see Portal.streams_per_mac, and STB-Proxy's "streams per mac" setting).
STREAMS_PER_MAC = max(1, int(os.environ.get("SPM_STREAMS_PER_MAC", "1")))


class StreamManager:
    def __init__(self) -> None:
        self.streams: dict[str, StreamHandle] = {}
        # mac_id -> the stream ids of the ffmpeg pipes we own on it. A set, not
        # a single id, because a panel may allow more than one concurrent
        # stream per MAC (Portal.streams_per_mac, default 1 = the historical
        # behaviour). See is_mac_busy.
        self.mac_locks: dict[int, set[str]] = {}
        # mac_id -> how many concurrent pipes this MAC's portal allows. Filled
        # from the chain being walked (_note_chain_limits), because is_mac_busy
        # only ever has a mac_id to work with.
        self.mac_limits: dict[int, int] = {}
        # Soft occupancy for redirect/direct plays: mac_id -> monotonic expiry.
        # See REDIRECT_LEASE_S. Expired entries are dropped lazily on read.
        self.redirect_leases: dict[int, float] = {}
        # Who took the lease (mac_id -> {holder, item, kind, ref, at, seconds}).
        # Needed because a lease is time-bounded, not exact: a quick zap by the
        # *same* user must not be blocked by the channel it just left, while a
        # different user's MAC stays off-limits.
        self.lease_meta: dict[int, dict] = {}
        self._watchers: set[asyncio.Task] = set()           # strong refs, see watch()
        self._proc_gone_since: dict[str, float] = {}        # stream_id -> first seen
        self.route_health = _RouteHealth()
        #: The last TIMING_HISTORY starts (and start failures) with their phase
        #: timings - what the diagnostics view answers "why is zapping slow, and
        #: on which portal" with. Bounded on purpose: a ring of 200 tells a panel
        #: got slow without being a database.
        self.timings: deque[dict] = deque(maxlen=TIMING_HISTORY)

    # ------------------------------------------------------------- occupancy
    def _expire_lease(self, mac_id: int | None) -> None:
        """Drop an expired lease (and its holder record) if it is past its time."""
        if mac_id is None:
            return
        exp = self.redirect_leases.get(mac_id)
        if exp is not None and exp <= time.monotonic():
            self.redirect_leases.pop(mac_id, None)
            self.lease_meta.pop(mac_id, None)

    def lease_holder(self, mac_id: int | None) -> str | None:
        """Which user the current redirect lease belongs to (None when free).

        An anonymous lease (a caller that did not say who it is playing for -
        the admin GUI's quick play, the test doubles) has no holder, so nothing
        may take it over.
        """
        self._expire_lease(mac_id)
        meta = self.lease_meta.get(mac_id or -1) or {}
        return str(meta.get("holder") or "") or None

    def is_mac_busy(self, mac_id: int | None, requester: str | None = None) -> bool:
        """True when a MAC must not be re-handshaked or handed to another play.

        Two independent signals:
          * ``mac_locks`` — an ffmpeg pipe we own (proxy/transcode). Released
            the moment the pump ends or the disconnect watchdog fires.
          * ``redirect_leases`` — we just 302'd a player to the panel CDN with
            this MAC's create_link token. We no longer see the socket, so the
            lease is time-bounded rather than exact.

        ``requester`` is the user asking *now*. A lease the same user took is
        not "someone else's stream": it is the channel that box just zapped
        away from, and holding it against the zap sends the request to a worse
        MAC for no reason (the panel, not this lease, is the authority on
        whether the slot is really free). So the same user may take it over -
        a *different* user never can. Hard ``mac_locks`` (a live ffmpeg pipe)
        are never taken over; those are real concurrent streams.
        """
        if mac_id is None:
            return False
        if len(self._lock_set(mac_id)) >= self._mac_limit(mac_id):
            return True
        self._expire_lease(mac_id)
        if mac_id not in self.redirect_leases:
            return False
        holder = (self.lease_meta.get(mac_id) or {}).get("holder")
        return not (requester and holder and holder == requester)

    def _lock_set(self, mac_id: int | None) -> set[str]:
        """The stream ids holding pipes on this MAC (never shared with anyone).

        A set, not a single id, since a portal may allow several streams per MAC
        (Portal.streams_per_mac). Plain-string values are accepted and upgraded
        on the spot: the attribute is poked directly by tools and stand-ins that
        predate the set, and silently dropping them would report a MAC as free
        while an ffmpeg pipe is streaming on it.
        """
        held = self.mac_locks.get(mac_id) if mac_id is not None else None
        if held is None:
            return set()
        if isinstance(held, str):                 # legacy single-id assignment
            held = {held}
            self.mac_locks[mac_id] = held
        return held

    def _mac_limit(self, mac_id: int | None) -> int:
        """How many concurrent pipes this MAC allows (>= 1)."""
        if mac_id is None:
            return STREAMS_PER_MAC
        return max(1, int(self.mac_limits.get(mac_id, STREAMS_PER_MAC) or 1))

    def note_mac_limit(self, mac_id: int | None, limit: int | None) -> None:
        """Remember the concurrency limit of one MAC (per-portal setting)."""
        if mac_id is None:
            return
        self.mac_limits[mac_id] = max(1, int(limit or STREAMS_PER_MAC))

    def lock_mac(self, mac_id: int | None, stream_id: str) -> None:
        """Take one pipe slot on a MAC (see is_mac_busy for the limit)."""
        if mac_id is None:
            return
        self.mac_locks.setdefault(mac_id, set()).add(stream_id)

    def unlock_mac(self, mac_id: int | None, stream_id: str) -> None:
        """Release one pipe slot; the MAC is free again when the set empties."""
        if mac_id is None:
            return
        held = self._lock_set(mac_id)
        if not held:
            return
        held.discard(stream_id)
        if not held:
            self.mac_locks.pop(mac_id, None)

    async def preempt_own(self, mac_id: int | None, requester: str | None) -> bool:
        """Kill a pipe of the SAME user on this MAC - a zap, not a conflict.

        The redirect path has allowed exactly this since the lease existed (the
        channel a box just zapped away from is not "someone else's stream"). A
        live ffmpeg pipe used to be untouchable instead, which is why changing
        channel could answer 404 while the player was still tearing the old
        socket down - the one thing that made a .ts -> .ts zap fail where a
        redirect zap worked. Same rule as the lease: only the same user, never
        another, and never an anonymous stream.
        """
        if mac_id is None or not requester:
            return False
        killed = False
        for sid in list(self._lock_set(mac_id)):
            h = self.streams.get(sid)
            if h is None or h.user_name != requester:
                continue
            await db_log("INFO", "stream",
                         f"[{h.item_name}] taking over the ffmpeg pipe on this MAC "
                         f"held by {requester} (the channel this zap left)")
            await self.kill(sid)
            killed = True
        return killed

    # ------------------------------------------------- parking (see LINGER_S)
    def _lock_of(self, h: StreamHandle) -> int | None:
        """The MAC this handle holds, if any."""
        for mac_id, sids in self.mac_locks.items():
            if h.id in self._lock_set(mac_id):
                return mac_id
        return None

    def _can_park(self, h: StreamHandle) -> bool:
        """May this pipe be held open for a client that just left?

        Only a live stream that actually played (bytes flowed - parking a pipe
        that never started would hold a panel slot for nothing), still running,
        and not deliberately killed (a zap takeover, the dashboard, the
        reaper all set `dead`).
        """
        return (LINGER_S > 0 and not h.parked and not h.dead
                and h.kind in LINGER_KINDS and h.bytes_sent > 0
                and h.proc is not None and h.proc.returncode is None)

    async def client_left(self, h: StreamHandle) -> None:
        """The player's socket is gone - hold the pipe briefly, or kill it.

        Both the disconnect watchdog and the pump's own teardown reach this for
        the same disconnect; whichever arrives second must not undo the
        decision - a parked pipe is never killed here.
        """
        if h.parked:
            return
        if self._can_park(h):
            await self._park(h)
            return
        await self.kill(h.id)

    async def _park(self, h: StreamHandle) -> None:
        """Keep the pipe alive (and draining) for LINGER_S."""
        if h.parked or not self._can_park(h):
            return
        h.parked = True
        h.parker = asyncio.get_running_loop().create_task(
            self._park_proc(h, h.proc), name=f"park-{h.id[:8]}")
        self._watchers.add(h.parker)
        h.parker.add_done_callback(self._watchers.discard)
        # The dashboard shows what somebody is watching, and nobody is: drop the
        # runtime row while the pipe is only being held (`_adopt` puts it back).
        try:
            await run_uncancelled(self._delete_row(h.id), what="parked row delete")
        except Exception:  # noqa: BLE001
            log.exception("active_streams delete (park) failed")
        await db_log("INFO", "stream",
                     f"[{h.item_name}] client left -> holding the pipe for "
                     f"{LINGER_S:.0f}s so a zap back is instant "
                     f"({h.bytes_sent / 1e6:.1f} MB so far)")

    async def _park_proc(self, h: StreamHandle, proc) -> None:
        """Drain a parked pipe into `ring` until it is wanted, dies, or expires.

        Draining matters: ffmpeg blocks on a full stdout pipe, and a blocked
        ffmpeg would stall the panel's stream - the parked pipe has to keep
        consuming for the attach to be worth anything.
        """
        deadline = time.monotonic() + LINGER_S
        cap = max(1, LINGER_BUFFER_KB) * 1024
        try:
            while h.parked and not h.dead and time.monotonic() < deadline:
                left = max(0.05, min(1.0, deadline - time.monotonic()))
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(CHUNK), left)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                ring = h.ring
                ring += chunk
                if len(ring) > cap:
                    del ring[:len(ring) - cap]
        except asyncio.CancelledError:
            # A returning client took the pipe over (see `_adopt`): the
            # response generator owns the process from here on.
            raise
        except Exception:  # noqa: BLE001 - never let the parker die silently
            log.exception("parked stream %s failed", h.id)
        if not h.parked:
            return                       # attached while we were reading
        h.parked = False
        h.parker = None
        gone = proc.returncode is not None
        await self._kill_quiet(proc)
        await self._deregister(h)
        await db_log("INFO", "stream",
                     f"[{h.item_name}] parked stream expired after {LINGER_S:.0f}s"
                     + (" (the source had ended)" if gone else "")
                     + " -> releasing the MAC")

    def _find_parked(self, kind: str, ref_id: int,
                     user_name: str | None) -> StreamHandle | None:
        """A still-running pipe of this user + item (the zap-back case)."""
        if LINGER_S <= 0 or kind not in LINGER_KINDS:
            return None
        key = (kind, ref_id)
        who = user_name or "-"
        for h in self.streams.values():
            if (h.parked and h.route_key == key and (h.user_name or "-") == who
                    and h.proc is not None and h.proc.returncode is None):
                return h
        return None

    async def _adopt(self, h: StreamHandle) -> None:
        """Stop parking so the caller's generator can read the pipe itself."""
        h.parked = False
        parker, h.parker = h.parker, None
        if parker is not None and parker is not asyncio.current_task():
            parker.cancel()
            try:
                await parker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        h.reattaches += 1
        try:
            await run_uncancelled(self._insert_row(h), what="parked row re-insert")
        except Exception:  # noqa: BLE001 - the stream works; only the row is late
            log.exception("active_streams re-insert (attach) failed")

    async def _drop_parked(self, chain: list) -> bool:
        """Kill parked pipes that stand in the way of somebody who wants to play.

        Parking is a courtesy, never a reservation: the pipe was held for a
        client that might come back, and it must never make another request wait
        (or answer 503) while it idles.
        """
        mac_ids = {m.id for (_s, _p, macs) in chain for m in (macs or ())}
        dropped = False
        for mac_id in mac_ids:
            for sid in list(self._lock_set(mac_id)):
                h = self.streams.get(sid)
                if h is not None and h.parked:
                    await db_log("INFO", "stream",
                                 f"[{h.item_name}] giving up the parked pipe on "
                                 f"{h.mac or mac_id} - somebody wants to play")
                    await self.kill(sid)
                    dropped = True
        return dropped

    async def wait_for_mac(self, mac_row, requester: str | None,
                           budget: float | None = None) -> bool:
        """Wait until a MAC can be used, preempting our own stream if needed.

        Returns False when the MAC stays unusable inside the budget - which the
        caller answers with "next candidate", not with an immediate 404 (see
        BUSY_WAIT_S). A lease held by *another* user is not worth waiting for:
        it lasts REDIRECT_LEASE_S, not seconds, so the caller should move on.
        """
        if mac_row is None:
            return True
        if not self.is_mac_busy(mac_row.id, requester=requester):
            return True
        budget = BUSY_WAIT_S if budget is None else max(0.0, budget)
        deadline = time.monotonic() + budget
        while True:
            if await self.preempt_own(mac_row.id, requester):
                if not self.is_mac_busy(mac_row.id, requester=requester):
                    return True
            if self.lease_remaining(mac_row.id) > BUSY_POLL_S:
                return False                     # another user's lease: minutes
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(BUSY_POLL_S, max(0.0, deadline - time.monotonic())))

    def lease_mac(self, mac_id: int | None, *, seconds: float = REDIRECT_LEASE_S,
                  holder: str | None = None, item: str = "", kind: str = "",
                  ref: int | None = None) -> None:
        """Mark a MAC busy for a short window after a redirect/direct resolve."""
        if mac_id is None:
            return
        window = max(1.0, float(seconds))
        self.redirect_leases[mac_id] = time.monotonic() + window
        self.lease_meta[mac_id] = {"holder": holder or None, "item": item or "",
                                   "kind": kind or "", "ref": ref,
                                   "seconds": round(window, 1),
                                   "at": time.time()}

    def lease_remaining(self, mac_id: int | None) -> float:
        """Seconds left on a redirect lease (0.0 when free)."""
        self._expire_lease(mac_id)
        exp = self.redirect_leases.get(mac_id)
        return max(0.0, exp - time.monotonic()) if exp is not None else 0.0

    def release_mac(self, mac_id: int | None) -> None:
        """Drop every occupancy record for one MAC (delete/edit cleanup)."""
        if mac_id is None:
            return
        self.mac_locks.pop(mac_id, None)
        self.redirect_leases.pop(mac_id, None)
        self.lease_meta.pop(mac_id, None)

    def release_macs(self, mac_ids) -> None:
        for mid in mac_ids or ():
            self.release_mac(mid)

    def busy_mac_ids(self) -> set[int]:
        """mac_ids with an ffmpeg pipe or a live redirect lease (any count).

        Deliberately conservative even when a MAC allows several streams: the
        callers are health probes and playlist checks, and handing a MAC to a
        *probe* while a viewer is on it is what kicks the viewer. The play path
        uses is_mac_busy, which honours the per-portal limit.
        """
        for mid in list(self.redirect_leases):
            self._expire_lease(mid)
        return {mid for mid in self.mac_locks if self._lock_set(mid)} | set(self.redirect_leases)

    def mac_occupancy(self, mac_id: int | None) -> dict | None:
        """Why a MAC is (not) usable right now, for the GUI and the logs.

        Deliberately side-effect free: it neither creates nor extends anything,
        so asking the question can never keep a lease alive. Expired leases are
        reported as free, and the caller is told which kind of busy this is -

          ``pipe``  an ffmpeg pipe we own (a real stream, right now)
          ``lease`` a post-302 redirect lease: the player is on the panel's CDN,
                     so this is "probably still watching", with N seconds left
        """
        if mac_id is None:
            return None
        holder = self.lease_holder(mac_id)
        stream_ids = sorted(self._lock_set(mac_id))
        if stream_ids and len(stream_ids) >= self._mac_limit(mac_id):
            h = self.streams.get(stream_ids[0])
            return {"busy": True, "reason": "pipe", "stream_id": stream_ids[0],
                    "streams": len(stream_ids),
                    "holder": (h.user_name if h else None),
                    "item": (h.item_name if h else ""),
                    "remaining_s": 0.0}
        remaining = self.lease_remaining(mac_id)
        if remaining <= 0:
            return {"busy": False, "reason": "free", "remaining_s": 0.0}
        meta = self.lease_meta.get(mac_id) or {}
        return {"busy": True, "reason": "lease", "remaining_s": round(remaining, 1),
                "holder": holder, "item": meta.get("item") or "",
                "kind": meta.get("kind") or "", "ref": meta.get("ref"),
                "seconds": meta.get("seconds")}

    def occupancy_map(self) -> dict[int, dict]:
        """Every MAC with something to say right now (busy ones only)."""
        out: dict[int, dict] = {}
        for mid in self.busy_mac_ids():
            info = self.mac_occupancy(mid)
            if info and info.get("busy"):
                out[int(mid)] = info
        return out

    def busy_mac_addresses(self) -> set[str]:
        """Uppercased MAC strings currently occupied (for health-skip lookups).

        Prefers the live StreamHandle.mac (ffmpeg path, always accurate while
        bytes flow). Falls back to nothing for leases alone — those are keyed
        by id; callers that only have a MAC string should pass through
        ``busy_mac_ids`` + the row id instead. Health refresh has the row, so
        it uses both.
        """
        out: set[str] = set()
        for h in self.streams.values():
            mac = (h.mac or "").strip().upper()
            if mac:
                out.add(mac)
        return out

    # ------------------------------------------------------------- registry
    def list(self) -> list[dict]:
        return [h.public() for h in self.streams.values()]

    def user_stream_count(self, username: str | None) -> int:
        """
        Open streams for a user, derived from the live registry.

        This used to be a separate counter incremented in `_register` and
        decremented in `_deregister`. Every missed decrement (a generator parked
        at `yield` that never got finalised, a watchdog task that was garbage
        collected) permanently consumed one of the user's slots, so after a
        while every play request answered "exceeded max_connections" until the
        container was restarted. A derived count cannot drift.
        """
        key = username or "-"
        # A parked pipe does not count: it exists only so this same user can
        # come back to it, and their next request either attaches to it or makes
        # it give way (see LINGER_S). Counting it would answer "max connections
        # reached" to the very request the parking exists for.
        return sum(1 for h in self.streams.values()
                   if (h.user_name or "-") == key and not h.parked)

    def can_open_for(self, username: str | None, max_conn: int | None) -> bool:
        if max_conn is None or max_conn <= 0:
            return True
        return self.user_stream_count(username) < max_conn

    @staticmethod
    async def _insert_row(h: StreamHandle) -> None:
        async with SessionLocal() as s:
            s.add(ActiveStream(id=h.id, kind=h.kind, item_name=h.item_name,
                               user_name=h.user_name, portal_name=h.portal_name or None,
                               mac=h.mac or None, template_name=h.template_name,
                               pid=h.proc.pid if h.proc else None))
            await s.commit()

    @staticmethod
    async def _delete_row(stream_id: str) -> None:
        async with SessionLocal() as s:
            await s.execute(delete(ActiveStream).where(ActiveStream.id == stream_id))
            await s.commit()

    async def _register(self, h: StreamHandle) -> None:
        self.streams[h.id] = h
        try:
            await run_uncancelled(self._insert_row(h), what="active_streams insert")
        except Exception:  # noqa: BLE001
            log.exception("active_streams insert failed")

    async def _deregister(self, h: StreamHandle) -> None:
        # In-memory state first, DB second: the connection slot and the MAC
        # lock must be free the moment we know the stream is gone, even when
        # the database is slow (a stalled commit must never keep a user at
        # "max connections reached" or a MAC locked).
        h.dead = True
        self.streams.pop(h.id, None)
        self._proc_gone_since.pop(h.id, None)
        for mac_id in list(self.mac_locks):
            sids = self._lock_set(mac_id)
            sids.discard(h.id)
            if not sids:
                del self.mac_locks[mac_id]
        # The DELETE is what removes the dashboard row, and deregistration
        # normally runs from the pump's finally - i.e. inside the request task
        # that is being cancelled right now. Left unshielded it dies halfway,
        # SQLAlchemy drops the pooled connection, and the row stays behind as a
        # ghost until the next container start.
        try:
            await run_uncancelled(self._delete_row(h.id), what="active_streams delete")
        except Exception:  # noqa: BLE001
            pass

    async def kill(self, stream_id: str) -> bool:
        h = self.streams.get(stream_id)
        if not h:
            return False
        if h.dead:
            return True
        h.dead = True
        if h.parker is not None and not h.parker.done():
            # A parked pipe is drained by its own task; killing the process is
            # enough for it to end, but cancelling it now frees the buffer and
            # the watcher slot deterministically.
            h.parker.cancel()
            h.parker = None
            h.parked = False          # it is not being held any more, it is gone
        if h.proc and h.proc.returncode is None:
            try:
                h.proc.kill()
            except ProcessLookupError:  # noqa: PERF203
                pass
        # Deregister HERE, not only in the pump's finally: the pump generator
        # is pull-based and may be parked at a `yield` forever once the client
        # vanished, so only an actively-running task (watchdog/API) can
        # release registry + MAC locks deterministically. `_deregister` is
        # idempotent, the pump's finally will simply no-op afterwards.
        # Release BEFORE logging: the log write goes to the database, and a
        # slow database must not delay freeing the user's connection slot.
        await self._deregister(h)
        await db_log("WARNING", "stream", f"stream '{h.item_name}' killed (user/disconnect)")
        return True

    async def kill_all(self) -> int:
        n = len(self.streams)
        for sid in list(self.streams):
            await self.kill(sid)
        return n

    async def sweep_orphans(self, where: str = "boot") -> int:
        """Kill ffmpeg pipes this process did not spawn (see `_orphan_ffmpeg_pids`).

        Called at boot (a previous run may have died hard - SIGKILL, OOM, a
        container restart - and its pipes hold panel slots that nothing in the
        GUI can release) and at shutdown (kill them ourselves instead of leaving
        them for the next boot to clean up).
        """
        import signal as _signal

        pids = _orphan_ffmpeg_pids()
        killed: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, _signal.SIGKILL)
                killed.append(pid)
            except OSError:
                continue
        if killed:
            await db_log("WARNING", "stream",
                         f"{len(killed)} orphaned ffmpeg pipe(s) killed at {where} - "
                         f"they would have kept their panel slot counted "
                         f"(pids {', '.join(str(p) for p in killed)})")
        return len(killed)

    def watch(self, request, handle: StreamHandle) -> asyncio.Task:
        """
        Start the disconnect watchdog and KEEP A REFERENCE to it.

        `asyncio.create_task()` only leaves a weak reference behind: a task
        nobody holds can be garbage-collected mid-flight and stop silently.
        That is exactly what the watchdog needs to survive, because it is the
        thing that releases the MAC lock and the user's connection slot when a
        player disappears without a clean socket close.
        """
        task = asyncio.get_running_loop().create_task(
            self.watch_disconnect(request, handle),
            name=f"watch-{handle.id[:8]}")
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)
        return task

    async def reap_dead(self, interval: float = 10.0) -> None:
        """
        Safety net for lost teardowns.

        The pump's `finally` and the watchdog cover the normal paths, but a
        generator can be parked at `yield` and never finalised (no `aclose()`
        from Starlette, client behind a buffering proxy). Such a handle stays in
        the registry forever and permanently occupies a MAC and a connection
        slot. Anything whose ffmpeg process is gone and stays gone for
        REAP_GRACE without producing bytes is finished by definition, so free
        it here.
        """
        while True:
            try:
                await asyncio.sleep(interval)
                now = time.time()
                for h in list(self.streams.values()):
                    gone = h.dead or (h.proc is not None and h.proc.returncode is not None)
                    if not gone:
                        self._proc_gone_since.pop(h.id, None)
                        continue
                    since = self._proc_gone_since.setdefault(h.id, now)
                    if now - since < REAP_GRACE:
                        continue
                    self._proc_gone_since.pop(h.id, None)
                    await db_log("WARNING", "stream",
                                 f"[{h.item_name}] teardown was lost (process gone "
                                 f">{REAP_GRACE:.0f}s) -> freeing slot/MAC")
                    await self._deregister(h)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the reaper must never die
                log.exception("reaper sweep failed")

    async def watch_disconnect(self, request, handle: StreamHandle,
                               interval: float = 0.5) -> None:
        """
        Client-disconnect watchdog. Runs from response-creation time on: the
        stream registers itself inside the pump only once data actually flows,
        so we first wait for registration and only then treat "not registered /
        dead" as 'finished'. On http.disconnect we kill the stream so the MAC
        lock and ffmpeg process are released deterministically even when the
        socket close is not propagated to sends (buffers, proxies).
        """
        try:
            registered = False
            while True:
                if handle.dead:                                # killed/failed -> done
                    return
                if handle.id in self.streams:
                    registered = True
                elif registered:                               # deregistered -> normal end
                    return
                if registered and await request.is_disconnected():
                    # `client_left`, not `kill`: a live pipe that played is held
                    # for LINGER_S first so a zap back attaches to it (see
                    # LINGER_S). Everything else is killed as before.
                    await self.client_left(handle)
                    return
                await asyncio.sleep(interval)
        except Exception:  # noqa: BLE001 - watchdog must never crash the app
            log.exception("disconnect watchdog crashed for %s", handle.id)

    # --------------------------------------------------------- ffmpeg spawn
    @staticmethod
    def _network_input_options(cmd_text: str, url: str,
                               user_agent: str | None = None) -> str:
        """
        Give ffmpeg the identity of the STB it is impersonating, and the input
        options the resolved link itself requires.

        Identity: ffmpeg announces itself as "Lavf/61.x" and sends no Referer;
        plenty of Stalker panels - and the CDNs in front of them - answer that
        with 403 or 405 ("Method Not Allowed") on an otherwise perfectly valid
        link. The default is the UA of the MAG box's embedded media *player*
        (Lavf53.32.100, see app/services/stream_identity.py): injecting the
        portal BROWSER UA here makes play/live.php-style origins answer the
        media endpoint with HTTP 456 ("unrecoverable") and zero bytes, which
        reads as "redirect/direct works but every ffmpeg template fails". The
        pump walks the player->browser UA ladder (stream_identity) and passes
        the value to try; every other caller gets the faithful player UA.

        Per-input options: an HLS playlist additionally needs its segment
        protocols whitelisted or ffmpeg refuses to open it at all (see
        ffmpeg_templates.HLS_INPUT_OPTS). A user who wrote their own
        -protocol_whitelist/-user_agent/-referer/-headers into the template is
        never overridden.
        """
        if not url.lower().startswith(_NET_SCHEMES):
            return cmd_text
        add: list[str] = []
        if "-user_agent" not in cmd_text:
            add.append(f'-user_agent "{user_agent or stream_identity.PLAYER_UA}"')
        if "-referer" not in cmd_text and "-headers" not in cmd_text:
            origin = url.split("://", 1)[-1].split("/", 1)[0]
            add.append(f'-referer "{url.split("://", 1)[0]}://{origin}/"')
        if is_hls(url):                                 # two independent flags
            if "-protocol_whitelist" not in cmd_text:
                add.append(f'-protocol_whitelist "{HLS_PROTOCOL_WHITELIST}"')
            if "-allowed_extensions" not in cmd_text:
                add.append(f"-allowed_extensions {HLS_ALLOWED_EXTENSIONS}")
        if not add:
            return cmd_text
        # options belong directly in front of the input they apply to
        return re.sub(rf"\s-i\s+{re.escape(url)}",
                      lambda m: " " + " ".join(add) + m.group(0),
                      cmd_text, count=1)

    async def _first_bytes(self, proc, timeout: float | None = None) -> bytes:
        """
        Wait for the first chunk - but only until ffmpeg dies, not until the
        start timeout expires. A process that exits before sending a byte will
        never send one, so falling back immediately is both faster (no 12 s
        wait per dead source) and honest in the log.

        `timeout` is the per-candidate window (see STREAM_START_TIMEOUT_REST):
        the caller spends the full window on the source it believes in and much
        less on the ones after it.
        """
        read_t = asyncio.ensure_future(proc.stdout.read(CHUNK))
        exit_t = asyncio.ensure_future(proc.wait())
        try:
            done, _pending = await asyncio.wait(
                {read_t, exit_t},
                timeout=STREAM_START_TIMEOUT if timeout is None else timeout,
                return_when=asyncio.FIRST_COMPLETED)
        except Exception:  # noqa: BLE001 - never let the wait break the pump
            done = set()
        if read_t in done:
            try:
                data = read_t.result() or b""
            except Exception:  # noqa: BLE001
                data = b""
            if data:
                exit_t.cancel()
                return data
            # EOF without a single byte: ffmpeg is gone. Give the exit waiter
            # a moment so the log can name the real reason (rc=8 -> HTTP 405,
            # rc=1 -> bad template, ...) instead of a bogus "no data within 12s".
            try:
                await asyncio.wait_for(exit_t, 1.0)
            except Exception:  # noqa: BLE001
                exit_t.cancel()
            return b""
        read_t.cancel()
        exit_t.cancel()
        return b""

    @staticmethod
    def _strip_subs(args: list[str]) -> list[str]:
        """
        Remove every subtitle OUTPUT mechanism from argv:

          * `-map` specs that address a subtitle stream (`:s`)
          * `-c:s` / `-scodec` pairs
          * the `subtitles=` burn filter inside a -vf value (burn-in is gone:
            it needs CPU video frames, and hardware-only transcoding is the
            supported path - a legacy command that still carries the filter
            gets it stripped here instead of dying inside the filtergraph)

        Video/audio maps and the rest of the filter chain are untouched;
        segments split on unescaped commas, so filter-escaped separators
        ('\,') inside the remaining filters survive intact.
        """
        out: list[str] = []
        i = 0
        while i < len(args):
            t = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if t == "-map" and nxt is not None and ":s" in nxt:
                i += 2                     # subtitle map -> dropped
                continue
            if t in ("-c:s", "-scodec") and nxt is not None:
                i += 2                     # subtitle codec -> dropped
                continue
            if t == "-vf" and nxt is not None and "subtitles=" in nxt:
                segs = re.split(r"(?<!\\),", nxt)
                kept = [s for s in segs if s.strip() and not s.strip().startswith("subtitles=")]
                if kept:
                    out += ["-vf", ",".join(kept)]
                i += 2
                continue
            out.append(t)
            i += 1
        return out

    @staticmethod
    def _ensure_sn(args: list[str]) -> list[str]:
        """Pin -sn into the OUTPUT section so ffmpeg's default stream
        selection cannot auto-pick a subtitle stream either - an
        unmapped-but-auto-selected subrip track dies at the mpegts muxer just
        the same."""
        if "-sn" in args:
            return args
        out = list(args)
        try:
            last_i = max(idx for idx, a in enumerate(out) if a == "-i")
            f_idx = out.index("-f", last_i) if "-f" in out[last_i:] else len(out) - 1
        except ValueError:
            f_idx = len(out) - 1
        out.insert(f_idx, "-sn")
        return out

    @staticmethod
    def _drop_subtitles(args: list[str]) -> list[str]:
        """
        Enforce 'the TS/HLS pipe carries no subtitles' at spawn time.

        build_command() renders `-sn` for this reason (see there for the full
        story: text->bitmap and text->mpegts both abort ffmpeg before the
        first output byte, which killed every VOD/local transcode of a movie
        with an SRT/ASS/PGS track). This net does the same for command TEXT
        stored before that change: templates written for live MPEG-TS used to
        carry `-map 0:s?` + `-c:s dvbsub`, and a user who pasted a sub-mapping
        command from elsewhere deserves the same protection. Explicitly mapped
        video/audio streams are untouched; whole-program maps like `-map 0`
        are the user's own statement and are left alone.
        """
        return StreamManager._ensure_sn(StreamManager._strip_subs(args))

    @staticmethod
    def _has_sub_map(cmd_template: str) -> bool:
        """True when the command's OUTPUT side maps subtitle streams (dvb
        intent). Tolerant tokenising: a half-typed command must not raise."""
        try:
            toks = shlex.split(cmd_template or "")
        except ValueError:
            toks = (cmd_template or "").split()
        output_side = False
        for i, t in enumerate(toks):
            if t == "-i":
                output_side = True
                continue
            if not output_side:
                continue
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if (t == "-map" and nxt is not None and ":s" in nxt) \
                    or (t in ("-c:s", "-scodec") and nxt is not None):
                return True
        return False

    @staticmethod
    def _ffmpeg_argv(cmd_template: str, url: str, title: str | None = None,
                     pace: bool = False, user_agent: str | None = None) -> list[str] | None:
        """Render a template + input into an argv list, or None if unusable.

        Local paths are quoted so `shlex.split` keeps spaces/quotes as one
        `-i` argument, and network-only flags are stripped so ffmpeg does not
        abort with "Option reconnect not found" on a file.
        
        If title is provided, injects -metadata title=... into the output so
        players (VLC, etc.) display the correct stream name instead of whatever
        metadata the source stream contains.

        pace=True (VOD/episode/local: the input is a FILE that ffmpeg would
        otherwise read as fast as it can transcode) inserts `-re` so the file
        streams at its own frame rate - without it a 2-hour movie is pushed
        through the pipe at encode speed, the player's buffer fills, ffmpeg
        hits EOF long before the viewer reaches the end and the stream just
        stops mid-playback. Live inputs are already paced by the encoder on
        the other end and must NOT be throttled.

        A dvb subtitle intent (a command text that maps subtitle streams) is
        NOT flattened here: it is checked against the real source by the
        async _subs_gate at spawn time, which alone knows whether the
        file/link actually carries a convertible (bitmap) track. Everything
        else gets the plain "no subtitles" net (which also strips a legacy
        subtitles= burn filter from the -vf value).
        """
        if (cmd_template or "").strip() == REDIRECT_COMMAND:
            return None
        is_net = url.lower().startswith(_NET_SCHEMES)
        insert = url if is_net else shlex.quote(url)
        cmd_text = StreamManager._network_input_options(
            cmd_template.replace(URL_PLACEHOLDER, insert), url,
            user_agent=user_agent)
        if not is_net:
            cmd_text = _NETONLY_OPTS.sub(" ", cmd_text)
        if cmd_text.startswith("ffmpeg"):
            cmd_text = FFMPEG_BIN + cmd_text[len("ffmpeg"):]
        if "<out_dir>" in cmd_text:
            return None
        try:
            args = shlex.split(cmd_text)
        except ValueError:
            return None
        if not args:
            return None
        if not StreamManager._has_sub_map(cmd_template):
            args = StreamManager._drop_subtitles(args)
        if pace and not ({"-re", "-readrate", "-readrate_initial"} & set(args)):
            try:
                args.insert(args.index("-i"), "-re")   # input option: before -i
            except ValueError:
                pass
        args = StreamManager._ensure_annexb(args)
        args = StreamManager._ensure_interleave_flush(args)
        # Live Matroska is audio-only on Enigma2 (no cues on a pipe). A VOD
        # MKV template assigned to a live channel is rewritten to MPEG-TS.
        if not pace and is_net:
            # `pace=False` is normally live, but callers also use the pure
            # argv renderer for local-file diagnostics. Only a network/live
            # input should have its Matroska pipe rewritten for Enigma2.
            args = StreamManager._matroska_to_mpegts_for_live(args)
            args = StreamManager._ensure_annexb(args)
            args = StreamManager._ensure_interleave_flush(args)
        # Inject metadata title before the output format specifier so players
        # display the correct stream name instead of source stream metadata.
        # One argv element (create_subprocess_exec); a title with spaces or
        # a minus must not split into extra flags.
        if title:
            safe = (title or "").replace("\r", " ").replace("\n", " ").replace("\0", "")
            try:
                last_i = max(idx for idx, a in enumerate(args) if a == "-i")
                f_idx = args.index("-f", last_i)
                args[f_idx:f_idx] = ["-metadata", f"title={safe}"]
            except (ValueError, IndexError):
                if len(args) > 1:
                    args[-1:-1] = ["-metadata", f"title={safe}"]
        return args

    @staticmethod
    def _pref_lang_hit(raw) -> str | None:
        """The normalized tag when `raw` is Dutch or English, else None.

        'en-GB' / 'nl_NL' lose their region first, so a Matroska muxed with
        BCP47 tags answers exactly like one tagged 'eng' / 'dut'."""
        norm = (str(raw or "")).strip().lower().split("-")[0].split("_")[0]
        return norm if norm in MKV_PREF_SUB_LANGS else None

    async def _mkv_lang_verdict(self, total: int, kept: list[dict], tag: str) -> None:
        """For a VOD with MULTIPLE subtitle tracks, record whether Dutch (nl)
        or English is among what the Matroska output actually carries.

        `-map 0:s?` copies whatever the source has, so "nl/en is in the
        output" is exactly "nl/en is tagged on one of the KEPT tracks" (the
        codec gate may have routed some tracks out). Scoped to total > 1 as
        asked: with a single track there is no menu to choose from. Probe
        failures never reach here — the caller returns on None/[] first.
        """
        if total <= 1:
            return
        langs = [(s.get("lang") or "").strip().lower() for s in kept]
        unknown = sum(1 for l in langs if not l)
        known = sorted({l for l in langs if l})
        hits = sorted({h for l in known if (h := self._pref_lang_hit(l))})
        if hits:
            await db_log("INFO", "stream", tag +
                         f"Matroska output includes Dutch/English subtitle(s) "
                         f"({', '.join(hits)} of {total} track(s))")
        elif not kept:
            await db_log("WARNING", "stream", tag +
                         f"all {total} subtitle track(s) dropped — no Dutch (nl) "
                         f"or English subtitle in the Matroska output")
        elif known:
            tail = f", {unknown} untagged" if unknown else ""
            await db_log("WARNING", "stream", tag +
                         f"no Dutch (nl) or English subtitle in the Matroska "
                         f"output ({', '.join(known)}{tail} of {total} track(s))")
        else:
            await db_log("INFO", "stream", tag +
                         f"{total} subtitle track(s) copied to Matroska, "
                         f"language(s) untagged — cannot verify nl/en")

    async def _subs_gate(self, args: list[str], url: str, pace: bool,
                         name: str = "") -> list[str]:
        """
        Per-source reality check for DVB and Matroska subtitle intents.

        Matroska network inputs use cached metadata only: a preflight must
        not spend the portal's playback token. Local Matroska files can be
        probed, and unknown metadata leaves the optional copy maps intact.

        For DVB, the template says what it WANTS; whether the source can
        deliver it is a property of the file/link being played. The gate
        probes once (8 s cap, 10 min cache without the per-play token) and
        degrades to the safe default - no subtitle track in the pipe - instead
        of letting ffmpeg abort before its first output byte. Live inputs
        (pace=False) are trusted instead of probed: zapping must stay instant,
        and live MPEG-TS carries DVB bitmap subs natively - the only kind the
        dvb mode can keep anyway.

        dvb is hardware-safe: the subtitle track is demuxed and re-encoded
        independently of the video, so the VAAPI/QSV pipeline
        (hw decode -> scale_vaapi/qsv -> hw encode) never touches it.
        """
        tag = f"[{name}] " if name else ""
        is_net = url.lower().startswith(_NET_SCHEMES)
        mapped = "-c:s" in args or "-scodec" in args
        if not mapped:
            return args

        # ---- keep: MATROSKA output, every subtitle track copied ------------
        # Nothing to degrade here - mkv holds SRT/ASS/PGS/DVB alike - except
        # the handful of codecs the muxer refuses (teletext & closed captions).
        # `-map 0:s?` is optional, so a source without subtitles is fine and a
        # failed probe changes nothing: it is only used to route AROUND a
        # codec that would abort the muxer.
        if StreamManager._outputs_matroska(args):
            if not pace:                   # live: trusted, no probe (zap speed)
                return args
            # Do not open a portal's playback URL twice. Even a short probe
            # can spend a one-use token or leave the provider's only stream
            # slot occupied when the real player opens it. Language reporting
            # must never require an extra connection before the first byte.
            # Cached codec information still lets us avoid unsupported tracks;
            # without it, preserve the optional copy-all mapping (as we do on
            # probe failure). Local files can safely be probed as before.
            subs = (await subtitle_streams(url, is_url=True, cached_only=True)
                    if is_net else await subtitle_streams(url, is_url=False))
            if not subs:                   # unknown metadata or no subtitles
                return args
            bad = [s["codec"] for s in subs if s["codec"] in _MKV_UNSUPPORTED_SUB_CODECS]
            if not bad:
                # every track lands in the output — answer the nl/en question
                await self._mkv_lang_verdict(len(subs), subs, tag)
                return args
            idxs = [n for n, s in enumerate(subs)
                    if s["codec"] not in _MKV_UNSUPPORTED_SUB_CODECS][:16]
            if not idxs:
                await db_log("INFO", "stream", tag +
                             "no Matroska-compatible subtitle track ("
                             + ", ".join(bad) + ") -> subtitles dropped")
                await self._mkv_lang_verdict(len(subs), [], tag)
                return StreamManager._ensure_sn(StreamManager._strip_subs(args))
            await db_log("INFO", "stream", tag +
                         "subtitles copied to Matroska, skipping "
                         + ", ".join(bad))
            await self._mkv_lang_verdict(len(subs), [subs[n] for n in idxs], tag)
            return StreamManager._remap_subs(args, idxs, "copy")

        # ---- dvb / copy: keep BITMAP subtitles as a track in the output TS --
        if not pace:                       # live: opt-in trusted, no probe
            return args
        subs = await subtitle_streams(url, is_url=is_net)
        if subs is None:
            await db_log("INFO", "stream", tag +
                         "subtitle probe failed -> subtitles dropped (safe default)")
            return StreamManager._ensure_sn(StreamManager._strip_subs(args))
        # ffmpeg's `0:s:N` addresses the Nth SUBTITLE stream, not the Nth
        # stream of the input: the map needs the ordinal among the subtitle
        # tracks, not the absolute stream index the probe reports.
        idxs = [n for n, s in enumerate(subs) if s["codec"] in _TS_NATIVE_SUB_CODECS][:8]
        if not idxs:
            extra = ", ".join(s["codec"] for s in subs) if subs else "none"
            await db_log("INFO", "stream", tag +
                         "no native DVB subtitle track ("
                         + extra + ") -> subtitles dropped "
                         "(PGS/DVD->dvbsub would stall the first byte)")
            return StreamManager._ensure_sn(StreamManager._strip_subs(args))
        # rebuild: explicit maps for the bitmap tracks only, then the codec.
        # 'copy' is honoured only when every source track is already DVB
        # (mpegts cannot carry raw PGS/DVD) - anything else re-encodes.
        codec_tok = next((args[i + 1] for i, t in enumerate(args)
                          if t in ("-c:s", "-scodec") and i + 1 < len(args)),
                         "dvbsub")
        all_dvb = all(s["codec"] == "dvb_subtitle" for s in subs)
        tok = codec_tok if (codec_tok == "copy" and all_dvb) else "dvbsub"
        return StreamManager._remap_subs(args, idxs, tok)

    async def _remux_gate(self, args: list[str], url: str, pace: bool,
                          name: str = "") -> list[str]:
        """
        Per-source reality check for the copy -> MPEG-TS remux (the "direct"
        path: local files requested as .ts, and copy templates on VOD links).

        The command template can only be syntactic - it cannot know what is
        inside the file. Two file properties decide whether the remux lives:
          * the VIDEO codec needs the right Annex-B bitstream filter: H.264
            wants h264_mp4toannexb, HEVC wants hevc_mp4toannexb, MPEG-2/VC-1
            carry start codes natively. The wrong filter (which is what every
            HEVC or MPEG-2-in-MP4 local file got, since _ensure_annexb has to
            assume H.264) kills ffmpeg with rc=234 before the first byte -
            the exact "ffmpeg produced no data for local file" failure.
          * the AUDIO codec must have a berth in MPEG-TS: Vorbis/FLAC/PCM/
            ALAC/Opus (all common in MKV) abort the muxer with "codec not
            currently supported in container". Those get an audio-only
            re-encode to AC3 (a few % CPU; the video stays a copy).

        Like _subs_gate this probes once per source and degrades to the safe
        default, never to an ffmpeg abort. Live inputs (pace=False) are again
        trusted instead of probed (zap speed), and a failed probe leaves the
        command untouched - i.e. exactly the behaviour we had before this
        gate existed.
        """
        if not pace:                       # live: trusted, no probe
            return args
        fmt, f_idx = StreamManager._output_format(args)
        if fmt != "mpegts":                # matroska/hls accept everything
            return args
        copy_v, copy_a = StreamManager._copy_flags(args)
        if not (copy_v or copy_a):         # a real transcode: nothing to fix
            return args
        tag = f"[{name}] " if name else ""
        is_net = url.lower().startswith(_NET_SCHEMES)
        codecs = await media_codecs(url, is_url=is_net)
        if codecs is None:
            await db_log("INFO", "stream", tag +
                         "codec probe failed -> remux command left unchanged")
            return args
        vcodec, acodec = codecs.get("video"), codecs.get("audio")
        out = list(args)

        # ---- video: match the bitstream filter to the real codec ----------
        if copy_v and vcodec:
            want = _TS_VIDEO_BSF.get(vcodec)
            bsf_i = next((i for i, t in enumerate(out)
                          if t in ("-bsf:v", "-bsf:v:0") and i + 1 < len(out)),
                         None)
            have = out[bsf_i + 1] if bsf_i is not None else None
            if want:
                if have is None and f_idx is not None:
                    out[f_idx:f_idx] = ["-bsf:v", want]
                    await db_log("INFO", "stream", tag +
                                 f"video is {vcodec} -> inserted -bsf:v {want}")
                elif have is not None and have != want:
                    out[bsf_i + 1] = want
                    await db_log("INFO", "stream", tag +
                                 f"video is {vcodec} -> -bsf:v {want} "
                                 f"(was {have}; the wrong filter aborts at init)")
            elif have in _TS_VIDEO_BSF.values():
                await db_log("INFO", "stream", tag +
                             f"video is {vcodec} (start codes native) -> "
                             f"-bsf:v {have} removed")
                del out[bsf_i:bsf_i + 2]

        # ---- audio: MPEG-TS berth or a light AC3 re-encode ----------------
        if copy_a and acodec and acodec not in _TS_AUDIO_CODECS:
            changed = False
            j = 0
            while j < len(out):
                t = out[j]
                if t in ("-c:a", "-acodec") and j + 1 < len(out) and out[j + 1] == "copy":
                    out[j + 1] = "ac3"
                    changed = True
                    j += 2
                    continue
                if t in ("-c", "-codec") and j + 1 < len(out) and out[j + 1] == "copy":
                    # `-c copy` covers every stream; expand so video stays a
                    # copy while audio alone is re-encoded
                    out[j:j + 2] = ["-c:v", "copy", "-c:a", "ac3"]
                    changed = True
                    j += 4
                    continue
                j += 1
            if changed:
                if "-b:a" not in out:
                    k = out.index("ac3")
                    out[k + 1:k + 1] = ["-b:a", "384k"]
                await db_log("INFO", "stream", tag +
                             f"audio codec {acodec} cannot ride MPEG-TS -> "
                             "audio transcoded to AC3 (video stays copy)")

        return out

    @staticmethod
    def _ensure_annexb(args: list[str]) -> list[str]:
        """H.264 in MP4/MKV is AVCC; Enigma2's MPEG-TS demuxer needs Annex-B.

        Hand-written copy commands (and older stored templates) omit
        `-bsf:v h264_mp4toannexb`. Without it the box plays audio and a black
        picture. Idempotent: a command that already sets a video bitstream
        filter is left alone. Only applies to `-c:v copy` / `-c copy` into
        mpegts - a transcode already emits Annex-B.

        This pass can only be syntactic (argv knows no codecs), so the filter
        it inserts assumes H.264; the spawn-time `_remux_gate` then corrects
        it against the file's actual video codec (HEVC needs
        `hevc_mp4toannexb`, MPEG-2/VC-1 need none - the wrong filter is a
        fatal rc=234 before the first byte).
        """
        if not args or "-bsf:v" in args or "-bsf:v:0" in args:
            return args
        try:
            last_i = max(idx for idx, a in enumerate(args) if a == "-i")
        except ValueError:
            return args
        fmt = None
        f_idx = None
        copy_v = False
        i = last_i + 1
        while i < len(args):
            t = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if t == "-f" and nxt is not None:
                fmt, f_idx = nxt, i
                i += 2
                continue
            if t in ("-c:v", "-vcodec") and nxt == "copy":
                copy_v = True
                i += 2
                continue
            if t in ("-c", "-codec") and nxt == "copy":
                copy_v = True
                i += 2
                continue
            i += 1
        if not copy_v or fmt != "mpegts" or f_idx is None:
            return args
        out = list(args)
        out[f_idx:f_idx] = ["-bsf:v", "h264_mp4toannexb"]
        return out

    @staticmethod
    def _output_format(args: list[str]) -> tuple[str | None, int | None]:
        """(output muxer, index of its `-f`) for the OUTPUT section, i.e. after
        the last `-i` - an input-side `-f` (grab devices) is not our business."""
        try:
            last_i = max(idx for idx, a in enumerate(args) if a == "-i")
        except ValueError:
            last_i = -1
        fmt = f_idx = None
        i = last_i + 1
        while i < len(args):
            if args[i] == "-f" and i + 1 < len(args):
                fmt, f_idx = args[i + 1], i
                i += 2
                continue
            i += 1
        return fmt, f_idx

    @staticmethod
    def _outputs_matroska(args: list[str]) -> bool:
        """True when the OUTPUT muxer is Matroska (`-f matroska` / `-f mkv`)."""
        return StreamManager._output_format(args)[0] in ("matroska", "mkv")

    @staticmethod
    def _matroska_to_mpegts_for_live(args: list[str]) -> list[str]:
        """Rewrite a live Matroska pipe to MPEG-TS.

        exteplayer3 is audio-only on a non-seekable MKV (no cue index). The
        Enigma2 VOD templates are Matroska on purpose; when one is assigned
        to a live channel we still owe the box a picture, so drop text
        subs (MPEG-TS cannot carry them) and mux MPEG-TS instead.
        """
        fmt, f_idx = StreamManager._output_format(args)
        if fmt not in ("matroska", "mkv") or f_idx is None:
            return args
        out = list(args)
        out[f_idx + 1] = "mpegts"
        i = 0
        while i < len(out):
            if out[i] == "-live" and i + 1 < len(out):
                del out[i:i + 2]
                continue
            i += 1
        out = StreamManager._drop_subtitles(out)
        if "-mpegts_flags" not in out:
            _fmt, f2 = StreamManager._output_format(out)
            if f2 is not None:
                out[f2:f2] = ["-mpegts_flags", "+resend_headers"]
        return out

    @staticmethod
    def _ensure_interleave_flush(args: list[str]) -> list[str]:
        """Cap the CLI interleave buffer so a lagging track cannot stall the
        stream start.

        ffmpeg muxes with av_interleaved_write_frame: packets of the EARLY
        stream are buffered until the LATE one catches up, and only
        `-max_interleave_delta` (default 10 s) forces a flush. An MP4 whose
        audio starts 30-60 s into the video therefore produces its first
        output byte after ~10 s of silence - past (or dangerously near) the
        start timeout, moot before the box ever sees a byte. Capping the delta
        at 2 s makes the same file start at ~2 s. Only for streamed pipes
        (mpegts/matroska); a user's own value always wins.
        """
        if not args or "-max_interleave_delta" in args:
            return args
        fmt, f_idx = StreamManager._output_format(args)
        if fmt not in ("mpegts", "matroska", "mkv") or f_idx is None:
            return args
        out = list(args)
        out[f_idx:f_idx] = ["-max_interleave_delta", MAX_INTERLEAVE_DELTA_US]
        return out

    @staticmethod
    def _copy_flags(args: list[str]) -> tuple[bool, bool]:
        """(video copied, audio copied) for the output section. `-c copy`
        covers both; `-an` means there is no audio to fix at all."""
        copy_v = copy_a = False
        i = 0
        while i < len(args):
            t = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if t in ("-c:v", "-vcodec") and nxt == "copy":
                copy_v = True
                i += 2
                continue
            if t in ("-c:a", "-acodec") and nxt == "copy":
                copy_a = True
                i += 2
                continue
            if t in ("-c", "-codec") and nxt == "copy":
                copy_v = copy_a = True
                i += 2
                continue
            if t == "-an":
                copy_a = False
                i += 1
                continue
            i += 1
        return copy_v, copy_a

    @staticmethod
    def _remap_subs(args: list[str], idxs: list[int], codec: str) -> list[str]:
        """Replace whatever subtitle mapping argv carries with explicit maps for
        `idxs` (ordinals AMONG the subtitle streams, which is what `0:s:N`
        addresses) plus one `-c:s codec`."""
        out = StreamManager._strip_subs(args)
        try:
            last_map = max(i for i, t in enumerate(out) if t == "-map")
            at = last_map + 2          # after the map's VALUE token
        except ValueError:
            at = out.index("-f") if "-f" in out else len(out)
        ins: list[str] = []
        for idx in idxs:
            ins += ["-map", f"0:s:{idx}"]
        ins += ["-c:s", codec]
        out[at:at] = ins
        return out

    async def _open_with_identity(self, command: str, url: str, *,
                                  title: str, pace: bool,
                                  first_byte_timeout: float | None = None
                                  ) -> tuple[object | None, bytes, dict | None]:
        """Spawn ffmpeg for a network URL, walking the media-UA ladder.

        Returns ``(proc, first_chunk, None)`` once an identity produced bytes,
        or ``(None, b"", fail)`` when nothing did, where ``fail`` is
        ``{"rc", "tail", "stalled"}`` for the caller's existing warning, or
        None when ffmpeg itself could not be spawned (bad template/binary -
        the caller already logged it).

        A template that pins its own ``-user_agent`` opts out of the ladder:
        the operator's explicit choice wins. Otherwise the origin is offered
        first its learned UA, then the MAG player UA, then - exactly once, on
        a pre-first-byte HTTP 4xx - the portal browser UA. The identity that
        produces bytes is remembered per origin host (stream_identity), so
        the next play costs one spawn again. A silent stall/timeout is NOT an
        identity answer and never spends the browser-UA retry.
        """
        template_owns = "-user_agent" in (command or "")
        is_net = url.lower().startswith(_NET_SCHEMES)
        # Local files have no media endpoint to negotiate identity with: one
        # attempt, no respawn, nothing learned.
        if template_owns or not is_net:
            choices: list[str | None] = [None]
        else:
            choices = stream_identity.ladder(url)
        last: dict | None = None
        for rung, ua in enumerate(choices):
            t0 = time.monotonic()
            proc = await self._spawn(command, url, title, pace=pace,
                                     user_agent=ua)
            if proc is None:
                return None, b"", None
            first = await self._first_bytes(proc, first_byte_timeout)
            if first:
                if ua:
                    stream_identity.remember(url, ua)
                return proc, first, None
            stalled = proc.returncode is None
            await self._kill_quiet(proc)
            # The tail now drives the retry decision, so wait for the stderr
            # reader to finish (it ends on process exit) before looking at it.
            drain = getattr(proc, "spm_stderr_task", None)
            if drain is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(drain), timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
            tail = self._stderr_tail(proc)
            elapsed = time.monotonic() - t0
            last = {"rc": proc.returncode, "tail": tail, "stalled": stalled}
            # A deterministic output/template failure cannot be repaired by a
            # different HTTP identity. Return it immediately so the caller can
            # mark the handle dead instead of spending the browser-UA rung.
            if _is_template_output_failure(last):
                return None, b"", last
            status = stream_identity.http_open_error(last["rc"], tail, elapsed)
            if status is None and stalled is False and not template_owns:
                # Explain why an apparently identity-shaped 4xx did NOT spend
                # the other-UA rung: it arrived too slowly (panel connection
                # slot check, not a WAF refusal) - the MAC fallback is the
                # useful next step.
                bare = stream_identity.http_open_error(last["rc"], tail)
                if bare is not None and elapsed > stream_identity.FAST_REFUSAL_S:
                    await db_log(
                        "INFO", "stream",
                        f"[{title}] origin answered HTTP {bare} after "
                        f"{elapsed:.1f}s - too slow for an identity refusal "
                        f"(usually: MAC connection slot still held); moving "
                        f"to the next source/MAC instead of retrying the UA")
            if status is not None and rung + 1 < len(choices):
                nxt = choices[rung + 1]
                next_label = ("portal browser"
                              if nxt == stream_identity.STB_UA else "media player")
                await db_log(
                    "INFO", "stream",
                    f"[{title}] origin answered HTTP {status} to ffmpeg's "
                    f"media request and sent 0 bytes - retrying once with the "
                    f"{next_label} user-agent")
                continue
            return None, b"", last
        return None, b"", last

    async def _spawn(self, cmd_template: str, url: str, title: str | None = None,
                     pace: bool = False, user_agent: str | None = None) -> asyncio.subprocess.Process | None:
        if (cmd_template or "").strip() == REDIRECT_COMMAND:
            await db_log("ERROR", "stream",
                         "cannot spawn the redirect template as ffmpeg "
                         "(local files must be served directly)")
            return None
        if "<out_dir>" in (cmd_template or ""):
            await db_log("ERROR", "stream", "template uses HLS file output; use mpegts for live proxying")
            return None
        args = self._ffmpeg_argv(cmd_template, url, title, pace, user_agent)
        if not args:
            await db_log("ERROR", "stream", "unparseable ffmpeg template")
            raise FFmpegTemplateError("template could not be tokenized")
        argv_errors = argv_validation_errors(args)
        if argv_errors:
            detail = "; ".join(argv_errors)
            await db_log("ERROR", "stream",
                         f"invalid FFmpeg template argv: {detail}")
            raise FFmpegTemplateError(detail)
        args = await self._remux_gate(args, url, pace, title or "")
        args = await self._subs_gate(args, url, pace, title or "")
        argv_errors = argv_validation_errors(args)
        if argv_errors:
            detail = "; ".join(argv_errors)
            await db_log("ERROR", "stream",
                         f"invalid FFmpeg argv after stream gates: {detail}")
            raise FFmpegTemplateError(detail)
        # Use shell-escaped text for humans copying the diagnostic. The process
        # itself still receives the original argv through create_subprocess_exec.
        await db_log("DEBUG", "ffmpeg", f"spawn command: {shlex.join(args)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # The marker (uuid per stream) is how a later SPM process
                # recognises OUR ffmpeg children: after a crash, `kill -9`, or a
                # container restart the pipes survive their parent, keep reading
                # the panel stream, and the panel keeps counting the MAC's slot -
                # visible only as `limit` / "account is in use" with an empty
                # dashboard. `sweep_orphans()` matches on this and on nothing
                # else, so a user's own ffmpeg is never touched.
                env={**os.environ, _STREAM_ENV_MARKER: uuid.uuid4().hex})
            # parked on the process so a failed identity-ladder rung can wait
            # for ffmpeg's final stderr (the HTTP 4xx line drives the retry)
            proc.spm_stderr_task = asyncio.get_running_loop().create_task(
                self._drain_stderr(proc))
            return proc
        except FileNotFoundError:
            await db_log("ERROR", "stream", f"ffmpeg binary not found: {args[0]}")
            return None

    async def _drain_stderr(self, proc) -> None:
        """Keep stderr from blocking; last lines are logged on failure.

        The raw tail is also parked on the process object: a stream that has
        to be KILLED after a silent stall (rc ends up -9, which is
        deliberately not logged here - it is mostly user kills) then still
        gets to explain itself; the local pump logs that tail after stopping
        the process (see _pump's 'no data' branch).
        """
        lines: list[bytes] = []
        proc.spm_stderr_tail = lines
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                lines.append(line)
                del lines[:-30]
        except Exception:  # noqa: BLE001
            pass
        rc = await proc.wait()
        if lines and rc not in (0, None, -9, 9):
            tail = b"".join(lines[-12:]).decode(errors="replace").strip()
            if tail:
                await db_log("WARNING", "ffmpeg", f"ffmpeg exited rc={rc}: {tail[:900]}")

    @staticmethod
    def _stderr_tail(proc, max_lines: int = 8, limit: int = 600) -> str:
        """The last stderr lines (the tail _drain_stderr collected), as text.

        ffmpeg writes progress stats separated by \r without newlines, so
        squeezing them onto single lines keeps the log readable.
        """
        lines = getattr(proc, "spm_stderr_tail", None) or []
        if not lines:
            return ""
        text = b"".join(lines[-max_lines:]).decode(errors="replace")
        squashed = "\n".join(ln for ln in
                             (l.strip() for l in text.replace("\r", "\n").splitlines())
                             if ln)
        return squashed[-limit:]

    @staticmethod
    async def _kill_quiet(proc) -> None:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------- chain building
    @staticmethod
    def _pick_macs(macs, portal_id: int, strategy: str, used_portals: set[int]):
        """Apply the GUI/env fallback strategy to one portal's MAC list.

        macs_first  -> try every MAC of this portal (even if the portal already
                       appeared earlier in the chain as a different source).
        portal_first -> one MAC per portal; later sources on the same portal
                       are skipped so we hop to the next portal immediately.

        MACs the *portal* says are unusable (banned / expired subscription) are
        dropped first: opening a stream through one costs a create_link, a
        refusal, and - on a panel that counts connections per MAC - a slot that a
        working MAC could have used. `offline`/`error` stay in the list because
        those are our own verdicts about transport, usually transient.
        """
        usable = [m for m in macs if mac_is_usable(getattr(m, "status", None))]
        if len(usable) != len(macs):
            log.info("skipping %d mac(s) the portal says are unusable for portal %s",
                     len(macs) - len(usable), portal_id)
        macs = usable
        if not macs:
            return None
        if strategy == "portal_first":
            if portal_id in used_portals:
                return None
            picked = list(macs[:1])
            if len(macs) > 1:
                # Worth one line per portal: with several MACs on it, this
                # strategy answers a zap with the *same* MAC every time -
                # exactly the thing a panel's one-connection slot refuses.
                # `macs_first` walks them all, like the reference proxy does.
                _warn_portal_first_once(portal_id, len(macs))
        else:
            picked = list(macs)
        used_portals.add(portal_id)
        return picked

    async def _live_chain(self, playlist_id: int) -> tuple[list, str, object]:
        """[(LiveSource, Portal, [MacAddress...])] in fallback priority order."""
        from .runtime_settings import fallback_strategy
        strategy = await fallback_strategy()
        async with SessionLocal() as s:
            item = await s.get(LivePlaylist, playlist_id)
            if not item:
                return [], "", None
            rows = (await s.execute(
                select(LivePlaylistSource).where(LivePlaylistSource.live_playlist_id == playlist_id)
                .order_by(LivePlaylistSource.priority))).scalars().all()
            chain = []
            used_portals: set[int] = set()
            for r in rows:
                src = await s.get(LiveSource, r.live_source_id)
                if not src or not src.cmd:
                    continue
                portal = await s.get(Portal, src.portal_id)
                if not portal or not portal.enabled:
                    continue
                macs = (await s.execute(select(MacAddress).where(
                    MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().all()
                picked = self._pick_macs(macs, portal.id, strategy, used_portals)
                if not picked:
                    continue
                chain.append((src, portal, picked))
            # Per-MAC id translations: every chain MAC asks with ITS own cmd.
            # Inside the open session on purpose — chain rows detach when it
            # closes, and the plain attribute survives that.
            await attach_overrides(chain, session=s)
            return chain, item.custom_name, item

    async def _vod_chain(self, playlist_id: int):
        from .runtime_settings import fallback_strategy
        strategy = await fallback_strategy()
        async with SessionLocal() as s:
            item = await s.get(VodPlaylist, playlist_id)
            if not item:
                return [], "", None
            rows = (await s.execute(
                select(VodPlaylistSource).where(VodPlaylistSource.vod_playlist_id == playlist_id)
                .order_by(VodPlaylistSource.priority))).scalars().all()
            chain = []
            used_portals: set[int] = set()
            for r in rows:
                src = await s.get(VodSource, r.vod_source_id)
                if not src or not src.cmd:
                    continue
                portal = await s.get(Portal, src.portal_id)
                if not portal or not portal.enabled:
                    continue
                macs = (await s.execute(select(MacAddress).where(
                    MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().all()
                picked = self._pick_macs(macs, portal.id, strategy, used_portals)
                if picked:
                    chain.append((src, portal, picked))
            return chain, item.custom_name, item

    async def _episode_target(self, episode_id: int):
        """Episode -> owning SeriePlaylist, its season/episode coords + fallback chain."""
        async with SessionLocal() as s:
            ep = await s.get(SerieEpisode, episode_id)
            if not ep:
                return None
            season = await s.get(SerieSeason, ep.serie_season_id)
            serie = await s.get(SerieSource, season.serie_source_id)
            pl = (await s.execute(select(SeriePlaylist).where(
                SeriePlaylist.serie_source_id == serie.id, SeriePlaylist.enabled.is_(True))
            )).scalar_one_or_none()
            if pl is None:
                return None
            rows = (await s.execute(
                select(SeriePlaylistSource).where(SeriePlaylistSource.serie_playlist_id == pl.id)
                .order_by(SeriePlaylistSource.priority))).scalars().all()
            from .runtime_settings import fallback_strategy
            strategy = await fallback_strategy()
            chain = []
            used_portals: set[int] = set()
            for r in rows:
                src = await s.get(SerieSource, r.serie_source_id)
                if not src:
                    continue
                # find same season/episode on this source
                alt_season = (await s.execute(select(SerieSeason).where(
                    SerieSeason.serie_source_id == src.id,
                    SerieSeason.season_number == season.season_number))).scalar_one_or_none()
                if not alt_season:
                    continue
                alt_ep = (await s.execute(select(SerieEpisode).where(
                    SerieEpisode.serie_season_id == alt_season.id,
                    SerieEpisode.episode_number == ep.episode_number))).scalar_one_or_none()
                if not alt_ep or not alt_ep.cmd:
                    continue
                portal = await s.get(Portal, src.portal_id)
                if not portal or not portal.enabled:
                    continue
                macs = (await s.execute(select(MacAddress).where(
                    MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().all()
                picked = self._pick_macs(macs, portal.id, strategy, used_portals)
                if picked:
                    chain.append((alt_ep, portal, picked))
            name = f"{pl.custom_name} S{season.season_number:02d}E{ep.episode_number:02d}"
            return chain, name, pl

    async def _open_preview(self, src, portal, macs, kind: str = "live",
                            name: str | None = None, template_id: int | None = None):
        """
        Throwaway stream straight from an ORIGINAL source (GUI 'test stream').
        No playlist involvement; full fallback over the portal's MACs applies.

        Uses the same template resolution as the real pipeline, so what you
        preview is what your viewers get. It used to hardcode `-c copy`, which
        meant a HEVC or AC3 stream stayed black in the preview even when a
        working VAAPI/QSV transcode template would have played it fine - the
        preview disagreed with reality in exactly the case that matters.
        Pass template_id to force a specific one.

        ONE exception, and it is the reason the preview popup used to sit
        there with no input at all: sources carry no ffmpeg_template_id of
        their own, so without ?tpl= resolution lands on the DEFAULT template -
        and the shipped default is "Redirect (bypass ffmpeg)" (@redirect),
        which is a marker, not a command. _spawn refuses it, the pump yields
        nothing and the popup stares at a 25s "no data" 502. A preview probes
        the SOURCE, so when the marker comes back the probe falls back to the
        Copy passthrough command (the same thing the "Retry with" dropdown
        would otherwise be needed for)."""
        probe = src if template_id is None else _WithTemplate(src, template_id)
        tpl_name, command = await self._template_for(probe)
        if command == REDIRECT_COMMAND:
            tpl_name, command = await self._copy_fallback_command()
        h = StreamHandle(id=uuid.uuid4().hex, kind="preview",
                         item_name=name or getattr(src, "original_name", None)
                         or getattr(src, "name", "preview"),
                         user_name="admin", template_name=tpl_name, command=command)
        # RAW src, not the _WithTemplate probe (that wrapper is template
        # lookup only): translations ride on the row plan_for will read.
        # Vod/Serie sources make this a no-op (isinstance guard inside).
        await attach_overrides([(src, portal, macs)])
        gen = self._pump(h, [(src, portal, macs)], "live" if kind == "live" else "vod")
        return h, gen

    async def _copy_fallback_command(self) -> tuple[str, str]:
        """(name, command) for 'proxy this without transcoding'.

        Used wherever a play is forced through ffmpeg but the resolved template
        is the `@redirect` marker - which is not a command and would die in
        _spawn with a 502: the preview probe, and an explicit `?mode=proxy`
        (or an Enigma2 profile with delivery=proxy) on a redirect-template
        item. Prefers the Copy preset; the synthetic command is the last resort
        when it is missing or disabled. Always plain MPEG-TS, which is what the
        Enigma2 bouquet announces for these items.
        """
        async with SessionLocal() as s:
            copy_tpl = (await s.execute(select(FFmpegTemplate).where(
                FFmpegTemplate.name == COPY_PRESET_NAME,
                FFmpegTemplate.enabled.is_(True)))).scalar_one_or_none()
        if copy_tpl is not None and (copy_tpl.command or "").strip():
            return copy_tpl.name, copy_tpl.command
        return "(copy)", f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"

    async def _template_for(self, item, *, kind: str | None = None,
                            user_name: str | None = None) -> tuple[str, str]:
        """(template name, command with <url>) - area overlay then item then default."""
        from .playback import template_map_for_username
        async with SessionLocal() as s:
            tmap = await template_map_for_username(s, user_name)
            resolved = tmap.resolve(kind or "", item)
            return resolved.name, resolved.command

    async def _item_for(self, kind: str, ref_id: int):
        """The playlist item owning a stream ref, for template resolution.

        Kept deliberately light (no fallback-chain assembly): the redirect
        decision only needs to know which template the item carries.
        """
        async with SessionLocal() as s:
            if kind == "live":
                return await s.get(LivePlaylist, ref_id)
            if kind == "vod":
                return await s.get(VodPlaylist, ref_id)
            if kind == "episode":
                ep = await s.get(SerieEpisode, ref_id)
                if not ep:
                    return None
                season = await s.get(SerieSeason, ep.serie_season_id)
                serie = await s.get(SerieSource, season.serie_source_id) if season else None
                if not serie:
                    return None
                return (await s.execute(select(SeriePlaylist).where(
                    SeriePlaylist.serie_source_id == serie.id,
                    SeriePlaylist.enabled.is_(True)))).scalar_one_or_none()
            if kind == "local":
                return await s.get(LocalPlaylist, ref_id)
        return None

    async def uses_redirect(self, kind: str, ref_id: int,
                            user_name: str | None = None) -> bool:
        """True when the item's effective template is the redirect/bypass preset.

        This backs the per-channel "bypass ffmpeg" mode: a playlist item whose
        FFmpeg template is `REDIRECT_PRESET_NAME` is 302'd straight to the
        panel's CDN instead of being proxied through ffmpeg. The redirect
        preset is ALSO the built-in default template, so an item without an
        explicit template assignment resolves to it and redirects too - the old
        global switch, expressed as a template. An area overlay (per user)
        can still force a real transcode template on the same item.
        """
        item = await self._item_for(kind, ref_id)
        if item is None:
            return False
        _name, command = await self._template_for(item, kind=kind, user_name=user_name)
        return command == REDIRECT_COMMAND

    async def local_disk_path(self, ref_id: int) -> tuple[str | None, str]:
        """Absolute path of a local playlist item, or (None, name) if missing."""
        async with SessionLocal() as s:
            row = (await s.execute(
                select(LocalPlaylist, LocalFile, LocalSource)
                .join(LocalFile, LocalPlaylist.local_file_id == LocalFile.id)
                .join(LocalSource, LocalFile.local_source_id == LocalSource.id)
                .where(LocalPlaylist.id == ref_id)
            )).one_or_none()
        if not row:
            return None, "local file"
        lp, lf, ls = row
        name = lp.custom_name or lf.filename
        path = local_file_path(ls.directory, lf.relative_path)
        if path and os.path.isfile(path):
            probe = None
            try:
                stat = os.stat(path)
                stored_ts = datetime.fromisoformat(lf.mtime).timestamp() if lf.mtime else None
                unchanged = (stat.st_size == lf.size_bytes and stored_ts is not None
                             and abs(stat.st_mtime - stored_ts) < 0.001)
            except (OSError, TypeError, ValueError):
                unchanged = False
            if unchanged and lf.media_probe:
                try:
                    probe = json.loads(lf.media_probe)
                except (TypeError, ValueError):
                    pass
            # Missing/invalid/stale metadata deliberately evicts an older
            # in-memory answer, so a replaced file falls back to a safe probe.
            prime_local_startup_cache(path, probe)
            return path, name
        return None, name

    async def local_serves_original(self, ref_id: int,
                                    user_name: str | None = None) -> bool:
        """True when this local item should be FileResponse'd, not ffmpeg'd."""
        item = await self._item_for("local", ref_id)
        _name, command = await self._template_for(item, kind="local", user_name=user_name)
        return serves_original_file(command)

    async def register_local_file(self, ref_id: int, user_name: str | None,
                                  path: str, item_name: str) -> StreamHandle:
        """Dashboard/max-connections slot for a direct file serve (no ffmpeg)."""
        item = await self._item_for("local", ref_id)
        tpl_name, _command = await self._template_for(
            item, kind="local", user_name=user_name)
        h = StreamHandle(id=uuid.uuid4().hex, kind="local", item_name=item_name,
                         user_name=user_name, template_name=tpl_name or "file",
                         command="(file)", url=path)
        await self._register(h)
        from ..database import spawn
        from .local_files import fill_duration_for_playlist_item
        spawn(fill_duration_for_playlist_item(ref_id),
              name=f"dur-local-{ref_id}")
        return h

    # ---------------------------------------------------------- link (R2)
    @staticmethod
    def start_budget(chain: list, *, kind: str = "live") -> float:
        """How long the engine may look for a first byte before it gives up.

        The chain time is `passes x candidates x STREAM_START_TIMEOUT` plus one
        ZAP_RETRY_DELAY per extra pass. Capped by SPM_STREAM_START_BUDGET so a
        playlist with six sources cannot hang a player for four minutes; the
        output guard adds START_BUDGET_SLACK on top, which is what makes the
        guard a backstop instead of a race.
        """
        if kind == "local" or not chain:
            # Local files never walk MACs: one spawn, one start window.
            return STREAM_START_TIMEOUT if chain else 0.0
        candidates = sum(max(1, len(macs)) for _s, _p, macs in chain)
        passes = 2 if (ZAP_RETRY and chain) else 1
        raw = passes * candidates * STREAM_START_TIMEOUT + (passes - 1) * ZAP_RETRY_DELAY
        return min(raw, STREAM_START_BUDGET) if STREAM_START_BUDGET > 0 else raw

    def _note_chain_limits(self, chain: list) -> None:
        """Remember each MAC's concurrency limit before anything asks is_mac_busy."""
        for _src, portal, macs in chain or ():
            for m in macs or ():
                self.note_mac_limit(getattr(m, "id", None),
                                    getattr(portal, "streams_per_mac", None))

    def _any_free(self, chain: list, requester: str | None) -> bool:
        """Is there a MAC in this chain nothing holds right now?

        The question STB-Proxy's `/play` loop asks per MAC (`isMacFree()`) and
        the reason a zap lands on a multi-MAC portal: with one free MAC there is
        no reason to wait for, or take over, any busy one.
        """
        for _src, portal, macs in chain or ():
            for m in self._macs_for(portal, _src, macs) or ():
                if m is None or not self.is_mac_busy(getattr(m, "id", None),
                                                     requester=requester):
                    return True
        return False

    def order_by_free(self, route, source, macs: list,
                      requester: str | None) -> list:
        """Untouched MACs first, then ones this user just used, then the rest.

        Route affinity (`ordered_macs`) answers "which MAC worked last" - and on
        a zap that can be exactly the MAC the player is still leaving, whose
        panel slot is not free yet. So the ranking is:

          0  nothing holds it (no pipe, no lease) - the one a player should get
          1  only *this user's* post-302 lease: usable (the lease may be taken
             back), but the panel probably still counts it
          2  somebody else's stream or lease

        Stable within a rank, so affinity still decides between equals - which is
        what keeps the zap-back cache working (both MACs rank 1, and the one that
        played the channel keeps its link).
        """
        if not macs:
            return macs

        def rank(m):
            mid = getattr(m, "id", None)
            if not self._lock_set(mid) and self.lease_remaining(mid) <= 0:
                return 0
            if not self._lock_set(mid) and requester \
                    and self.lease_holder(mid) == requester:
                return 1
            return 2

        return sorted(macs, key=rank)

    async def _first_free_mac(self, chain: list, requester: str | None):
        """(source, portal, mac) of the first MAC this start may use, or None.

        Waiting is bounded for the whole chain, not per MAC (BUSY_WAIT_S in
        total): three busy MACs must not turn a zap into a nine-second hang.
        """
        deadline = time.monotonic() + BUSY_WAIT_S
        for step in chain or ():
            src, portal, macs = step
            for mac in macs or ():
                left = max(0.0, deadline - time.monotonic())
                if await self.wait_for_mac(mac, requester, budget=left):
                    return src, portal, mac
        return None

    # ------------------------------------------------------------ diagnostics
    def note_timing(self, *, kind: str, mode: str, item: str = "", portal: str = "",
                    mac: str = "", total_ms: float = 0.0,
                    prepare_ms: float | None = None, first_ms: float | None = None,
                    fail: str = "") -> None:
        """Remember one play's phase timings (or why it never played)."""
        self.timings.append({
            "at": time.time(), "kind": kind, "mode": mode, "item": item,
            "portal": portal, "mac": mac, "fail": fail,
            "total_ms": round(float(total_ms or 0.0), 1),
            "prepare_ms": None if prepare_ms is None else round(float(prepare_ms), 1),
            "first_ms": None if first_ms is None else round(float(first_ms), 1),
        })

    @staticmethod
    def _percentiles(values: list[float]) -> dict:
        if not values:
            return {"n": 0, "p50": None, "p90": None, "max": None}
        ordered = sorted(values)
        def at(p: float) -> float:
            idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
            return round(ordered[idx], 1)
        return {"n": len(ordered), "p50": at(0.5), "p90": at(0.9), "max": at(1.0)}

    def timing_summary(self) -> dict:
        """Percentiles per path, failures by reason, and a per-portal view.

        This is the answer to "why does zapping feel slow": it separates the
        302 path (panel + one probe) from the proxy path (panel + ffmpeg + the
        source's first byte) and names the portal and MAC that were slow.
        """
        rows = list(self.timings)
        modes: dict[str, dict] = {}
        for mode in ("proxy", "redirect", "local"):
            ok = [r for r in rows if r["mode"] == mode and not r["fail"]]
            if not ok:
                continue
            modes[mode] = {
                "total": self._percentiles([r["total_ms"] for r in ok]),
                "first_byte": self._percentiles(
                    [r["first_ms"] for r in ok if r["first_ms"] is not None]),
                "prepare": self._percentiles(
                    [r["prepare_ms"] for r in ok if r["prepare_ms"] is not None]),
            }
        fails: dict[str, int] = {}
        for r in rows:
            if r["fail"]:
                fails[r["fail"]] = fails.get(r["fail"], 0) + 1
        portals: dict[str, dict] = {}
        for r in rows:
            if not r["portal"]:
                continue
            entry = portals.setdefault(r["portal"], {"starts": 0, "fails": 0,
                                                     "total_ms": []})
            if r["fail"]:
                entry["fails"] += 1
            else:
                entry["starts"] += 1
                entry["total_ms"].append(r["total_ms"])
        for name, entry in portals.items():
            entry["p50_ms"] = self._percentiles(entry.pop("total_ms"))["p50"]
        return {"window": len(rows), "modes": modes, "failures": fails,
                "portals": portals, "recent": rows[-12:]}

    def _free_candidates(self, chain: list, requester: str | None) -> int:
        """How many MACs in this chain could be played right now (see HEDGE_AFTER_S).

        A MAC counts once, even when several steps of the chain carry it: the
        fence's threshold is "this many *different* slots are free", and a
        decision walked twice must not look like two free candidates. An
        adopted Xtream step owns no MAC and never counts - see `_macs_for`.
        """
        free: set[int] = set()
        for _src, portal, macs in chain or ():
            for mac in self._macs_for(portal, _src, macs) or ():
                mid = getattr(mac, "id", None)
                if mid is None:
                    continue
                if not self.is_mac_busy(mid, requester=requester):
                    free.add(mid)
        return len(free)

    def _occupied_note(self, mac_row) -> str:
        """One MAC's occupancy as a phrase for a log line ('' when free)."""
        info = self.mac_occupancy(getattr(mac_row, "id", None))
        if not info or not info.get("busy"):
            return ""
        mac = getattr(mac_row, "mac", "?")
        if info.get("reason") == "pipe":
            who = info.get("holder") or "?"
            return f"{mac} streaming via ffmpeg ({who})"
        who = info.get("holder") or "unknown user"
        item = info.get("item") or "?"
        return (f"{mac} redirect lease {info.get('remaining_s', 0):.0f}s left "
                f"({who}, {item})")

    def occupancy_for_stats(self) -> dict:
        """Redirect leases and ffmpeg pipes as plain MAC ids, for the GUI."""
        return {str(mid): info for mid, info in self.occupancy_map().items()}

    @staticmethod
    def _macs_for(portal, src, macs):
        """The MAC rows a chain step may walk, or the one thing that replaces them.

        An adopted Xtream source needs no MAC at all (its URL carries the Xtream
        credentials), and a user who adopted a portal and then removed its MACs -
        the honest thing to do, since the panel no longer needs them - must still
        be able to watch it. `None` means exactly that, and every MAC-slot path
        below is guarded by `adopted`; an adopted step never touches one.
        """
        if macs:
            return macs
        if getattr(portal, "xtream_adopted", False) and getattr(src, "xtream_url", None):
            return [None]
        return macs

    @staticmethod
    def _plan(src, mac_row, portal, *, ffmpeg: bool = False):
        """(ask or play as stored) for one (source, MAC), decided the same way by
        every path. See app/portal/links.py for the rules and their reasons.

        An adopted portal (R7) with a per-channel Xtream URL outranks everything,
        including the "ffmpeg always asks" rule: there is nothing to ask for, since
        the harvested URL *is* the stream the portal would have built.
        """
        if getattr(portal, "xtream_adopted", False):
            adopted = str(getattr(src, "xtream_url", "") or "")
            if adopted:
                return plan_adopted(adopted, src=src, mac_row=mac_row)
        return plan_for(src, mac_row, ffmpeg=ffmpeg,
                        allow_direct=bool(getattr(portal, "direct_links", True)))

    async def _create_link_with_backoff(self, client, plan, link_kind: str,
                                        item_name: str, mac_row):
        """create_link, re-asking the same MAC while the panel says "busy".

        A panel refuses a second link on a MAC that is still streaming with
        `limit` / `account_is_in_use` / HTTP 456, and frees the slot seconds
        after the old connection dies. Moving on at the first refusal is what
        turned a fast zap into "switching channel does not work" - on a
        single-MAC portal there is no next candidate, and even with several the
        panel's own answer is a moment away, not a MAC problem. The ladder is
        deliberately short (BUSY_BACKOFF): it sits inside the start budget, and
        a MAC that is genuinely in use elsewhere must not cost seconds before
        the walk moves on.
        """
        for idx, wait in enumerate((0.0,) + BUSY_BACKOFF):
            if wait:
                await asyncio.sleep(wait)
            try:
                return await client.create_link(plan.cmd, link_kind,
                                                **plan.request_kwargs())
            except PortalError as exc:
                if exc.code not in SLOT_BUSY_CODES or idx >= len(BUSY_BACKOFF):
                    raise
                await db_log("INFO", "stream",
                             f"[{item_name}] {getattr(mac_row, 'mac', '?')}: the panel still "
                             f"counts the previous connection ({exc.code}) - asking again in "
                             f"{BUSY_BACKOFF[idx]:.1f}s")
        raise AssertionError("unreachable")   # pragma: no cover

    # ------------------------------------------------------------ the pump
    async def resolve(self, kind: str, ref_id: int,
                      requester: str | None = None,
                      out: dict | None = None) -> tuple[str | None, str]:
        """
        Resolve a playable portal URL WITHOUT starting ffmpeg.

        This backs the "redirect" output mode: the player is sent straight to
        the panel's CDN, so there is no transcode, no container CPU and no
        ffmpeg start-up delay - the answer to "VAAPI takes three minutes to
        start" and to "copy does not work" on panels whose stream ffmpeg will
        not remux. The trade-off is that we cannot rewrite the transport
        stream, and a link that dies mid-playback is not retried (the player
        sees EOF instead of our fallback chain).

        Returns (url, item_name); url is None when nothing resolved. `out`, when
        given, receives {"busy": bool} - True when every candidate was refused by
        *our own* occupancy or by the panel's connection slot rather than by a
        dead source. The route answers 503 + Retry-After for that instead of a
        502 "no source produced a link", because it is the one failure a player
        wins by asking again a second later.
        """
        if kind == "live":
            chain, item_name, _item = await self._live_chain(ref_id)
            link_kind = "live"
        elif kind == "vod":
            chain, item_name, _item = await self._vod_chain(ref_id)
            link_kind = "vod"
        elif kind == "episode":
            got = await self._episode_target(ref_id)
            chain, item_name, _item = got if got else ([], "episode", None)
            link_kind = "vod"
        else:
            raise ValueError(f"kind {kind!r} cannot be redirected (no portal URL)")

        route_key = (kind, ref_id)
        self._note_chain_limits(chain)
        chain = self.route_health.ordered_chain(route_key, chain)
        # >>> redirect-guard (feature 2; delete with app/services/redirect_guard.py)
        chain = demote_recently_handed(route_key, chain)
        # <<< redirect-guard
        from .runtime_settings import prefer_free_mac
        prefer_free = await prefer_free_mac()
        every_candidate_busy = True
        attempts = 2 if (ZAP_RETRY and chain) else 1
        for pass_no in range(attempts):
            if pass_no == 1:
                await db_log("INFO", "stream",
                             f"[{item_name}] redirect: first pass found no link "
                             f"-> retrying once in {ZAP_RETRY_DELAY:.1f}s (zap overlap?)")
                await asyncio.sleep(ZAP_RETRY_DELAY)
            # A (source, MAC) that failed a moment ago goes last (see
            # demote_failed_candidates): affinity must not re-pick it first.
            chain = demote_failed_candidates(route_key, chain)
            for _src, portal, macs in chain:
                candidates = self._macs_for(portal, _src, macs)
                candidates = self.route_health.ordered_macs(route_key, _src, candidates)
                candidates = demote_macs(route_key, _src, candidates)
                if prefer_free:
                    candidates = self.order_by_free(route_key, _src, candidates, requester)
                for mac_row in candidates:
                    # ffmpeg lock OR a recent redirect lease — both mean "leave this
                    # MAC alone". Redirects never enter mac_locks (we no longer hold
                    # the socket after the 302), so the lease is the only signal.
                    # The same user's own lease is exempt: that is the channel this
                    # box just zapped away from (see is_mac_busy), not another
                    # viewer, and skipping it sends the zap to a worse MAC.
                    hit = cached_link(kind, ref_id, getattr(mac_row, "id", None),
                                      user_name=requester) if pass_no == 0 else None
                    if hit and not self.is_mac_busy(getattr(mac_row, "id", None),
                                                    requester=requester):
                        cached_url, age = hit
                        # The link was good minutes ago, not now: probe before
                        # replaying it (same probe the fresh link gets below).
                        # <<< redirect-guard note: link_is_alive is the guard's
                        if await link_is_alive(cached_url):
                            await db_log("INFO", "stream",
                                         f"[{item_name}] redirect: replaying the link resolved "
                                         f"{age:.0f}s ago on {mac_row.mac} (no create_link)")
                            if mac_row is not None:
                                self.lease_mac(mac_row.id, holder=requester,
                                               item=item_name, kind=kind, ref=ref_id)
                            self.route_health.succeeded(route_key, _src, mac_row)
                            return cached_url, item_name
                        drop_link(kind, ref_id, getattr(mac_row, "id", None), requester)
                        await db_log("INFO", "stream",
                                     f"[{item_name}] redirect: the link from {age:.0f}s ago is "
                                     "gone -> resolving a fresh one")
                    if mac_row is not None and self.is_mac_busy(mac_row.id,
                                                                requester=requester):
                        # Not a failure of the source: our own pipe/lease (or
                        # another user's) holds the MAC. Leaves the "every
                        # candidate was busy" flag alone, which is what turns
                        # the answer into 503 + Retry-After.
                        await db_log("INFO", "stream",
                                     f"[{item_name}] redirect: mac {mac_row.mac} busy "
                                     "(ffmpeg pipe or redirect lease) -> skip")
                        continue
                    if mac_row is not None and self.lease_holder(mac_row.id) == requester \
                            and requester:
                        await db_log("INFO", "stream",
                                     f"[{item_name}] redirect: taking over the redirect "
                                     f"lease on {mac_row.mac} held by {requester} "
                                     "(the channel this zap left)")
                    # Decided first, before any portal session exists: the point of
                    # R2 is that a channel the panel described as permanent costs the
                    # player one redirect and us *nothing* - no handshake reuse, no
                    # token, no create_link. The old shape paid for all of that and
                    # then threw the answer away in favour of the stored URL anyway.
                    plan = self._plan(_src, mac_row, portal)
                    if plan.policy.direct:
                        await db_log("INFO", "stream",
                                     f"[{item_name}] playing the stored link via "
                                     f"{portal.name}/"
                                     f"{mac_row.mac if mac_row is not None else 'xtream'}: "
                                     f"{plan.policy.reason}")
                        # >>> redirect-guard (features 1+2; delete with app/services/redirect_guard.py)
                        _probe = await link_is_alive(plan.direct_url)
                        if not _probe:
                            await db_log("WARNING", "stream",
                                         f"[{item_name}] redirect: stored link dead "
                                         f"({portal.name}; {_probe.detail}) -> next candidate")
                            note_candidate_failure(route_key, _src, mac_row)
                            every_candidate_busy = False
                            continue
                        note_handed_out(route_key, _src, mac_row)
                        # <<< redirect-guard
                        note_link(kind, ref_id, getattr(mac_row, "id", None),
                                  plan.direct_url, requester)
                        clear_candidate_failures(route_key)
                        if mac_row is not None:
                            self.lease_mac(mac_row.id, holder=requester,
                                           item=item_name, kind=kind, ref=ref_id)
                        self.route_health.succeeded(route_key, _src, mac_row)
                        if out is not None:
                            out["portal"] = portal.name
                            out["mac"] = getattr(mac_row, "mac", "") or ""
                        return plan.direct_url, item_name
                    client = await POOL.get(PortalSession.from_rows(portal, mac_row))
                    repair = None
                    try:
                        if not portal.resolved_url:
                            from ..portal.resolver import resolve_portal
                            res = await resolve_portal(portal.base_url, mac=mac_row.mac,
                                                      proxy=portal.proxy_url,
                                                      tls_insecure=portal.tls_insecure)
                            if res.ok:
                                portal.resolved_url = res.portal_url
                                portal.resolved_path = res.path
                                await _store_resolved_portal(portal.id, res.portal_url, res.path)
                                client.portal_url = res.portal_url
                                client.invalidate()   # token was for the old URL
                        await client.ensure_auth()
                        url = await self._create_link_with_backoff(client, plan, link_kind,
                                                                   item_name, mac_row)
                        # getattr, not the attribute: a stand-in client (the test
                        # doubles, and anything else that grows into this slot)
                        # owes us a URL, not this hand-off field.
                        repair = getattr(client, "last_cmd_repair", None)
                    except PortalError as exc:
                        await db_log("WARNING", "stream",
                                     f"[{item_name}] redirect: {portal.name}/{mac_row.mac}: "
                                     f"{exc.detail()} -> next")
                        note_candidate_failure(route_key, _src, mac_row)
                        if exc.code in SLOT_BUSY_CODES:
                            continue          # busy stays "busy" for the 503
                        every_candidate_busy = False
                        if exc.mac_suspect:
                            continue
                        self.route_health.failed(_src)
                        break
                    except Exception as exc:  # noqa: BLE001
                        await db_log("WARNING", "stream",
                                     f"[{item_name}] redirect: {portal.name}/{mac_row.mac}: "
                                     f"{type(exc).__name__}: {exc} -> next")
                        note_candidate_failure(route_key, _src, mac_row)
                        every_candidate_busy = False
                        # Transport errors are source failures too. Keep trying
                        # other MACs/sources for this request, but make subsequent
                        # starts skip a source that just timed out.
                        self.route_health.failed(_src)
                        continue
                    finally:
                        await client.close()
                    if url:
                        # >>> redirect-guard (features 1+2; delete with app/services/redirect_guard.py)
                        _probe = await link_is_alive(url)
                        if not _probe:
                            await db_log("WARNING", "stream",
                                         f"[{item_name}] redirect: fresh link dead "
                                         f"({portal.name}/{mac_row.mac}; {_probe.detail}) "
                                         f"-> next candidate")
                            note_candidate_failure(route_key, _src, mac_row)
                            every_candidate_busy = False
                            continue
                        note_handed_out(route_key, _src, mac_row)
                        _note = shrug_note(url, _probe)
                        if _note:
                            await db_log("INFO", "stream",
                                         f"[{item_name}] redirect: {_note}")
                        # <<< redirect-guard
                        if repair is not None:
                            await _store_media_cmd(_src, repair, item_name)
                        note_link(kind, ref_id, getattr(mac_row, "id", None), url,
                                  requester)
                        clear_candidate_failures(route_key)
                        await db_log("INFO", "stream",
                                     f"[{item_name}] redirecting to {portal.name}/{mac_row.mac} "
                                     f"(no ffmpeg)")
                        if mac_row is not None:
                            self.lease_mac(mac_row.id, holder=requester,
                                           item=item_name, kind=kind, ref=ref_id)
                        self.route_health.succeeded(route_key, _src, mac_row)
                        if out is not None:
                            out["portal"] = portal.name
                            out["mac"] = getattr(mac_row, "mac", "") or ""
                        return url, item_name
                    # A portal that answered with no URL at all is not a busy
                    # slot: this source has nothing to play, and the answer must
                    # say that (502) rather than "retry in a second" (503).
                    note_candidate_failure(route_key, _src, mac_row)
                    every_candidate_busy = False
        if out is not None:
            out["busy"] = bool(chain) and every_candidate_busy
        busy_note = (" (every candidate was busy - not a dead source)"
                     if (out is not None and out.get("busy")) else "")
        await db_log("ERROR", "stream",
                     f"[{item_name}] redirect failed: no source produced a link{busy_note}")
        return None, item_name

    async def open(self, kind: str, ref_id: int, user_name: str | None,
                 force_proxy: bool = False) -> tuple[StreamHandle, object]:
        """
        Build fallback chain for a playlist item and return
        (handle, async generator yielding mpegts bytes).

        `force_proxy` is the explicit `?mode=proxy` URL flag (or an Enigma2
        profile with delivery=proxy): the caller already decided against the
        302, so an item assigned the redirect marker is proxied as a plain
        MPEG-TS copy instead of dying in _spawn with a 502.
        """
        chain: list = []
        tpl_name = "(default)"
        if kind == "live":
            chain, item_name, item = await self._live_chain(ref_id)
        elif kind == "vod":
            chain, item_name, item = await self._vod_chain(ref_id)
        elif kind == "episode":
            got = await self._episode_target(ref_id)
            if not got:
                chain, item_name, item = [], "episode", None
            else:
                chain, item_name, item = got
        elif kind == "local":
            path, item_name = await self.local_disk_path(ref_id)
            item = await self._item_for("local", ref_id)
            chain = [("local", path)] if path else []
        else:
            raise ValueError(f"unknown kind {kind}")

        tpl_name, command = await self._template_for(
            item, kind=kind, user_name=user_name)
        if force_proxy and kind != "local" \
                and (command or "").strip() == REDIRECT_COMMAND:
            tpl_name, command = await self._copy_fallback_command()
            tpl_name = f"{tpl_name} (proxy override)"
        # Local files never 302 (the client cannot see our disk). The default
        # template is the redirect marker, which is not an ffmpeg command: when
        # we reach the pipe (Enigma2 asked for `.ts`, not the original MP4)
        # remux to MPEG-TS with Annex-B instead of dying in _spawn.
        if kind == "local" and (command or "").strip() == REDIRECT_COMMAND:
            tpl_name = "(local mpegts remux)"
            command = mpegts_copy_command()
        handle = StreamHandle(id=uuid.uuid4().hex, kind=kind, item_name=item_name,
                              user_name=user_name, template_name=tpl_name, command=command,
                              route_key=(kind, ref_id))
        # The engine's own start budget - what the output guard waits for before
        # it declares the pipe dead (see STREAM_START_BUDGET). A fixed guard
        # shorter than the chain it guards turns "walking the fallbacks" into a
        # 502 while the engine is still working.
        handle.start_budget = self.start_budget(chain, kind=kind)
        # A zap back inside LINGER_S: this user's previous pipe for this very
        # item may still be running, holding its MAC. Attaching to it skips the
        # create_link, the process start and the panel's slot accounting - the
        # bytes are already flowing. Deliberately before every "is the MAC free"
        # check: the MAC is not free, it is OURS.
        parked = self._find_parked(kind, ref_id, user_name)
        if parked is not None and kind != "local":
            # The mechanical attach happens in the pump (it owns the reader);
            # here it is only announced.
            await db_log("INFO", "stream",
                         f"[{parked.item_name}] zap back within {LINGER_S:.0f}s -> "
                         f"attaching to the pipe that is still running "
                         f"(no create_link, no ffmpeg start)")
            return parked, self._pump(parked, chain, kind, adopt=True)
        # Pre-check: empty chain or EVERY mac currently occupied -> fail fast
        # with 404 instead of hanging a client with a 200 + empty body.
        if kind == "local":
            if not chain:
                await db_log("ERROR", "stream",
                             f"[{item_name}] local file missing on disk -> 404")
                handle.dead = True
        else:
            self._note_chain_limits(chain)
            # `requester=user_name`: a lease this same user took is the channel
            # the box just left, not somebody else's stream (see is_mac_busy).
            #
            # Never veto a zap with our own bookkeeping (see BUSY_WAIT_S): wait
            # for a MAC and take back this user's previous stream if that is
            # what is holding it. Only when nothing frees within the budget does
            # this fail - and then it fails as *busy* (503 + Retry-After), not
            # as "no source" (404), because the source is fine.
            free = any(not self.is_mac_busy(m.id, requester=user_name)
                       for (_s, _p, macs) in chain for m in macs)
            if chain and not free:
                # Courtesy, not reservation: a pipe we parked for its own client
                # cannot make this one wait (or answer 503) while it idles.
                if await self._drop_parked(chain):
                    free = any(not self.is_mac_busy(m.id, requester=user_name)
                               for (_s, _p, macs) in chain for m in macs)
            if chain and not free:
                got = await self._first_free_mac(chain, user_name)
                free = got is not None
            if not chain or not free:
                if chain and not free:
                    busy = "; ".join(self._occupied_note(m) for (_s, _p, macs) in chain
                                     for m in macs)
                    await db_log("WARNING", "stream",
                                 f"[{item_name}] every MAC still busy after "
                                 f"{BUSY_WAIT_S:.1f}s -> 503 (retry shortly)"
                                 + (f" | {busy}" if busy else ""))
                    handle.busy = True
                if not chain:
                    await db_log("ERROR", "stream",
                                 f"[{item_name}] no usable sources (empty fallback chain / portal disabled / no MAC)")
                handle.dead = True

        if handle.dead:
            async def empty():
                return
                yield b""                                # pragma: no cover
            return handle, empty()

        gen = self._pump(handle, chain, kind)
        return handle, gen

    async def _pump(self, h: StreamHandle, chain: list, kind: str,
                    adopt: bool = False):
        registered = False
        try:
            if adopt and h.proc is not None and h.proc.returncode is None:
                # Attach: the pipe is still running (see LINGER_S). Buffered
                # bytes first, so the player sees no gap, then live bytes. On
                # EOF the normal machinery below takes over - the chain is
                # re-walked and the stream restarts in this same response.
                proc = h.proc
                await self._adopt(h)
                await db_log("INFO", "stream",
                             f"[{h.item_name}] attached to the running pipe "
                             f"({h.bytes_sent / 1e6:.1f} MB streamed so far)")
                buffered = bytes(h.ring)
                h.ring.clear()
                if buffered:
                    h.bytes_sent += len(buffered)
                    yield buffered
                async for chunk in self._read_proc(h, proc):
                    yield chunk
                locked = self._lock_of(h)
                if locked is not None:
                    self.unlock_mac(locked, h.id)
                await self._kill_quiet(proc)
                if not h.dead:
                    await db_log("WARNING", "stream",
                                 f"[{h.item_name}] attached stream ended"
                                 f" -> re-resolving")
            if kind == "local":
                if not chain:
                    return
                _tag, path = chain[0]
                try:
                    proc = await self._spawn(h.command, path, h.item_name, pace=True)
                except FFmpegTemplateError as exc:
                    h.dead = True
                    h.fail_note = f"invalid FFmpeg template: {exc}"
                    await db_log("ERROR", "stream",
                                 f"[{h.item_name}] {h.fail_note} -> no fallback")
                    return
                if proc is None:
                    h.dead = True
                    h.fail_note = "FFmpeg could not be spawned"
                    return
                h.url, h.proc = path, proc
                await self._register(h)
                registered = True
                first = await self._first_bytes(proc)
                if not first:
                    stalled = proc.returncode is None
                    h.note_attempt("local file: "
                                   + (f"silent {STREAM_START_TIMEOUT:.0f}s" if stalled
                                      else f"ffmpeg rc={proc.returncode}"))
                    if stalled:
                        # ffmpeg is still running, just silent: slow storage
                        # (disk spin-up, network mount) or a file its demuxer
                        # chews on. The guard upstream reports the 502.
                        await db_log("WARNING", "stream",
                                     f"[{h.item_name}] no data within "
                                     f"{STREAM_START_TIMEOUT:.0f}s from local file "
                                     f"(template '{h.template_name}'); ffmpeg still "
                                     "running - slow storage or unparseable file")
                    else:
                        # ffmpeg is gone: it died AT OUTPUT INIT. Its own error
                        # (unsupported codec in MPEG-TS, bad bitstream filter,
                        # ...) is in the [ffmpeg] stderr tail right above.
                        await db_log("WARNING", "stream",
                                     f"[{h.item_name}] ffmpeg exited "
                                     f"rc={proc.returncode} before sending data for "
                                     f"local file (template '{h.template_name}') - "
                                     "see the [ffmpeg] log entry for its error")
                    await self._kill_quiet(proc)
                    if stalled or proc.returncode == 0:
                        # Killed (-9) and clean-exit (0) processes are the two
                        # cases _drain_stderr deliberately does NOT report
                        # (it would yell on every user stop / normal EOF). But
                        # here the process never sent a single byte, so whatever
                        # it had to say IS the diagnosis - VAAPI init hanging,
                        # "moov atom not found", demuxer errors on a broken
                        # file. Log it.
                        tail = self._stderr_tail(proc)
                        if tail:
                            await db_log("WARNING", "stream",
                                         f"[{h.item_name}] ffmpeg's last words: {tail}")
                    return
                yield first
                async for chunk in self._read_proc(h, proc):
                    yield chunk
                return

            configured_count = len(chain)
            chain = self.route_health.ordered_chain(h.route_key, chain)
            if len(chain) < configured_count:
                await db_log("INFO", "stream",
                             f"[{h.item_name}] circuit breaker skipped "
                             f"{configured_count - len(chain)} cooling source(s)")
            from .runtime_settings import prefer_free_mac
            prefer_free = await prefer_free_mac()
            yielded_any = False
            ref_id = h.route_key[1] if h.route_key else 0   # for the link cache
            busy_skips = 0            # candidates refused by occupancy alone
            other_failures = 0        # candidates that failed for a real reason
            attempts = 2 if (ZAP_RETRY and chain) else 1
            restarts = MIDSTREAM_RESTARTS if kind in MIDSTREAM_RESTART_KINDS else 0
            last_used: tuple | None = None      # (src, mac_row) that was playing
            # The engine's own deadline: STREAM_START_TIMEOUT per candidate is
            # only honest while the whole walk fits in one budget. Past it the
            # request answers "no data" promptly and the output guard turns that
            # into a 502 that names what was tried - better than a player
            # hanging on a chain that has four more silent MACs to go.
            deadline = (time.monotonic() + h.start_budget) if h.start_budget > 0 else None

            def _budget_spent() -> bool:
                return bool(deadline and time.monotonic() >= deadline)

            async def _give_up() -> None:
                h.fail_note = (f"start budget of {h.start_budget:g}s spent after "
                               f"{len(h.attempts)} attempt(s)")
                await db_log("WARNING", "stream",
                             f"[{h.item_name}] {h.fail_note}"
                             + (f" - {h.trace}" if h.trace else "")
                             + " -> giving the player an answer instead of a hang")

            tried_any = False
            while True:
              for pass_no in range(attempts):
                  if pass_no == 1:
                      if _budget_spent():
                          await _give_up()
                          return
                      await db_log("INFO", "stream",
                                   f"[{h.item_name}] first pass produced no data "
                                   f"-> retrying once in {ZAP_RETRY_DELAY:.1f}s (zap overlap?)")
                      await asyncio.sleep(ZAP_RETRY_DELAY)
                  # A (source, MAC) that failed a moment ago goes last: route
                  # affinity must not re-pick the candidate the player just saw
                  # fail (STB-Proxy's `moveMac`, per route and per process).
                  chain = demote_failed_candidates(h.route_key, chain)
                  # Walk the MACs and take the first FREE one (STB-Proxy's
                  # `isMacFree()` loop): waiting per busy MAC would cost
                  # BUSY_WAIT_S for every MAC that is not free - on a two-MAC
                  # portal with the first one busy that is seconds of nothing
                  # while a working MAC sits in the same chain. Only when nothing
                  # is free (or the free ones already failed on the first pass)
                  # is waiting worth anything - and only then is our own previous
                  # stream taken back. See BUSY_WAIT_S / preempt_own.
                  if not (prefer_free and pass_no == 0
                          and self._any_free(chain, h.user_name)):
                      if await self._first_free_mac(chain, h.user_name) is None:
                          await db_log("INFO", "stream",
                                       f"[{h.item_name}] every MAC is held (by this user's "
                                       f"previous play or by somebody else) - waited "
                                       f"{BUSY_WAIT_S:.0f}s for one to free")
                  for idx, (src, portal, macs) in enumerate(chain, 1):
                      if h.dead:
                          return
                      candidates = self._macs_for(portal, src, macs)
                      candidates = self.route_health.ordered_macs(h.route_key, src, candidates)
                      candidates = demote_macs(h.route_key, src, candidates)
                      if prefer_free:
                          candidates = self.order_by_free(h.route_key, src, candidates,
                                                          h.user_name)
                      for mac_row in candidates:
                          if h.dead:
                              return
                          if _budget_spent():
                              await _give_up()
                              return
                          first_candidate = not tried_any
                          tried_any = True
                          # Hedge by patience, not by a parallel race: with a free
                          # alternative in the chain, a candidate that has said
                          # nothing after HEDGE_AFTER_S is not "slow", it is
                          # probably dead - so do not sit on it for the full
                          # window. See HEDGE_AFTER_S.
                          window = (STREAM_START_TIMEOUT if first_candidate
                                    else STREAM_START_TIMEOUT_REST)
                          if (HEDGE_AFTER_S > 0 and h.kind in HEDGE_KINDS
                                  and not first_candidate
                                  and self._free_candidates(chain, h.user_name)
                                  >= HEDGE_MIN_CANDIDATES):
                              window = min(window, HEDGE_AFTER_S)
                          # Decided before the portal is touched, for the same reason the
                          # redirect path decides first: for a source the user adopted onto
                          # the panel's Xtream side (R7) there is no MAC to spend and no
                          # session to open, and reaching for a client "just in case"
                          # would put the portal back in the loop we removed.
                          plan = self._plan(src, mac_row, portal, ffmpeg=True)
                          adopted = plan.adopted
                          # `requester=h.user_name`: this user's own post-302 lease is
                          # the channel the box just zapped away from, not another
                          # viewer - taking it back is what keeps a single-MAC zap on
                          # the MAC that actually works instead of a worse one.
                          if not adopted and self.is_mac_busy(mac_row.id,
                                                              requester=h.user_name):
                              # Occupancy never vetoes a start (see BUSY_WAIT_S and
                              # `preempt_own`): the chain was walked for a free MAC
                              # above, our own previous stream was taken back if
                              # nothing was free, and this candidate is what is
                              # left. Move on to the next one - never block here.
                              h.note_attempt(f"{mac_row.mac}: busy "
                                             f"({self._occupied_note(mac_row) or 'unknown'})")
                              note_candidate_failure(h.route_key, src, mac_row)
                              busy_skips += 1
                              await db_log("INFO", "stream",
                                           f"[{h.item_name}] mac {mac_row.mac} busy -> next "
                                           f"(fallback step {idx}/{len(chain)})")
                              continue
                          if not adopted and self.lease_holder(mac_row.id) == h.user_name \
                                  and h.user_name:
                              h.took_over_lease = True
                              await db_log("INFO", "stream",
                                           f"[{h.item_name}] taking over the redirect lease "
                                           f"on {mac_row.mac} held by {h.user_name} "
                                           "(the channel this zap left)")
                          await db_log("INFO", "stream",
                                       f"[{h.item_name}] fallback step {idx}/{len(chain)}: "
                                       + (f"portal '{portal.name}' - {plan.policy.reason}" if adopted
                                          else f"portal '{portal.name}' mac {mac_row.mac}"))
                          url = None
                          repair = None      # set only when the panel answered
                          if adopted:
                              url = plan.direct_url
                          elif plan.policy.direct:
                              # R2b: the channel's own flags say its link is
                              # permanent, so ffmpeg gets the stored URL - no
                              # handshake, no create_link, no panel slot. That is
                              # what STB-Proxy does for every channel whose cmd is
                              # not `http://localhost/...`, and it is why a zap
                              # there never collides with the panel's connection
                              # table. `link_policy` has already refused this path
                              # for tmp/load-balanced links, templates and URLs
                              # that still carry a session token.
                              url = plan.direct_url
                              await db_log("INFO", "stream",
                                           f"[{h.item_name}] playing the stored link via "
                                           f"{portal.name}/{mac_row.mac if mac_row else 'xtream'}"
                                           f" (ffmpeg): {plan.policy.reason}")
                          else:
                              client = await POOL.get(PortalSession.from_rows(portal, mac_row))
                              repair = None
                              try:
                                  if not portal.resolved_url:
                                      from ..portal.resolver import resolve_portal  # local import: avoids cycle
                                      res = await resolve_portal(portal.base_url, mac=mac_row.mac,
                                                                 proxy=portal.proxy_url,
                                                                 tls_insecure=portal.tls_insecure)
                                      if res.ok:
                                          portal.resolved_url = res.portal_url
                                          portal.resolved_path = res.path
                                          await _store_resolved_portal(portal.id, res.portal_url, res.path)
                                          client.portal_url = res.portal_url
                                          client.invalidate()   # token was for the old URL
                                  await client.ensure_auth()
                                  link_kind = "live" if kind == "live" else "vod"
                                  # The plan asked for a link (the flags say the URL
                                  # is not permanent, or the stored cmd is a template
                                  # the panel must finish). `_create_link_with_backoff`
                                  # keeps asking the SAME MAC while the panel reports
                                  # a busy slot instead of burning the candidate: that
                                  # is the zap overlap, and on a single-MAC portal it
                                  # is the only candidate there is.
                                  url = await self._create_link_with_backoff(
                                      client, plan, link_kind, h.item_name, mac_row)
                                  repair = getattr(client, "last_cmd_repair", None)
                              except PortalError as exc:
                                  # The code decides what this means for the rest of the
                                  # chain: `limit` is "this MAC is busy over there", so
                                  # the next MAC is the right move, while `nothing_to_play`
                                  # is "this source is dead", so hopping MACs is pointless.
                                  h.note_attempt(f"{portal.name}/{mac_row.mac}: {exc.code or exc}")
                                  note_candidate_failure(h.route_key, src, mac_row)
                                  if exc.code in SLOT_BUSY_CODES:
                                      busy_skips += 1
                                  else:
                                      other_failures += 1
                                  await db_log("WARNING", "stream",
                                               f"[{h.item_name}] {portal.name}/{mac_row.mac}: "
                                               f"{exc.detail()}"
                                               f"{' -> next mac' if exc.mac_suspect else ' -> next'}")
                                  if exc.mac_suspect:
                                      continue
                                  self.route_health.failed(src)
                                  break  # source-specific failure: another MAC cannot repair it
                              except Exception as exc:  # noqa: BLE001
                                  h.note_attempt(f"{portal.name}/{mac_row.mac}: "
                                                 f"{type(exc).__name__}")
                                  note_candidate_failure(h.route_key, src, mac_row)
                                  other_failures += 1
                                  await db_log("WARNING", "stream",
                                               f"[{h.item_name}] {portal.name}/{mac_row.mac}: "
                                               f"unexpected {type(exc).__name__}: {exc} -> next")
                                  continue
                              finally:
                                  await client.close()
                          if not url:
                              # An Xtream URL that will not open is not a MAC problem:
                              # the next MAC would be handed exactly the same URL, so
                              # move on to the next source instead of walking the list.
                              h.note_attempt(f"{portal.name}/{mac_row.mac}: no URL")
                              note_candidate_failure(h.route_key, src, mac_row)
                              other_failures += 1
                              if adopted:
                                  break
                              continue
                          if repair is not None:
                              # A form the panel accepted is worth keeping BEFORE the
                              # pipe is opened: if ffmpeg then fails on the bytes, the
                              # next attempt still starts from the cmd that got a link.
                              await _store_media_cmd(src, repair, h.item_name)

                          # lock the MAC BEFORE starting ffmpeg so parallel requests
                          # see it as occupied immediately. An adopted play owns no MAC,
                          # and `locked` is what keeps the three release sites below from
                          # popping a slot that a *different* stream on this MAC is holding.
                          locked = None
                          if not adopted:
                              self.lock_mac(mac_row.id, h.id)
                              locked = mac_row.id
                          # VOD/episode links are FILES (mkv/mp4 over the CDN): pace
                          # them to real time like local files, or the player hits
                          # EOF early. Live is paced by its own encoder - never -re.
                          # The opener walks the media-UA ladder (player identity
                          # first, one browser-UA retry on an HTTP 4xx open error):
                          # play/live.php origins answer the portal browser UA with
                          # HTTP 456/403 and zero bytes while a player-shaped request
                          # plays the very same play_token - see stream_identity.
                          try:
                              proc, first, open_fail = await self._open_with_identity(
                                  h.command, url, title=h.item_name,
                                  pace=(kind != "live"),
                                  # The first candidate gets the full window;
                                  # after it, a silent source is far more likely
                                  # to be dead than slow (measured: a live one
                                  # answers in ~550 ms), and a whole chain of
                                  # 12 s waits is what a player shows as a frozen
                                  # screen. See STREAM_START_TIMEOUT_REST.
                                  first_byte_timeout=window)
                          except FFmpegTemplateError as exc:
                              if locked is not None:
                                  self.unlock_mac(locked, h.id)
                              h.dead = True
                              h.fail_note = f"invalid FFmpeg template: {exc}"
                              h.note_attempt(f"template: {exc}")
                              await db_log("ERROR", "stream",
                                           f"[{h.item_name}] {h.fail_note} -> no MAC/source fallback")
                              return
                          if proc is None:
                              if _is_template_output_failure(open_fail):
                                  tail = (open_fail.get("tail") or "").strip()
                                  detail = tail[-500:] if tail else f"rc={open_fail.get('rc')}"
                                  if locked is not None:
                                      self.unlock_mac(locked, h.id)
                                  h.dead = True
                                  h.fail_note = ("FFmpeg template/output initialization failed "
                                                 f"(rc={open_fail.get('rc')})")
                                  h.note_attempt("template: " + h.fail_note)
                                  await db_log(
                                      "ERROR", "stream",
                                      f"[{h.item_name}] {h.fail_note} -> no MAC/source fallback | "
                                      f"ffmpeg's last words: {detail}")
                                  return
                              if open_fail is not None:
                                  who = portal.name + ("/xtream"
                                                       if adopted
                                                       else f"/{mac_row.mac}")
                                  # The stderr tail is the *only* evidence for a silent
                                  # stall (rc == -9 because we killed it, which is
                                  # deliberately not logged on its own): without it the
                                  # log says "no data within 12s" and leaves the real
                                  # reason - VAAPI init, a 4xx on the media request, a
                                  # panel slot check - to guesswork.
                                  tail = (open_fail.get("tail") or "").strip()
                                  words = f" | ffmpeg's last words: {tail[:400]}" if tail else ""
                                  note_candidate_failure(h.route_key, src, mac_row)
                                  other_failures += 1
                                  if open_fail["stalled"]:
                                      # Name the window that actually expired: it is
                                      # `window`, not the full start timeout, when a
                                      # free alternative was waiting (HEDGE_AFTER_S).
                                      h.note_attempt(f"{who}: silent {window:g}s")
                                      await db_log("WARNING", "stream",
                                                   f"[{h.item_name}] no data within {window:g}s from "
                                                   f"{who} -> fallback{words}")
                                  else:
                                      # ffmpeg is gone and will never send a byte: say so
                                      # (the [ffmpeg] log line has the stderr tail)
                                      h.note_attempt(f"{who}: ffmpeg rc={open_fail['rc']}")
                                      await db_log("WARNING", "stream",
                                                   f"[{h.item_name}] ffmpeg exited rc={open_fail['rc']} before sending "
                                                   f"data ({who}) -> fallback{words}")
                              if locked is not None:
                                  self.unlock_mac(locked, h.id)
                              self.route_health.failed(src)
                              if adopted:
                                  break
                              continue
                          h.portal_name, h.mac, h.url, h.proc = (
                              f"{portal.name} (xtream)" if adopted else portal.name,
                              "" if adopted else mac_row.mac, url, proc)
                          h.portal_id = getattr(portal, "id", None)
                          if not registered:
                              await self._register(h)
                              registered = True
                          self.route_health.succeeded(h.route_key, src, mac_row, verified_media=True)
                          note_link(kind, ref_id, getattr(mac_row, "id", None), url,
                                    h.user_name)
                          clear_candidate_failures(h.route_key)
                          await db_log("INFO", "stream",
                                       f"[{h.item_name}] playing via {portal.name}/"
                                       f"{mac_row.mac if mac_row is not None else 'xtream'} "
                                       f"({'transcode' if ' -c:v copy' not in h.command else 'copy'})")
                          yielded_any = True
                          last_used = (src, mac_row)
                          yield first
                          async for chunk in self._read_proc(h, proc):
                              yield chunk
                          # EOF: stream ended/died -> move to next fallback silently
                          if locked is not None:
                              self.unlock_mac(locked, h.id)
                          await self._kill_quiet(proc)
                          if not h.dead:
                              await db_log("WARNING", "stream",
                                           f"[{h.item_name}] stream ended from {portal.name}/{mac_row.mac}"
                                           f" -> trying next fallback")
                      # next portal in chain
                  if yielded_any:
                      break
              # ---- the chain is exhausted after a stream that WAS playing ----
              if not (yielded_any and not h.dead and restarts > 0) or _budget_spent():
                  break
              restarts -= 1
              # The link we were playing is the one that died, so replaying it
              # from the cache would restart the same corpse: forget every cached
              # link of this route and ask the panel again (that is the point).
              for _s, _p, _macs in chain:
                  for _m in self._macs_for(_p, _s, _macs) or ():
                      drop_link(kind, ref_id, getattr(_m, "id", None), h.user_name)
              if last_used is not None:
                  # A dropped *stream* is a reason to prefer another MAC next time
                  # (STB-Proxy moves the MAC when a stream dies): the panel may
                  # still count the old one's connection for a few seconds.
                  note_candidate_failure(h.route_key, last_used[0], last_used[1])
              await db_log("INFO", "stream",
                           f"[{h.item_name}] live stream ended after "
                           f"{h.bytes_sent / 1e6:.1f} MB -> re-resolving "
                           f"(restart {MIDSTREAM_RESTARTS - restarts}/{MIDSTREAM_RESTARTS})")
              await asyncio.sleep(MIDSTREAM_RESTART_DELAY)
              chain = self.route_health.ordered_chain(h.route_key, chain)
              yielded_any = False
              busy_skips = 0
              other_failures = 0
            if not h.fail_note:
                h.fail_note = (f"{len(h.attempts)} attempt(s) without data"
                               if h.attempts else "no candidate source could be tried")
            if busy_skips and not other_failures:
                # Nothing failed for a real reason: every candidate was refused
                # by occupancy (our pipe/lease) or by the panel's own slot. The
                # route turns this into 503 + Retry-After - the truth is "come
                # back in a second", and a 404 told the player the channel was
                # gone (and made players stop retrying).
                h.busy = True
                h.fail_note += f" (all {busy_skips} refusal(s) were busy slots)"
            if not h.dead:
                # `h.dead` here means somebody killed this stream on purpose
                # (a zap taking over our pipe, the client disconnecting, the
                # dashboard) - that is not "we could not start a stream", and
                # logging it as an exhausted fallback chain made a healthy zap
                # look like a failure in the log.
                await db_log("ERROR", "stream",
                             f"[{h.item_name}] all fallbacks exhausted - {h.fail_note}"
                             + (f" | {h.trace}" if h.trace else ""))
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            import traceback
            await db_log("ERROR", "stream",
                         f"[{h.item_name}] pump crashed: {type(exc).__name__}: {exc}\n"
                         f"{''.join(traceback.format_exception(exc))[-1200:]}")
        finally:
            if registered or h.proc:
                # One shielded unit, not three sequential awaits: this runs
                # while the client's request task is being cancelled, and every
                # step (proc.wait, the active_streams DELETE, the log write)
                # awaits something a cancellation would abort halfway.
                await run_uncancelled(self._finish(h), what="stream teardown")

    async def _finish(self, h: StreamHandle) -> None:
        """Complete stream teardown, run outside the dying request's scope."""
        if h.parked:
            # The parker owns the process now (a client may attach to it); the
            # watchdog already saw the disconnect, and the parker reaps it.
            return
        if self._can_park(h):
            # The client left mid-stream: hold the pipe instead of killing it,
            # so the same player's zap back costs no create_link and no ffmpeg
            # start (LINGER_S). `client_left` is the same decision, reached
            # without a watchdog.
            await run_uncancelled(self._park(h), what="stream parking")
            return
        await self._kill_quiet(h.proc)
        await self._deregister(h)
        await db_log("INFO", "stream",
                     f"[{h.item_name}] stopped after {h.bytes_sent/1e6:.1f} MB")

    def _stall_window(self, h: StreamHandle) -> float:
        """Seconds of silence this stream tolerates before it counts as over.

        A live stream that is *restartable* (the mid-stream re-resolve is on
        for its kind) gets the shorter window: a drop can be continued in the
        same response, so the old 25 s of black screen buys nothing. Anything
        else keeps the generous one - the wait is for a source that is
        starting or buffering, not for a stream we can replace.
        """
        if MIDSTREAM_RESTARTS > 0 and h.kind in MIDSTREAM_RESTART_KINDS:
            return STREAM_STALL_TIMEOUT_LIVE
        return STREAM_STALL_TIMEOUT

    async def _read_proc(self, h: StreamHandle, proc):
        """Yield bytes with stall detection until EOF/death/kill."""
        while not h.dead:
            window = self._stall_window(h)
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(CHUNK), window)
            except asyncio.TimeoutError:
                await db_log("WARNING", "stream",
                             f"[{h.item_name}] stalled >{window:.0f}s without data")
                break
            if not chunk:
                break
            h.bytes_sent += len(chunk)
            yield chunk

    async def purge_runtime_rows(self) -> None:
        """Called at boot: sqlite/postgres may hold rows from a previous life."""
        try:
            async with SessionLocal() as s:
                await s.execute(delete(ActiveStream))
                await s.commit()
        except Exception:  # noqa: BLE001
            pass


MANAGER = StreamManager()
