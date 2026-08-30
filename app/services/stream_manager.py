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
from ..portal.pool import POOL
from ..portal.client import MAG_UA, PortalError, StalkerClient
from .db_logging import db_log
from .ffmpeg_templates import REDIRECT_COMMAND, URL_PLACEHOLDER, serves_original_file
from .item_info import local_file_path

log = logging.getLogger("spm.stream")

STREAM_STALL_TIMEOUT = 25.0   # seconds without a single byte => dead stream
CHUNK = 64 * 1024
# input options that only exist for network protocols (stripped for file://)
_NETONLY_OPTS = re.compile(
    r"-(?:reconnect\w*|-?rw_timeout|timeout|user_agent|headers|http_proxy"
    r"|seekable|multiple_requests|referer)\b\s*(?:\S+)?")
_NET_SCHEMES = ("http://", "https://")


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


class StreamManager:
    def __init__(self) -> None:
        self.streams: dict[str, StreamHandle] = {}
        self.mac_locks: dict[int, str] = {}                 # mac_id -> stream_id
        self._watchers: set[asyncio.Task] = set()           # strong refs, see watch()
        self._proc_gone_since: dict[str, float] = {}        # stream_id -> first seen

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
    def _network_identity(cmd_text: str, url: str) -> str:
        """
        Give ffmpeg the identity of the STB it is impersonating.

        ffmpeg announces itself as "Lavf/61.x" and sends no Referer; plenty of
        Stalker panels - and the CDNs in front of them - answer that with 403
        or 405 ("Method Not Allowed") on an otherwise perfectly valid link.
        Inject the MAG user-agent and the stream's own origin unless the
        template already says otherwise (user edits always win).
        """
        if not url.lower().startswith(_NET_SCHEMES):
            return cmd_text
        add: list[str] = []
        if "-user_agent" not in cmd_text:
            add.append(f'-user_agent "{MAG_UA}"')
        if "-referer" not in cmd_text and "-headers" not in cmd_text:
            origin = url.split("://", 1)[-1].split("/", 1)[0]
            add.append(f'-referer "{url.split("://", 1)[0]}://{origin}/"')
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
    def _ffmpeg_argv(cmd_template: str, url: str, title: str | None = None) -> list[str] | None:
        """Render a template + input into an argv list, or None if unusable.

        Local paths are quoted so `shlex.split` keeps spaces/quotes as one
        `-i` argument, and network-only flags are stripped so ffmpeg does not
        abort with "Option reconnect not found" on a file.
        
        If title is provided, injects -metadata title=... into the output so
        players (VLC, etc.) display the correct stream name instead of whatever
        metadata the source stream contains.
        """
        if (cmd_template or "").strip() == REDIRECT_COMMAND:
            return None
        is_net = url.lower().startswith(_NET_SCHEMES)
        insert = url if is_net else shlex.quote(url)
        cmd_text = StreamManager._network_identity(
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

    async def _spawn(self, cmd_template: str, url: str, title: str | None = None) -> asyncio.subprocess.Process | None:
        if (cmd_template or "").strip() == REDIRECT_COMMAND:
            await db_log("ERROR", "stream",
                         "cannot spawn the redirect template as ffmpeg "
                         "(local files must be served directly)")
            return None
        if "<out_dir>" in (cmd_template or ""):
            await db_log("ERROR", "stream", "template uses HLS file output; use mpegts for live proxying")
            return None
        args = self._ffmpeg_argv(cmd_template, url, title)
        if not args:
            await db_log("ERROR", "stream", "unparseable ffmpeg template")
            return None
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
        """
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
        """
        probe = src if template_id is None else _WithTemplate(src, template_id)
        tpl_name, command = await self._template_for(probe)
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
            for mac_row in macs:
                if mac_row.id in self.mac_locks:
                    continue                      # occupied by one of our own pipes
                client = await POOL.get(portal.resolved_url or portal.base_url,
                                        mac_row.mac, mac_row.password, portal.proxy_url)
                try:
                    if not portal.resolved_url:
                        from ..portal.resolver import resolve_portal
                        res = await resolve_portal(portal.base_url, mac=mac_row.mac)
                        if res.ok:
                            portal.resolved_url = res.portal_url
                            client.portal_url = res.portal_url
                            client.invalidate()   # token was for the old URL
                    await client.ensure_auth()
                    url = await client.create_link(getattr(_src, "cmd", "") or "", link_kind)
                except PortalError as exc:
                    await db_log("WARNING", "stream",
                                 f"[{item_name}] redirect: {portal.name}/{mac_row.mac}: {exc} -> next")
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
            free = any(m.id not in self.mac_locks for (_s, _p, macs) in chain for m in macs)
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
                proc = await self._spawn(h.command, path, h.item_name)
                if proc is None:
                    return
                h.url, h.proc = path, proc
                await self._register(h)
                registered = True
                first = await self._first_bytes(proc)
                if not first:
                    await db_log("WARNING", "stream",
                                 f"[{h.item_name}] ffmpeg produced no data for local file")
                    await self._kill_quiet(proc)
                    return
                yield first
                async for chunk in self._read_proc(h, proc):
                    yield chunk
                return

            for idx, (src, portal, macs) in enumerate(chain, 1):
                if h.dead:
                    return
                for mac_row in macs:
                    if h.dead:
                        return
                    if mac_row.id in self.mac_locks:
                        await db_log("INFO", "stream",
                                     f"[{h.item_name}] mac {mac_row.mac} busy -> skip "
                                     f"(fallback step {idx}/{len(chain)})")
                        continue
                    await db_log("INFO", "stream",
                                 f"[{h.item_name}] fallback step {idx}/{len(chain)}: "
                                 f"portal '{portal.name}' mac {mac_row.mac}")
                    client = await POOL.get(portal.resolved_url or portal.base_url,
                                     mac_row.mac, mac_row.password, portal.proxy_url)
                    url = None
                    try:
                        if not portal.resolved_url:
                            from ..portal.resolver import resolve_portal  # local import: avoids cycle
                            res = await resolve_portal(portal.base_url, mac=mac_row.mac)
                            if res.ok:
                                portal.resolved_url = res.portal_url
                                client.portal_url = res.portal_url
                                client.invalidate()   # token was for the old URL
                        await client.ensure_auth()
                        link_kind = "live" if kind == "live" else "vod"
                        url = await client.create_link(getattr(src, "cmd", "") or "", link_kind)
                    except PortalError as exc:
                        await db_log("WARNING", "stream",
                                     f"[{h.item_name}] {portal.name}/{mac_row.mac}: {exc} -> next")
                        continue
                    except Exception as exc:  # noqa: BLE001
                        await db_log("WARNING", "stream",
                                     f"[{h.item_name}] {portal.name}/{mac_row.mac}: "
                                     f"unexpected {type(exc).__name__}: {exc} -> next")
                        continue
                    finally:
                        await client.close()
                    if not url:
                        continue

                    # lock the MAC BEFORE starting ffmpeg so parallel requests
                    # see it as occupied immediately
                    self.mac_locks[mac_row.id] = h.id
                    proc = await self._spawn(h.command, url, h.item_name)
                    if proc is None:
                        self.mac_locks.pop(mac_row.id, None)
                        continue
                    h.portal_name, h.mac, h.url, h.proc = portal.name, mac_row.mac, url, proc
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
                        self.mac_locks.pop(mac_row.id, None)
                        continue
                    await db_log("INFO", "stream",
                                 f"[{h.item_name}] playing via {portal.name}/{mac_row.mac} "
                                 f"({'transcode' if ' -c:v copy' not in h.command else 'copy'})")
                    yield first
                    async for chunk in self._read_proc(h, proc):
                        yield chunk
                    # EOF: stream ended/died -> move to next fallback silently
                    self.mac_locks.pop(mac_row.id, None)
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
