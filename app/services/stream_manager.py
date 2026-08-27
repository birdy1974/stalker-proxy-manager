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

from ..config import (FALLBACK_STRATEGY, FFMPEG_BIN, MEDIA_ROOT,
                      STREAM_START_TIMEOUT)
from ..database import SessionLocal
from ..models import (
    ActiveStream, FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource,
    LocalFile, LocalPlaylist, MacAddress, Portal, SerieEpisode, SeriePlaylist,
    SeriePlaylistSource, SerieSeason, SerieSource, VodPlaylist, VodPlaylistSource,
    VodSource,
)
from ..portal.client import PortalError, StalkerClient
from .db_logging import db_log
from .ffmpeg_templates import URL_PLACEHOLDER

log = logging.getLogger("spm.stream")

STREAM_STALL_TIMEOUT = 25.0   # seconds without a single byte => dead stream
CHUNK = 64 * 1024
# input options that only exist for network protocols (stripped for file://)
_NETONLY_OPTS = re.compile(
    r"-(?:reconnect\w*|-?rw_timeout|timeout|user_agent|headers|http_proxy"
    r"|seekable|multiple_requests)\b\s*(?:\S+)?")


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


class StreamManager:
    def __init__(self) -> None:
        self.streams: dict[str, StreamHandle] = {}
        self.mac_locks: dict[int, str] = {}                 # mac_id -> stream_id
        self.user_counts: dict[str, int] = {}               # username -> open streams

    # ------------------------------------------------------------- registry
    def list(self) -> list[dict]:
        return [h.public() for h in self.streams.values()]

    def user_stream_count(self, username: str | None) -> int:
        return self.user_counts.get(username or "-", 0)

    def can_open_for(self, username: str | None, max_conn: int | None) -> bool:
        if max_conn is None or max_conn <= 0:
            return True
        return self.user_stream_count(username) < max_conn

    async def _register(self, h: StreamHandle) -> None:
        self.streams[h.id] = h
        self.user_counts[h.user_name or "-"] = self.user_stream_count(h.user_name) + 1
        try:
            async with SessionLocal() as s:
                s.add(ActiveStream(id=h.id, kind=h.kind, item_name=h.item_name,
                                   user_name=h.user_name, portal_name=h.portal_name or None,
                                   mac=h.mac or None, template_name=h.template_name,
                                   pid=h.proc.pid if h.proc else None))
                await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("active_streams insert failed")

    async def _deregister(self, h: StreamHandle) -> None:
        h.dead = True
        self.streams.pop(h.id, None)
        key = h.user_name or "-"
        self.user_counts[key] = max(0, self.user_counts.get(key, 1) - 1)
        for mac_id, sid in list(self.mac_locks.items()):
            if sid == h.id:
                del self.mac_locks[mac_id]
        try:
            async with SessionLocal() as s:
                await s.execute(delete(ActiveStream).where(ActiveStream.id == h.id))
                await s.commit()
        except Exception:  # noqa: BLE001
            pass

    async def kill(self, stream_id: str) -> bool:
        h = self.streams.get(stream_id)
        if not h:
            return False
        if h.dead:
            return True
        await db_log("WARNING", "stream", f"stream '{h.item_name}' killed (user/disconnect)")
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
        await self._deregister(h)
        return True

    async def kill_all(self) -> int:
        n = len(self.streams)
        for sid in list(self.streams):
            await self.kill(sid)
        return n

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
    async def _spawn(self, cmd_template: str, url: str) -> asyncio.subprocess.Process | None:
        cmd_text = cmd_template.replace(URL_PLACEHOLDER, url)
        # Network-source options do not exist for file:// inputs and make
        # ffmpeg abort with "Option reconnect not found". Strip them for
        # local-file playback.
        if url.startswith("file:") or os.path.isabs(url):
            cmd_text = _NETONLY_OPTS.sub(" ", cmd_text)
        if cmd_text.startswith("ffmpeg"):
            cmd_text = FFMPEG_BIN + cmd_text[len("ffmpeg"):]
        # HLS writes to files instead of stdout - not supported in phase 2 core
        if "<out_dir>" in cmd_text:
            await db_log("ERROR", "stream", "template uses HLS file output; use mpegts for live proxying")
            return None
        try:
            args = shlex.split(cmd_text)
        except ValueError as exc:
            await db_log("ERROR", "stream", f"unparseable ffmpeg template: {exc}")
            return None
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
    async def _live_chain(self, playlist_id: int) -> tuple[list, str, object]:
        """[(LiveSource, Portal, [MacAddress...])] in fallback priority order."""
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
                if not macs:
                    continue
                # portal_first strategy: only the best MAC per portal is tried
                macs = list(macs) if FALLBACK_STRATEGY == "macs_first" else list(macs[:1])
                if FALLBACK_STRATEGY == "portal_first" and portal.id in used_portals:
                    continue
                used_portals.add(portal.id)
                chain.append((src, portal, macs))
            return chain, item.custom_name, item

    async def _vod_chain(self, playlist_id: int):
        async with SessionLocal() as s:
            item = await s.get(VodPlaylist, playlist_id)
            if not item:
                return [], "", None
            rows = (await s.execute(
                select(VodPlaylistSource).where(VodPlaylistSource.vod_playlist_id == playlist_id)
                .order_by(VodPlaylistSource.priority))).scalars().all()
            chain = []
            for r in rows:
                src = await s.get(VodSource, r.vod_source_id)
                if not src or not src.cmd:
                    continue
                portal = await s.get(Portal, src.portal_id)
                if not portal or not portal.enabled:
                    continue
                macs = (await s.execute(select(MacAddress).where(
                    MacAddress.portal_id == portal.id).order_by(MacAddress.order))).scalars().all()
                if macs:
                    chain.append((src, portal, list(macs)))
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
            chain = []
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
                if macs:
                    chain.append((alt_ep, portal, list(macs)))
            name = f"{pl.custom_name} S{season.season_number:02d}E{ep.episode_number:02d}"
            return chain, name, pl

    async def _open_preview(self, src, portal, macs, kind: str = "live",
                            name: str | None = None):
        """
        Throwaway stream straight from an ORIGINAL source (GUI 'test stream').
        Passthrough copy command; no playlist involvement; full fallback over
        the portal's MACs still applies.
        """
        command = (f"ffmpeg -rw_timeout 10000000 -reconnect 1 -reconnect_at_eof 1 "
                   f"-reconnect_streamed 1 -reconnect_delay_max 5 -i {URL_PLACEHOLDER} "
                   f"-map 0:v:0 -map 0:a:0? -sn -dn -c copy -f mpegts pipe:1")
        h = StreamHandle(id=uuid.uuid4().hex, kind="preview",
                         item_name=name or getattr(src, "original_name", None)
                         or getattr(src, "name", "preview"),
                         user_name="admin", template_name="(preview copy)", command=command)
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

    # ------------------------------------------------------------ the pump
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
            async with SessionLocal() as s:
                lp = await s.get(LocalPlaylist, ref_id)
                lf = await s.get(LocalFile, lp.local_file_id) if lp else None
            item = lp
            item_name = (lp.custom_name or lf.filename) if lp and lf else "local file"
            path = None
            if lp and lf:
                async with SessionLocal() as s:
                    from ..models import LocalSource
                    ls = await s.get(LocalSource, lf.local_source_id)
                    path = f"{ls.directory.rstrip('/')}/{lf.relative_path}" if ls else None
            if path and not os.path.isabs(path):
                path = str(MEDIA_ROOT / path)          # same resolution as the scanner
            chain = [("local", path)] if path and os.path.exists(path) else []
        else:
            raise ValueError(f"unknown kind {kind}")

        tpl_name, command = await self._template_for(item)
        handle = StreamHandle(id=uuid.uuid4().hex, kind=kind, item_name=item_name,
                              user_name=user_name, template_name=tpl_name, command=command)
        # Pre-check: empty chain or EVERY mac currently occupied -> fail fast
        # with 404 instead of hanging a client with a 200 + empty body.
        if kind != "local":
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
                _tag, path = chain[0]
                proc = await self._spawn(h.command, ("file:" + path))
                if proc is None:
                    return
                h.url, h.proc = path, proc
                await self._register(h)
                registered = True
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
                    client = StalkerClient(portal.resolved_url or portal.base_url,
                                           mac_row.mac, mac_row.password, portal.proxy_url)
                    url = None
                    try:
                        if not portal.resolved_url:
                            from ..portal.resolver import resolve_portal  # local import: avoids cycle
                            res = await resolve_portal(portal.base_url, mac=mac_row.mac)
                            if res.ok:
                                portal.resolved_url = res.portal_url
                                client.portal_url = res.portal_url
                        await client.handshake()
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
                    proc = await self._spawn(h.command, url)
                    if proc is None:
                        self.mac_locks.pop(mac_row.id, None)
                        continue
                    h.portal_name, h.mac, h.url, h.proc = portal.name, mac_row.mac, url, proc
                    if not registered:
                        await self._register(h)
                        registered = True
                    try:
                        first = await asyncio.wait_for(proc.stdout.read(CHUNK),
                                                       STREAM_START_TIMEOUT)
                    except asyncio.TimeoutError:
                        first = b""
                    if not first:
                        pass  # diagnosis was: GPU template used without /dev/dri (fixed via _hardware_sanity)
                        await db_log("WARNING", "stream",
                                     f"[{h.item_name}] no data within {STREAM_START_TIMEOUT}s from "
                                     f"{portal.name}/{mac_row.mac} -> fallback")
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
