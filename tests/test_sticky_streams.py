"""Sticky streams: the pipe outlives the client for a moment (see LINGER_S).

A player that zaps away and comes back used to pay for everything twice: a
create_link, an ffmpeg start, and the first byte from the panel's CDN. Holding
the pipe for a few seconds turns that second visit into an attach - the bytes
are already flowing.

The contract these tests pin, in one place:

  * a live pipe that played and lost its client is PARKED: process alive, MAC
    still locked (the panel is still counting that connection), and the runtime
    row gone (the dashboard shows what is being watched - nobody is);
  * a returning client attaches: same handle, no create_link, no ffmpeg start,
    buffered bytes first;
  * parking is a courtesy, never a reservation: the pipe expires by itself, and
    anybody who actually wants that MAC gets it immediately;
  * a pipe that never played, or a kind that is not on the list, is killed as
    it always was.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (ActiveStream, LivePlaylist, LivePlaylistSource,
                        LiveSource, MacAddress, Portal, User)
from app.services import stream_manager
from app.services.stream_manager import MANAGER, StreamHandle

MAC = "00:1A:79:00:00:01"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class _Pipe:
    """An ffmpeg-ish process: chunks are fed by the test, reads block."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.returncode: int | None = None
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


async def _route(*, name: str = "Ch1", portal_id: int | None = None,
                 kind_flags: str = "use_http_tmp_link"):
    """A playlist item; `portal_id` reuses an existing portal (and its MAC)."""
    async with SessionLocal() as s:
        if portal_id is None:
            portal = Portal(name="p", base_url="http://127.0.0.1:1/c/",
                            resolved_url="http://127.0.0.1:1/c/")
            s.add(portal)
            await s.flush()
            mac = MacAddress(portal_id=portal.id, mac=MAC, status="online", order=0)
            s.add(mac)
            await s.flush()
            portal_id, mac_id = portal.id, mac.id
        else:
            mac_id = (await s.execute(
                select(MacAddress.id).where(MacAddress.portal_id == portal_id)
            )).scalars().first()
        src = LiveSource(portal_id=portal_id, portal_channel_id=name,
                         original_name=name, cmd="ffmpeg http://cdn/x.ts",
                         enabled=True, link_flags=kind_flags)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name=name, enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id, mac_id, portal_id


def _wire(monkeypatch, pipes: list[_Pipe]):
    """Patching at class level: an instance patch would leave a shadow behind."""
    client = _Client()
    monkeypatch.setattr(stream_manager, "POOL", _Pool(client))
    monkeypatch.setattr(stream_manager, "link_is_alive", lambda *a, **k: True)

    async def fake_open(self, command, url, *, title="", pace=False, first_byte_timeout=None):
        pipe = pipes.pop(0)
        pipe.feed(b"first-chunk")
        return pipe, b"first-chunk", None

    calls = {"n": 0}
    real_open = fake_open

    async def counting_open(self, *a, **kw):
        calls["n"] += 1
        return await real_open(self, *a, **kw)

    monkeypatch.setattr(type(MANAGER), "_open_with_identity", counting_open)
    monkeypatch.setattr(type(MANAGER), "_drain_stderr",
                        lambda self, proc: asyncio.sleep(0))
    return client, calls


async def _count(model) -> int:
    async with SessionLocal() as s:
        return len((await s.execute(select(model))).scalars().all())


async def _park_one(monkeypatch, *, linger: float = 30.0):
    """Start a live stream, read a chunk, and let the client vanish."""
    monkeypatch.setattr(stream_manager, "LINGER_S", linger)
    pl, mac_id, portal_id = await _route()
    pipe = _Pipe()
    client, spawned = _wire(monkeypatch, [pipe])
    handle, gen = await MANAGER.open("live", pl, "box")
    first = await gen.__anext__()
    # A couple of chunks from the pipe itself: `bytes_sent` counts what the
    # pump really read (the first chunk is the opener's, before the pump).
    for _ in range(2):
        pipe.feed()
        first += await gen.__anext__()
    await gen.aclose()                    # the player walked away
    for _ in range(50):                   # the park is decided in the teardown
        if handle.parked:
            break
        await asyncio.sleep(0.02)
    return pl, mac_id, pipe, client, handle, first, portal_id


@pytest.fixture(autouse=True)
async def _no_parked_leftovers():
    """Parked pipes hold a process, a MAC and a task - end them with the test.

    (Otherwise a parker outlives its event loop and the next test sees
    "bound to a different event loop" - the parked handle belongs to the test
    that created it, not to the app's single loop.)
    """
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


# --------------------------------------------------------------------------- #
# parking
# --------------------------------------------------------------------------- #
async def test_a_live_pipe_is_parked_when_the_client_leaves(monkeypatch):
    pl, mac_id, pipe, client, h, first, _portal = await _park_one(monkeypatch)

    assert first, "the stream really played"
    assert h.parked is True
    assert pipe.returncode is None, "the process is still running"
    assert mac_id in MANAGER.mac_locks, "the MAC is held: the panel is still sending"
    assert h.id in MANAGER.mac_locks[mac_id]
    assert await _count(ActiveStream) == 0, "nobody is watching, so no dashboard row"
    assert MANAGER.user_stream_count("box") == 0, "a parked pipe is not a viewer"
    assert h.parker is not None and not h.parker.done()


async def test_a_zap_back_attaches_to_the_running_pipe(monkeypatch):
    """The whole point: no create_link, no ffmpeg start, bytes immediately."""
    pl, _mac, pipe, client, h, _first, _portal = await _park_one(monkeypatch)
    calls_before = client.calls
    assert h.parked

    pipe.feed(b"live-after-return")
    handle2, gen = await MANAGER.open("live", pl, "box")
    got = await gen.__anext__()
    await gen.aclose()

    assert handle2 is h, "the same pipe, not a new stream"
    assert client.calls == calls_before, "the panel was not asked again"
    assert b"live-after-return" in got or b"first-chunk" in got
    assert h.reattaches == 1
    assert await _count(ActiveStream) <= 1, "a row is back while somebody watches"


async def test_the_parked_pipe_expires_and_releases_everything(monkeypatch):
    pl, mac_id, pipe, client, h, _first, _p = await _park_one(monkeypatch, linger=0.2)

    for _ in range(100):
        if h.id not in MANAGER.streams:
            break
        await asyncio.sleep(0.05)

    assert h.id not in MANAGER.streams, "the parked stream was reaped"
    assert pipe.returncode == -9, "and its process killed"
    assert MANAGER.mac_locks.get(mac_id) in (None, set()), "MAC released"
    assert await _count(ActiveStream) == 0


async def test_another_user_takes_the_parked_mac_instead_of_waiting(monkeypatch):
    """Parking is a courtesy, not a reservation."""
    pl, mac_id, pipe, client, h, _first, _p = await _park_one(monkeypatch, linger=30.0)
    monkeypatch.setattr(stream_manager, "BUSY_WAIT_S", 5.0)     # must not be spent

    other = _Pipe()
    _wire(monkeypatch, [other])
    started = time.monotonic()
    handle2, gen = await MANAGER.open("live", pl, "anna")
    first = await gen.__anext__()
    took = time.monotonic() - started
    alive = not handle2.dead              # before the close, which ends it
    await gen.aclose()

    assert first and alive
    assert took < 0.5, f"no waiting on a parked pipe (took {took:.2f}s)"
    assert pipe.returncode == -9, "the parked pipe gave way immediately"
    assert h.parked is False


async def test_the_same_user_zapping_elsewhere_drops_the_parked_pipe(monkeypatch):
    pl, mac_id, pipe, client, h, _first, portal_id = await _park_one(monkeypatch)
    # Same portal, same MAC: this is the "zap to another channel" case.
    other_pl, _mac2, _p2 = await _route(name="Ch2", portal_id=portal_id)

    next_pipe = _Pipe()
    _wire(monkeypatch, [next_pipe])
    handle2, gen = await MANAGER.open("live", other_pl, "box")
    await gen.__anext__()
    await gen.aclose()

    assert pipe.returncode == -9, "the zap away recycled the held pipe on this MAC"


# --------------------------------------------------------------------------- #
# what must NOT be parked
# --------------------------------------------------------------------------- #
async def test_a_pipe_that_never_played_is_not_parked(monkeypatch):
    """Holding a panel slot for a stream that produced nothing is not a favour."""
    monkeypatch.setattr(stream_manager, "LINGER_S", 30.0)
    pl, mac_id, _portal = await _route()
    handle = StreamHandle(id="never-played", kind="live", item_name="Ch1",
                          user_name="box", template_name="t", command="ffmpeg")
    handle.bytes_sent = 0
    await MANAGER._register(handle)
    MANAGER.mac_locks.setdefault(mac_id, set()).add(handle.id)

    await MANAGER.client_left(handle)
    assert handle.parked is False
    assert handle.dead is True, "killed as before"


async def test_a_kind_that_is_not_on_the_list_is_not_parked(monkeypatch):
    monkeypatch.setattr(stream_manager, "LINGER_S", 30.0)
    pl, mac_id, _portal = await _route()
    handle = StreamHandle(id="a-movie", kind="vod", item_name="A Movie",
                          user_name="box", template_name="t", command="ffmpeg")
    handle.bytes_sent = 10_000
    await MANAGER._register(handle)
    MANAGER.mac_locks.setdefault(mac_id, set()).add(handle.id)

    await MANAGER.client_left(handle)
    assert handle.parked is False and handle.dead is True
