"""Orphaned ffmpeg on early disconnect, and the leaks around it.

Production incident: a player opened a channel after a day idle, the first
portal in the chain delivered nothing, and the player gave up (~24 s without
data) while the pump was still awaiting backup's first byte. Two things went
wrong *after* the disconnect:

  1. the ffmpeg the pump had just spawned was never killed: `CancelledError`
     propagated through `_open_with_identity` (which had no `try/finally`),
     the handle was never registered (so `_finish` never ran), and the
     process kept retrying its refused link until IT gave up - the log showed
     "stream ended without producing data" and then, 18 s later, "ffmpeg
     exited rc=8", from a request that was long dead;
  2. the handle was never marked dead either, so the disconnect watchdog -
     which waits for a registration that never comes - looped forever (one
     leaked task per failed start, on the plain 502 path too, not just on
     disconnects).

The `not h.parked` guard in the same `finally` pins a third, adjacent case:
an attached-then-abandoned stream is unregistered as well (its registry entry
predates this pump), and `_finish` just re-parked it - dropping its MAC lock
there would undo the park it just took.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (LivePlaylist, LivePlaylistSource, LiveSource,
                        MacAddress, Portal, Log)
from app.services import stream_manager
from app.services.db_logging import flush_logs
from app.services.stream_manager import MANAGER

MAC = "00:1A:79:00:00:01"


# --------------------------------------------------------------------------- #
# harness (same doubles as tests/test_sticky_streams.py)
# --------------------------------------------------------------------------- #
class _Pipe:
    """An ffmpeg-ish process: chunks are fed by the test, reads block."""

    def __init__(self, *, returncode: int | None = None) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.returncode = returncode
        self.pid = 777
        self.stdout = self
        self.stderr = self

    def feed(self, chunk: bytes = b"\x47" * (188 * 10)) -> None:
        self.queue.put_nowait(chunk)

    async def read(self, n: int) -> bytes:
        if self.returncode is not None:
            return b""
        try:
            return await asyncio.wait_for(self.queue.get(), 30)
        except asyncio.TimeoutError:  # pragma: no cover - only on a hung test
            return b""

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _Client:
    """Portal client: hands out a link and counts how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0
        self.url = "http://cdn/live.ts?play_token=t"

    async def ensure_auth(self) -> None:
        return None

    async def create_link(self, cmd: str, kind: str, **kw) -> str:
        self.calls += 1
        self.url = f"http://cdn/live.ts?play_token={self.calls}"
        return self.url

    def invalidate(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def _aclose(self) -> None:
        return None


class _Pool:
    def __init__(self, client: _Client) -> None:
        self._client = client

    async def get(self, session) -> _Client:
        return self._client


async def _route(*, name: str = "Ch1"):
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/",
                        resolved_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        mac = MacAddress(portal_id=portal.id, mac=MAC, status="online", order=0)
        s.add(mac)
        await s.flush()
        src = LiveSource(portal_id=portal.id, portal_channel_id=name,
                         original_name=name, cmd="ffmpeg http://cdn/x.ts",
                         enabled=True, link_flags="use_http_tmp_link")
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name=name, enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id, mac.id


def _wire_spawn(monkeypatch, pipes: list[_Pipe], spawned: asyncio.Event | None = None,
                *, feed_first: bool = True):
    """Real `_open_with_identity`, fake processes - the spawn is the seam.

    `feed_first=False` leaves the pipe silent, for tests that cancel (or time
    out) the first-byte wait itself."""
    client = _Client()
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))

    async def fake_spawn(self, cmd_template: str, url: str,
                         title: str | None = None, pace: bool = False,
                         user_agent: str | None = None):
        pipe = pipes.pop(0)
        if feed_first and pipe.returncode is None:
            # A process that produces bytes needs no feeding for its first one
            # (a preset returncode means "dies before a byte" - leave it).
            pipe.feed(b"first-chunk")
        if spawned is not None:
            spawned.set()
        return pipe

    # Class level: an instance patch would leave a shadow behind (see the
    # `_no_shadowed_manager_methods` fixture).
    monkeypatch.setattr(type(MANAGER), "_spawn", fake_spawn)
    return client


@pytest.fixture(autouse=True)
async def _no_parked_leftovers():
    """Parked pipes hold a process, a MAC and a task - end them with the test."""
    yield
    for h in list(MANAGER.streams.values()):
        h.dead = True
        if h.parker is not None and not h.parker.done():
            h.parker.cancel()
        if h.proc is not None:
            try:
                h.proc.kill()
            except Exception:  # noqa: BLE001
                pass
    MANAGER.streams.clear()
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()


class _Req:
    connected = True

    async def is_disconnected(self) -> bool:
        return not self.connected


# --------------------------------------------------------------------------- #
# the orphan
# --------------------------------------------------------------------------- #
async def test_cancelled_start_kills_the_spawned_ffmpeg(monkeypatch):
    """The production orphan: disconnect mid-start must not leak the process.

    The pump is cancelled while it awaits the candidate's first byte - the
    player walked away. The spawned ffmpeg, the MAC lock, the starting-entry
    and the handle itself must all be finished; only the dashboard row never
    existed (nothing ever played).
    """
    pl, mac_id = await _route()
    pipe = _Pipe()
    spawned = asyncio.Event()
    _wire_spawn(monkeypatch, [pipe], spawned, feed_first=False)

    handle, gen = await MANAGER.open("live", pl, "box")
    assert not handle.dead
    task = asyncio.create_task(gen.__anext__())
    await asyncio.wait_for(spawned.wait(), 5)
    await asyncio.sleep(0.2)          # inside the first-byte wait now
    assert pipe.returncode is None, "the test did not catch the pump mid-start"

    task.cancel()
    with pytest.raises(StopAsyncIteration):   # the pump swallows the cancel
        await task

    assert pipe.returncode == -9, "the spawned ffmpeg survived its request"
    assert handle.dead is True
    assert MANAGER.mac_locks.get(mac_id) in (None, set()), "MAC stayed locked"
    assert handle.id not in MANAGER.streams
    assert handle.id not in MANAGER.starting


async def test_failed_start_marks_handle_dead_so_watchdog_exits(monkeypatch):
    """Same leak without any disconnect: a 502 start used to strand the watchdog.

    The watchdog waits for a registration that only a first byte brings; a
    handle that never produces one must be marked dead when its pump ends, or
    the task loops (and its strong reference in `_watchers` keeps it) forever.
    """
    monkeypatch.setattr(stream_manager, "ZAP_RETRY", False)
    pl, mac_id = await _route()
    _wire_spawn(monkeypatch, [_Pipe(returncode=1)])   # dies before a byte

    handle, gen = await MANAGER.open("live", pl, "box")
    watch = asyncio.create_task(MANAGER.watch_disconnect(_Req(), handle))
    async for _chunk in gen:                          # exhaust the fallbacks
        pass

    assert handle.dead is True
    await asyncio.wait_for(watch, 2)                  # returns, not TimeoutError
    assert MANAGER.mac_locks.get(mac_id) in (None, set())


async def test_repark_after_attach_keeps_mac_and_pipe(monkeypatch):
    """An attached-then-abandoned stream re-parks: lock held, pipe alive.

    The re-park runs through the same `finally` as a failed start, but the
    handle there is NOT finished - `_finish` just parked it, and the branch
    that drops locks of never-registered handles must leave it alone.
    """
    monkeypatch.setattr(stream_manager, "LINGER_S", 30.0)
    pl, mac_id = await _route()
    pipe = _Pipe()
    _wire_spawn(monkeypatch, [pipe])

    handle, gen = await MANAGER.open("live", pl, "box")
    await gen.__anext__()
    for _ in range(2):
        pipe.feed()
        await gen.__anext__()
    await gen.aclose()                                # first client leaves
    assert handle.parked is True

    pipe.feed(b"live-after-return")
    handle2, gen2 = await MANAGER.open("live", pl, "box")
    assert handle2 is handle
    got = await gen2.__anext__()
    assert got
    await gen2.aclose()                               # ... and leaves again

    assert handle.parked is True, "the re-park was undone by its own teardown"
    assert handle.dead is False
    assert pipe.returncode is None, "the parked pipe was killed"
    assert handle.id in MANAGER.streams
    assert handle.id in MANAGER.mac_locks.get(mac_id, set()), \
        "the parked pipe lost its MAC lock"
    await MANAGER.kill(handle.id)


# --------------------------------------------------------------------------- #
# passthrough symmetry: the upstream socket is a panel slot too
# --------------------------------------------------------------------------- #
async def test_cancelled_passthrough_start_closes_the_client(monkeypatch):
    """Disconnect while the first bytes are on their way: close the socket.

    The passthrough path holds no process, but its httpx client IS the panel
    connection - leaving it open holds the slot until garbage collection.
    """
    got_request = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n\r\n")
            await writer.drain()
            got_request.set()
            await asyncio.sleep(30)                   # headers out, body never
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        seen: list[httpx.AsyncClient] = []
        real_client = httpx.AsyncClient

        class _Recording(real_client):  # type: ignore[misc]
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                seen.append(self)

        monkeypatch.setattr(httpx, "AsyncClient", _Recording)
        port = server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/live.ts?play_token=t"

        task = asyncio.create_task(
            MANAGER._open_passthrough(url, title="t", first_byte_timeout=10))
        await asyncio.wait_for(got_request.wait(), 5)
        await asyncio.sleep(0.2)                      # inside the first read
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(seen) == 1
        assert seen[0].is_closed, "the upstream socket survived its request"
    finally:
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- #
# the diagnosis: silence with an empty stderr is a local stall, say so
# --------------------------------------------------------------------------- #
async def test_silent_stall_with_empty_stderr_says_where_it_stuck(monkeypatch):
    """ffmpeg's banner always precedes input opening: nothing printed means it
    never reached the media request - a local startup stall, not the portal."""
    monkeypatch.setattr(stream_manager, "ZAP_RETRY", False)
    monkeypatch.setattr(stream_manager, "STREAM_START_TIMEOUT", 0.15)
    pl, _mac = await _route()
    _wire_spawn(monkeypatch, [_Pipe()],
                feed_first=False)                     # silent until killed

    handle, gen = await MANAGER.open("live", pl, "box")
    async for _chunk in gen:
        pass
    await flush_logs()

    assert handle.attempts == [f"p/{MAC}: silent 0.15s"], handle.attempts
    async with SessionLocal() as s:
        rows = (await s.execute(select(Log.message).where(
            Log.message.contains("printed nothing at all")))).scalars().all()
    assert rows, "the empty-stderr stall was not diagnosed in the log"
    assert "never reached the media request" in rows[0]
