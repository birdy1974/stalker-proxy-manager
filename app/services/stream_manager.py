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
import os
import re
import logging
import shlex
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select

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
from ..portal.client import MAG_UA, PortalError, is_hls
from ..portal.links import plan_adopted, plan_for
from .db_logging import db_log
from .ffmpeg_templates import (COPY_PRESET_NAME, HLS_ALLOWED_EXTENSIONS,
                               HLS_PROTOCOL_WHITELIST, REDIRECT_COMMAND,
                               URL_PLACEHOLDER, mpegts_copy_command,
                               serves_original_file)
from .probe import media_codecs, subtitle_streams
from .item_info import local_file_path

log = logging.getLogger("spm.stream")

STREAM_STALL_TIMEOUT = 25.0   # seconds without a single byte => dead stream
CHUNK = 64 * 1024
# input options that only exist for network protocols (stripped for file://)
_NETONLY_OPTS = re.compile(
    r"-(?:reconnect\w*|-?rw_timeout|timeout|user_agent|headers|http_proxy"
    r"|seekable|multiple_requests|referer)\b\s*(?:\S+)?")
_NET_SCHEMES = ("http://", "https://")
# Subtitle codecs a transcoded MPEG-TS pipe can keep: bitmap ones survive a
# re-encode into the DVB track of the output TS. Text subs (SRT/ASS/...) can
# only be burned into the picture - which needs CPU video frames, so with
# hardware-only transcoding there is deliberately nothing text subs can do
# here: they are dropped (safely, by the gate - never by an ffmpeg abort).
_BITMAP_SUB_CODECS = {"dvb_subtitle", "dvd_subtitle", "hdmv_pgs_subtitle", "xsub"}
# The other half of the story: a MATROSKA output (subs="keep", the Enigma2 VOD
# path) carries text subtitles too, so nothing has to be dropped there - the
# tracks are copied byte for byte beside a video pipeline that stays fully
# hardware. Only these few codecs have no place in a Matroska file and would
# abort the muxer, so the gate maps around them.
_MKV_UNSUPPORTED_SUB_CODECS = {"dvb_teletext", "eia_608", "eia_708", "cea_608",
                               "arib_caption", "hdmv_text_subtitle"}
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
    mac: str = ""
    url: str = ""
    bytes_sent: int = 0
    proc: asyncio.subprocess.Process | None = None
    dead: bool = False

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "item_name": self.item_name,
                "user_name": self.user_name, "portal_name": self.portal_name,
                "mac": self.mac, "template_name": self.template_name,
                "started": self.started, "bytes_sent": self.bytes_sent,
                "url": self.url, "pid": self.proc.pid if self.proc else None}


# How long a handle may sit in the registry with its ffmpeg process gone and no
# new bytes before the reaper concludes the teardown was lost and frees it.
# Must exceed STREAM_START_TIMEOUT + the stall window, otherwise the reaper
# would kill a stream that is legitimately walking its fallback chain.
REAP_GRACE = 45.0


# After a 302 redirect we no longer hold the socket, so we cannot know when the
# player stops. create_link itself often opens a panel slot though, and a health
# handshake on that same MAC mid-play can kick the viewer (or burn the slot).
# A soft lease covers the typical live-zap window; VOD leases outlive a movie
# only if the operator re-asks within the window, which is rare and harmless.
REDIRECT_LEASE_S = 180.0


class StreamManager:
    def __init__(self) -> None:
        self.streams: dict[str, StreamHandle] = {}
        self.mac_locks: dict[int, str] = {}                 # mac_id -> stream_id (ffmpeg pipes)
        # Soft occupancy for redirect/direct plays: mac_id -> monotonic expiry.
        # See REDIRECT_LEASE_S. Expired entries are dropped lazily on read.
        self.redirect_leases: dict[int, float] = {}
        self._watchers: set[asyncio.Task] = set()           # strong refs, see watch()
        self._proc_gone_since: dict[str, float] = {}        # stream_id -> first seen

    # ------------------------------------------------------------- occupancy
    def is_mac_busy(self, mac_id: int | None) -> bool:
        """True when a MAC must not be re-handshaked or handed to another play.

        Two independent signals:
          * ``mac_locks`` — an ffmpeg pipe we own (proxy/transcode). Released
            the moment the pump ends or the disconnect watchdog fires.
          * ``redirect_leases`` — we just 302'd a player to the panel CDN with
            this MAC's create_link token. We no longer see the socket, so the
            lease is time-bounded rather than exact.
        """
        if mac_id is None:
            return False
        if mac_id in self.mac_locks:
            return True
        exp = self.redirect_leases.get(mac_id)
        if exp is None:
            return False
        if exp <= time.monotonic():
            self.redirect_leases.pop(mac_id, None)
            return False
        return True

    def lease_mac(self, mac_id: int | None, *, seconds: float = REDIRECT_LEASE_S) -> None:
        """Mark a MAC busy for a short window after a redirect/direct resolve."""
        if mac_id is None:
            return
        self.redirect_leases[mac_id] = time.monotonic() + max(1.0, float(seconds))

    def release_mac(self, mac_id: int | None) -> None:
        """Drop every occupancy record for one MAC (delete/edit cleanup)."""
        if mac_id is None:
            return
        self.mac_locks.pop(mac_id, None)
        self.redirect_leases.pop(mac_id, None)

    def release_macs(self, mac_ids) -> None:
        for mid in mac_ids or ():
            self.release_mac(mid)

    def busy_mac_ids(self) -> set[int]:
        """mac_ids currently locked by ffmpeg or holding a live redirect lease."""
        now = time.monotonic()
        expired = [mid for mid, exp in self.redirect_leases.items() if exp <= now]
        for mid in expired:
            self.redirect_leases.pop(mid, None)
        return set(self.mac_locks) | set(self.redirect_leases)

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
        return sum(1 for h in self.streams.values() if (h.user_name or "-") == key)

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
        for mac_id, sid in list(self.mac_locks.items()):
            if sid == h.id:
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
                    await db_log("INFO", "stream",
                                 f"client left '{handle.item_name}' -> killing stream")
                    await self.kill(handle.id)
                    return
                await asyncio.sleep(interval)
        except Exception:  # noqa: BLE001 - watchdog must never crash the app
            log.exception("disconnect watchdog crashed for %s", handle.id)

    # --------------------------------------------------------- ffmpeg spawn
    @staticmethod
    def _network_input_options(cmd_text: str, url: str) -> str:
        """
        Give ffmpeg the identity of the STB it is impersonating, and the input
        options the resolved link itself requires.

        Identity: ffmpeg announces itself as "Lavf/61.x" and sends no Referer;
        plenty of Stalker panels - and the CDNs in front of them - answer that
        with 403 or 405 ("Method Not Allowed") on an otherwise perfectly valid
        link.

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
            add.append(f'-user_agent "{MAG_UA}"')
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

    async def _first_bytes(self, proc) -> bytes:
        """
        Wait for the first chunk - but only until ffmpeg dies, not until the
        start timeout expires. A process that exits before sending a byte will
        never send one, so falling back immediately is both faster (no 12 s
        wait per dead source) and honest in the log.
        """
        read_t = asyncio.ensure_future(proc.stdout.read(CHUNK))
        exit_t = asyncio.ensure_future(proc.wait())
        try:
            done, _pending = await asyncio.wait(
                {read_t, exit_t}, timeout=STREAM_START_TIMEOUT,
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
                     pace: bool = False) -> list[str] | None:
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
            cmd_template.replace(URL_PLACEHOLDER, insert), url)
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
        # Inject metadata title before the output format specifier so players
        # display the correct stream name instead of source stream metadata
        if title:
            try:
                # Find the last -i (input marker) to locate the output section
                last_i = max(idx for idx, a in enumerate(args) if a == "-i")
                # Find -f in the output section (after the last -i)
                f_idx = args.index("-f", last_i)
                # Insert metadata before -f
                args[f_idx:f_idx] = ["-metadata", f"title={title}"]
            except (ValueError, IndexError):
                # No -f found, insert before the last argument (the output)
                if len(args) > 1:
                    args[-1:-1] = ["-metadata", f"title={title}"]
        return args

    async def _subs_gate(self, args: list[str], url: str, pace: bool,
                         name: str = "") -> list[str]:
        """
        Per-source reality check for the dvb subtitle intent.

        The template says what it WANTS; whether the source can deliver it is
        a property of the file/link being played. The gate probes the source
        once (8 s cap, 10 min cache keyed without the per-play token) and
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
            subs = await subtitle_streams(url, is_url=is_net)
            if not subs:                   # None (probe failed) or [] (no subs)
                return args
            bad = [s["codec"] for s in subs if s["codec"] in _MKV_UNSUPPORTED_SUB_CODECS]
            if not bad:
                return args
            idxs = [n for n, s in enumerate(subs)
                    if s["codec"] not in _MKV_UNSUPPORTED_SUB_CODECS][:16]
            if not idxs:
                await db_log("INFO", "stream", tag +
                             "no Matroska-compatible subtitle track ("
                             + ", ".join(bad) + ") -> subtitles dropped")
                return StreamManager._ensure_sn(StreamManager._strip_subs(args))
            await db_log("INFO", "stream", tag +
                         "subtitles copied to Matroska, skipping "
                         + ", ".join(bad))
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
        idxs = [n for n, s in enumerate(subs) if s["codec"] in _BITMAP_SUB_CODECS][:8]
        if not idxs:
            await db_log("INFO", "stream", tag +
                         "no convertible (bitmap) subtitle track ("
                         + ", ".join(s["codec"] for s in subs) + ")"
                         " -> subtitles dropped")
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

    async def _spawn(self, cmd_template: str, url: str, title: str | None = None,
                     pace: bool = False) -> asyncio.subprocess.Process | None:
        if (cmd_template or "").strip() == REDIRECT_COMMAND:
            await db_log("ERROR", "stream",
                         "cannot spawn the redirect template as ffmpeg "
                         "(local files must be served directly)")
            return None
        if "<out_dir>" in (cmd_template or ""):
            await db_log("ERROR", "stream", "template uses HLS file output; use mpegts for live proxying")
            return None
        args = self._ffmpeg_argv(cmd_template, url, title, pace)
        if not args:
            await db_log("ERROR", "stream", "unparseable ffmpeg template")
            return None
        args = await self._remux_gate(args, url, pace, title or "")
        args = await self._subs_gate(args, url, pace, title or "")
        # Log the full command for debugging
        await db_log("DEBUG", "ffmpeg", f"spawn command: {' '.join(args)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            asyncio.get_running_loop().create_task(self._drain_stderr(proc))
            return proc
        except FileNotFoundError:
            await db_log("ERROR", "stream", f"ffmpeg binary not found: {args[0]}")
            return None

    async def _drain_stderr(self, proc) -> None:
        """Keep stderr from blocking; last lines are logged on failure."""
        lines: list[bytes] = []
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
            async with SessionLocal() as s:
                copy_tpl = (await s.execute(select(FFmpegTemplate).where(
                    FFmpegTemplate.name == COPY_PRESET_NAME,
                    FFmpegTemplate.enabled.is_(True)))).scalar_one_or_none()
            if copy_tpl is not None:
                tpl_name, command = copy_tpl.name, copy_tpl.command
            else:
                tpl_name = "(copy)"
                command = f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"
        h = StreamHandle(id=uuid.uuid4().hex, kind="preview",
                         item_name=name or getattr(src, "original_name", None)
                         or getattr(src, "name", "preview"),
                         user_name="admin", template_name=tpl_name, command=command)
        gen = self._pump(h, [(src, portal, macs)], "live" if kind == "live" else "vod")
        return h, gen

    async def _template_for(self, item) -> tuple[str, str]:
        """(template name, command with <url>) - default template as fallback."""
        async with SessionLocal() as s:
            tpl = None
            tid = getattr(item, "ffmpeg_template_id", None) if item is not None else None
            if tid:
                tpl = await s.get(FFmpegTemplate, tid)
            if tpl is None or not tpl.enabled:
                tpl = (await s.execute(select(FFmpegTemplate).where(
                    FFmpegTemplate.is_default.is_(True)))).scalar_one_or_none()
            if tpl is None:
                tpl = (await s.execute(select(FFmpegTemplate).limit(1))).scalar_one_or_none()
            if tpl is None:
                return "(pass)", f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"
            return tpl.name, (tpl.command or f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1")

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

    async def uses_redirect(self, kind: str, ref_id: int) -> bool:
        """True when the item's effective template is the redirect/bypass preset.

        This backs the per-channel "bypass ffmpeg" mode: a playlist item whose
        FFmpeg template is `REDIRECT_PRESET_NAME` is 302'd straight to the
        panel's CDN instead of being proxied through ffmpeg. The redirect
        preset is ALSO the built-in default template, so an item without an
        explicit template assignment resolves to it and redirects too - the old
        global switch, expressed as a template.
        """
        item = await self._item_for(kind, ref_id)
        if item is None:
            return False
        _name, command = await self._template_for(item)
        return command == REDIRECT_COMMAND

    async def local_disk_path(self, ref_id: int) -> tuple[str | None, str]:
        """Absolute path of a local playlist item, or (None, name) if missing."""
        async with SessionLocal() as s:
            lp = await s.get(LocalPlaylist, ref_id)
            lf = await s.get(LocalFile, lp.local_file_id) if lp else None
            ls = await s.get(LocalSource, lf.local_source_id) if lf else None
        name = (lp.custom_name or lf.filename) if lp and lf else "local file"
        if not (lf and ls):
            return None, name
        path = local_file_path(ls.directory, lf.relative_path)
        if path and os.path.isfile(path):
            return path, name
        return None, name

    async def local_serves_original(self, ref_id: int) -> bool:
        """True when this local item should be FileResponse'd, not ffmpeg'd."""
        item = await self._item_for("local", ref_id)
        _name, command = await self._template_for(item)
        return serves_original_file(command)

    async def register_local_file(self, ref_id: int, user_name: str | None,
                                  path: str, item_name: str) -> StreamHandle:
        """Dashboard/max-connections slot for a direct file serve (no ffmpeg)."""
        item = await self._item_for("local", ref_id)
        tpl_name, _command = await self._template_for(item)
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

    # ------------------------------------------------------------ the pump
    async def resolve(self, kind: str, ref_id: int) -> tuple[str | None, str]:
        """
        Resolve a playable portal URL WITHOUT starting ffmpeg.

        This backs the "redirect" output mode: the player is sent straight to
        the panel's CDN, so there is no transcode, no container CPU and no
        ffmpeg start-up delay - the answer to "VAAPI takes three minutes to
        start" and to "copy does not work" on panels whose stream ffmpeg will
        not remux. The trade-off is that we cannot rewrite the transport
        stream, and a link that dies mid-playback is not retried (the player
        sees EOF instead of our fallback chain).

        Returns (url, item_name); url is None when nothing resolved.
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

        for _src, portal, macs in chain:
            for mac_row in self._macs_for(portal, _src, macs):
                # ffmpeg lock OR a recent redirect lease — both mean "leave this
                # MAC alone". Redirects never enter mac_locks (we no longer hold
                # the socket after the 302), so the lease is the only signal.
                if mac_row is not None and self.is_mac_busy(mac_row.id):
                    continue
                # Decided first, before any portal session exists: the point of
                # R2 is that a channel the panel described as permanent costs the
                # player one redirect and us *nothing* - no handshake reuse, no
                # token, no create_link. The old shape paid for all of that and
                # then threw the answer away in favour of the stored URL anyway.
                plan = self._plan(_src, mac_row, portal)
                if plan.policy.direct:
                    await db_log("INFO", "stream",
                                 f"[{item_name}] playing the stored link via "
                                 f"{portal.name}/{mac_row.mac}: {plan.policy.reason}")
                    if mac_row is not None:
                        self.lease_mac(mac_row.id)
                    return plan.direct_url, item_name
                client = await POOL.get(PortalSession.from_rows(portal, mac_row))
                try:
                    if not portal.resolved_url:
                        from ..portal.resolver import resolve_portal
                        res = await resolve_portal(portal.base_url, mac=mac_row.mac,
                                                  proxy=portal.proxy_url,
                                                  tls_insecure=portal.tls_insecure)
                        if res.ok:
                            portal.resolved_url = res.portal_url
                            client.portal_url = res.portal_url
                            client.invalidate()   # token was for the old URL
                    await client.ensure_auth()
                    url = await client.create_link(plan.cmd, link_kind,
                                                   **plan.request_kwargs())
                except PortalError as exc:
                    await db_log("WARNING", "stream",
                                 f"[{item_name}] redirect: {portal.name}/{mac_row.mac}: "
                                 f"{exc.detail()} -> next")
                    continue
                except Exception as exc:  # noqa: BLE001
                    await db_log("WARNING", "stream",
                                 f"[{item_name}] redirect: {portal.name}/{mac_row.mac}: "
                                 f"{type(exc).__name__}: {exc} -> next")
                    continue
                finally:
                    await client.close()
                if url:
                    await db_log("INFO", "stream",
                                 f"[{item_name}] redirecting to {portal.name}/{mac_row.mac} "
                                 f"(no ffmpeg)")
                    if mac_row is not None:
                        self.lease_mac(mac_row.id)
                    return url, item_name
        await db_log("ERROR", "stream",
                     f"[{item_name}] redirect failed: no source produced a link")
        return None, item_name

    async def open(self, kind: str, ref_id: int, user_name: str | None) -> tuple[StreamHandle, object]:
        """
        Build fallback chain for a playlist item and return
        (handle, async generator yielding mpegts bytes).
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

        tpl_name, command = await self._template_for(item)
        # Local files never 302 (the client cannot see our disk). The default
        # template is the redirect marker, which is not an ffmpeg command: when
        # we reach the pipe (Enigma2 asked for `.ts`, not the original MP4)
        # remux to MPEG-TS with Annex-B instead of dying in _spawn.
        if kind == "local" and (command or "").strip() == REDIRECT_COMMAND:
            tpl_name = "(local mpegts remux)"
            command = mpegts_copy_command()
        handle = StreamHandle(id=uuid.uuid4().hex, kind=kind, item_name=item_name,
                              user_name=user_name, template_name=tpl_name, command=command)
        # Pre-check: empty chain or EVERY mac currently occupied -> fail fast
        # with 404 instead of hanging a client with a 200 + empty body.
        if kind == "local":
            if not chain:
                await db_log("ERROR", "stream",
                             f"[{item_name}] local file missing on disk -> 404")
                handle.dead = True
        else:
            free = any(not self.is_mac_busy(m.id) for (_s, _p, macs) in chain for m in macs)
            if not chain or not free:
                if chain and not free:
                    await db_log("WARNING", "stream",
                                 f"[{item_name}] all MACs occupied -> 404 (try again later)")
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

    async def _pump(self, h: StreamHandle, chain: list, kind: str):
        registered = False
        try:
            if kind == "local":
                if not chain:
                    return
                _tag, path = chain[0]
                proc = await self._spawn(h.command, path, h.item_name, pace=True)
                if proc is None:
                    return
                h.url, h.proc = path, proc
                await self._register(h)
                registered = True
                first = await self._first_bytes(proc)
                if not first:
                    if proc.returncode is None:
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
                    return
                yield first
                async for chunk in self._read_proc(h, proc):
                    yield chunk
                return

            for idx, (src, portal, macs) in enumerate(chain, 1):
                if h.dead:
                    return
                for mac_row in self._macs_for(portal, src, macs):
                    if h.dead:
                        return
                    # Decided before the portal is touched, for the same reason the
                    # redirect path decides first: for a source the user adopted onto
                    # the panel's Xtream side (R7) there is no MAC to spend and no
                    # session to open, and reaching for a client "just in case"
                    # would put the portal back in the loop we removed.
                    plan = self._plan(src, mac_row, portal, ffmpeg=True)
                    adopted = plan.adopted
                    if not adopted and self.is_mac_busy(mac_row.id):
                        await db_log("INFO", "stream",
                                     f"[{h.item_name}] mac {mac_row.mac} busy -> skip "
                                     f"(fallback step {idx}/{len(chain)})")
                        continue
                    await db_log("INFO", "stream",
                                 f"[{h.item_name}] fallback step {idx}/{len(chain)}: "
                                 + (f"portal '{portal.name}' - {plan.policy.reason}" if adopted
                                    else f"portal '{portal.name}' mac {mac_row.mac}"))
                    url = None
                    if adopted:
                        url = plan.direct_url
                    else:
                        client = await POOL.get(PortalSession.from_rows(portal, mac_row))
                        try:
                            if not portal.resolved_url:
                                from ..portal.resolver import resolve_portal  # local import: avoids cycle
                                res = await resolve_portal(portal.base_url, mac=mac_row.mac,
                                                           proxy=portal.proxy_url,
                                                           tls_insecure=portal.tls_insecure)
                                if res.ok:
                                    portal.resolved_url = res.portal_url
                                    client.portal_url = res.portal_url
                                    client.invalidate()   # token was for the old URL
                            await client.ensure_auth()
                            link_kind = "live" if kind == "live" else "vod"
                            # ffmpeg owns this stream, so the plan is always "ask"
                            # (fresh token + the liveness answer); the flags still
                            # decide what we tell the panel about ads and re-checks
                            url = await client.create_link(plan.cmd, link_kind,
                                                           **plan.request_kwargs())
                        except PortalError as exc:
                            # The code decides what this means for the rest of the
                            # chain: `limit` is "this MAC is busy over there", so
                            # the next MAC is the right move, while `nothing_to_play`
                            # is "this source is dead", so hopping MACs is pointless.
                            await db_log("WARNING", "stream",
                                         f"[{h.item_name}] {portal.name}/{mac_row.mac}: "
                                         f"{exc.detail()}"
                                         f"{' -> next mac' if exc.mac_suspect else ' -> next'}")
                            continue
                        except Exception as exc:  # noqa: BLE001
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
                        if adopted:
                            break
                        continue

                    # lock the MAC BEFORE starting ffmpeg so parallel requests
                    # see it as occupied immediately. An adopted play owns no MAC,
                    # and `locked` is what keeps the three release sites below from
                    # popping a slot that a *different* stream on this MAC is holding.
                    locked = None
                    if not adopted:
                        self.mac_locks[mac_row.id] = h.id
                        locked = mac_row.id
                    # VOD/episode links are FILES (mkv/mp4 over the CDN): pace
                    # them to real time like local files, or the player hits
                    # EOF early. Live is paced by its own encoder - never -re.
                    proc = await self._spawn(h.command, url, h.item_name,
                                             pace=(kind != "live"))
                    if proc is None:
                        if locked is not None:
                            self.mac_locks.pop(locked, None)
                        if adopted:
                            break
                        continue
                    h.portal_name, h.mac, h.url, h.proc = (
                        f"{portal.name} (xtream)" if adopted else portal.name,
                        "" if adopted else mac_row.mac, url, proc)
                    if not registered:
                        await self._register(h)
                        registered = True
                    first = await self._first_bytes(proc)
                    if not first:
                        if proc.returncode is None:
                            await db_log("WARNING", "stream",
                                         f"[{h.item_name}] no data within {STREAM_START_TIMEOUT}s from "
                                         f"{portal.name}/{mac_row.mac} -> fallback")
                        else:
                            # ffmpeg is gone and will never send a byte: say so
                            # (the [ffmpeg] log line has the stderr tail)
                            await db_log("WARNING", "stream",
                                         f"[{h.item_name}] ffmpeg exited rc={proc.returncode} before sending "
                                         f"data ({portal.name}/{mac_row.mac}) -> fallback")
                        await self._kill_quiet(proc)
                        if locked is not None:
                            self.mac_locks.pop(locked, None)
                        if adopted:
                            break
                        continue
                    await db_log("INFO", "stream",
                                 f"[{h.item_name}] playing via {portal.name}/{mac_row.mac} "
                                 f"({'transcode' if ' -c:v copy' not in h.command else 'copy'})")
                    yield first
                    async for chunk in self._read_proc(h, proc):
                        yield chunk
                    # EOF: stream ended/died -> move to next fallback silently
                    if locked is not None:
                        self.mac_locks.pop(locked, None)
                    await self._kill_quiet(proc)
                    if not h.dead:
                        await db_log("WARNING", "stream",
                                     f"[{h.item_name}] stream ended from {portal.name}/{mac_row.mac}"
                                     f" -> trying next fallback")
                # next portal in chain
            await db_log("ERROR", "stream", f"[{h.item_name}] all fallbacks exhausted")
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
        await self._kill_quiet(h.proc)
        await self._deregister(h)
        await db_log("INFO", "stream",
                     f"[{h.item_name}] stopped after {h.bytes_sent/1e6:.1f} MB")

    async def _read_proc(self, h: StreamHandle, proc):
        """Yield bytes with stall detection until EOF/death/kill."""
        while not h.dead:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(CHUNK), STREAM_STALL_TIMEOUT)
            except asyncio.TimeoutError:
                await db_log("WARNING", "stream",
                             f"[{h.item_name}] stalled >{STREAM_STALL_TIMEOUT}s without data")
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
