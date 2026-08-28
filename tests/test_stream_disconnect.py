"""
Regression tests for the client-disconnect teardown bug.

Reported symptom (production log, Postgres):

    ERROR [sqlalchemy.pool.impl.AsyncAdaptedQueuePool] Exception terminating
    connection <AdaptedConnection <asyncpg.connection.Connection ...>>
    ...
    asyncio.exceptions.CancelledError: Cancelled via cancel scope 7f5061ad4e10
    by <Task pending name='Task-158' coro=<RequestResponseCycle.run_asgi() ...>

Cause: a player that switches channel closes the socket, Starlette cancels its
StreamingResponse task group, and anyio then *keeps* re-delivering that
cancellation every event-loop turn until the request task is gone. Every
`await` in the stream teardown is therefore interrupted - including the one
that hands a pooled connection back to SQLAlchemy, which logs the traceback
above and drops the connection. The teardown writes never landed either
(active_streams row left as a ghost, "stopped after N MB" log row lost).

These tests drive a real ASGI streaming response, disconnect it mid-stream and
assert the teardown still finishes.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.database import SessionLocal, engine, run_uncancelled
from app.models import ActiveStream, Log
from app.services import stream_manager as sm
from app.services.db_logging import db_log, flush_logs
from app.services.stream_manager import MANAGER, StreamHandle


# ---------------------------------------------------------------- test doubles
class _Portal:
    name = "nexusconnects"
    base_url = "http://portal.invalid"
    resolved_url = "http://portal.invalid"
    proxy_url = None


class _Mac:
    def __init__(self, id_: int, mac: str) -> None:
        self.id, self.mac, self.password = id_, mac, ""


class _Src:
    cmd = "ffmpeg -i {url} -f mpegts pipe:1"


class _FakeStalkerClient:
    """Stands in for the portal HTTP client - the network is not under test."""

    def __init__(self, *args, **kwargs) -> None:
        self.portal_url = args[0] if args else ""

    async def handshake(self) -> None:
        return None

    async def create_link(self, cmd: str, kind: str) -> str:
        return "http://portal.invalid/stream/1.ts"

    async def close(self) -> None:
        return None


# One 64 KiB chunk every 150 ms: between chunks the pump is parked inside
# `await proc.stdout.read(CHUNK)`, which is where a real disconnect catches it
# (a generator suspended at `yield` instead would not reproduce the bug).
async def _spawn_stub(self, cmd_template: str, url: str):
    return await asyncio.create_subprocess_exec(
        "sh", "-c", "while :; do head -c 65536 /dev/zero | tr '\\0' 'A'; sleep 0.15; done",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)


def _handle(**kw) -> StreamHandle:
    base = dict(id="teststream", kind="live", item_name="Man of War - 2026",
                user_name="tester", template_name="Copy", command="ffmpeg {url}")
    base.update(kw)
    return StreamHandle(**base)


# --------------------------------------------------------------------- helpers
async def _count(model) -> int:
    async with SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _last_log() -> str | None:
    async with SessionLocal() as s:
        row = (await s.execute(select(Log).order_by(Log.id.desc()).limit(1))).scalar_one_or_none()
    return row.message if row else None


async def _run_until_disconnect(app, *, chunks: int) -> int:
    """Drive the ASGI app and make the client vanish after `chunks` bodies."""
    go = asyncio.Event()
    sent = 0

    async def receive():
        await go.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        nonlocal sent
        if msg["type"] == "http.response.body":
            sent += 1
            if sent >= chunks:
                go.set()                          # the player walks away

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/play.ts", "raw_path": b"/play.ts", "query_string": b"",
             "root_path": "", "headers": [(b"host", b"localhost")],
             "client": ("127.0.0.1", 51000), "server": ("localhost", 80)}
    # named like uvicorn's task so the CancelledError reads like the bug report
    task = asyncio.create_task(app(scope, receive, send),
                               name="RequestResponseCycle.run_asgi()")
    await asyncio.wait_for(task, 30)
    return sent


# ----------------------------------------------------------------------- tests
async def test_stream_teardown_is_clean_after_client_disconnect(pool_errors, monkeypatch):
    """The production scenario, end to end through the real pump."""
    monkeypatch.setattr(sm, "StalkerClient", _FakeStalkerClient)
    monkeypatch.setattr(sm.StreamManager, "_spawn", _spawn_stub)

    app = FastAPI()

    @app.get("/play.ts")
    async def play(request: Request):
        # wired exactly like routers/output.py: pump -> StreamingResponse + watchdog
        h = _handle()
        gen = MANAGER._pump(h, [(_Src(), _Portal(), [_Mac(1, "00:1A:79:01:6D:BF")])], "live")
        asyncio.get_running_loop().create_task(MANAGER.watch_disconnect(request, h))
        return StreamingResponse(gen, media_type="video/mp2t")

    sent = await _run_until_disconnect(app, chunks=3)
    for _ in range(100):                       # let the shielded teardown finish
        if await _count(ActiveStream) == 0 and MANAGER.mac_locks == {}:
            break
        await asyncio.sleep(0.05)
    await flush_logs()

    assert sent >= 3, "the harness never actually streamed anything"
    assert pool_errors == [], (
        "client disconnect still breaks connection teardown: " + repr(pool_errors))
    assert await _count(ActiveStream) == 0, "active_streams row leaked (ghost on the dashboard)"
    assert MANAGER.mac_locks == {}, "MAC stayed locked after the client left"
    assert MANAGER.user_stream_count("tester") == 0, "user connection slot not released"
    last = await _last_log()
    assert last is not None and "stopped after" in last, (
        f"teardown log row was lost; last row is {last!r}")


async def test_unshielded_session_close_still_reproduces_it(pool_errors):
    """
    Sensitivity check for the harness: the SAME pattern with a plain
    AsyncSession (what the code used before `_DisconnectSafeSession`) DOES
    produce the reported pool error. If this ever stops failing, the assertions
    around it would be passing vacuously.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    UnsafeSession = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    app = FastAPI()

    @app.get("/play.ts")
    async def play():
        async def gen():
            try:
                for _ in range(100000):
                    yield b"x" * 65536
                    await asyncio.sleep(0.15)   # <- the disconnect lands here
            finally:
                # what the old pump's finally did: DB work while the request
                # task is being cancelled
                async with UnsafeSession() as s:
                    await s.execute(select(func.count()).select_from(Log))
                    await s.commit()
        return StreamingResponse(gen(), media_type="video/mp2t")

    await _run_until_disconnect(app, chunks=3)
    await asyncio.sleep(0.2)

    assert pool_errors, (
        "the unshielded pattern no longer reproduces the pool error - this "
        "harness has stopped detecting the bug it is meant to guard against")


async def test_get_db_survives_an_abandoned_request(pool_errors):
    """
    Covers every `async with SessionLocal()` in the codebase (auth, M3U/XMLTV
    generation, the get_db dependency): a client that gives up mid-request must
    not turn into a dropped pooled connection. /playlist.m3u and /xmltv.php are
    the slow ones players actually abandon.
    """
    from fastapi import Depends

    from app.database import get_db

    app = FastAPI()

    @app.get("/play.ts")
    async def play(request: Request, db=Depends(get_db)):
        await asyncio.sleep(0.15)            # stand-in for a slow playlist build
        await db.execute(select(func.count()).select_from(Log))
        return StreamingResponse(_slow_body(), media_type="video/mp2t")

    await _run_until_disconnect(app, chunks=3)
    await asyncio.sleep(0.3)

    assert pool_errors == [], f"abandoned request dropped a connection: {pool_errors!r}"


async def _slow_body():
    for _ in range(1000):
        yield b"x" * 65536
        await asyncio.sleep(0.15)


async def test_deregister_deletes_row_even_when_its_task_is_cancelled(pool_errors):
    """`_deregister` runs from the pump's finally, i.e. inside a dying task."""
    h = _handle(id="cancel-me")
    MANAGER.streams[h.id] = h
    MANAGER.mac_locks[1] = h.id
    MANAGER.user_counts["tester"] = 1
    async with SessionLocal() as s:
        s.add(ActiveStream(id=h.id, kind="live", item_name=h.item_name,
                           user_name="tester", template_name="Copy"))
        await s.commit()
    assert await _count(ActiveStream) == 1

    task = asyncio.create_task(MANAGER._deregister(h))
    await asyncio.sleep(0)                     # let it enter the DB work
    task.cancel()                              # ...and yank it, like a disconnect
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if await _count(ActiveStream) == 0:
            break
        await asyncio.sleep(0.05)

    assert await _count(ActiveStream) == 0, "active_streams row survived the cancellation"
    assert MANAGER.mac_locks == {}, "MAC stayed locked"
    assert MANAGER.user_stream_count("tester") == 0
    assert pool_errors == [], f"connection teardown was interrupted: {pool_errors!r}"


async def test_db_log_persists_when_the_caller_is_cancelled(pool_errors):
    """Log rows go through the writer task, so a cancelled caller cannot lose them."""
    async def worker():
        await db_log("INFO", "stream", "fallback step 1/1: portal 'nexusconnects'")
        await asyncio.sleep(30)                # still running when we cancel

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await flush_logs()

    assert await _count(Log) == 1
    assert "fallback step 1/1" in (await _last_log())
    assert pool_errors == []


async def test_log_writer_restarts_if_it_dies(pool_errors):
    """A dead drain task must not cost a shutdown stall plus lost rows."""
    from app.services import db_logging

    await db_log("INFO", "stream", "before the writer died")
    await flush_logs()
    assert db_logging._writer is not None
    db_logging._writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await db_logging._writer

    await db_log("INFO", "stream", "after the writer died")
    await flush_logs(timeout=5)

    msgs = [r for r in (await _all_logs())]
    assert "after the writer died" in msgs, f"row lost after the writer died: {msgs}"
    assert "before the writer died" in msgs
    assert pool_errors == []


async def _all_logs() -> list[str]:
    async with SessionLocal() as s:
        return list((await s.execute(select(Log.message).order_by(Log.id))).scalars().all())


async def test_run_uncancelled_gets_work_through_repeated_cancellation():
    """
    The core primitive. anyio re-delivers cancellation on every loop turn, so a
    try/except cannot survive it - only leaving the cancel scope can.
    """
    steps = 0

    async def work():
        nonlocal steps
        for _ in range(6):
            await asyncio.sleep(0.02)
            steps += 1
        return steps

    import anyio
    outcome = None

    async with anyio.create_task_group() as tg:
        async def wrap():
            nonlocal outcome
            with anyio.move_on_after(10):
                outcome = await run_uncancelled(work(), timeout=5)
            tg.cancel_scope.cancel()

        async def canceller():
            await asyncio.sleep(0.03)          # cancel while work() is running
            tg.cancel_scope.cancel()

        tg.start_soon(wrap)
        tg.start_soon(canceller)

    assert steps == 6, f"only {steps}/6 steps of the shielded work ran"
    assert outcome == 6


async def test_run_uncancelled_does_not_swallow_cancellation():
    """Callers must still unwind - shielding protects the work, not the request."""
    async def work():
        await asyncio.sleep(0.01)
        return 1

    async def caller():
        await run_uncancelled(work())
        await asyncio.sleep(30)

    task = asyncio.create_task(caller())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
