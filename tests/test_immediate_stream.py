"""
Tests for immediate HTTP 200 with chunked transfer (Recommendation 1).

Enigma2 and set-top box players enforce a strict tune/PAT timeout (3 to 5 seconds).
If SPM withholds the HTTP 200 response headers while waiting for the first chunk
of media (which can take several seconds due to portal handshake, CDN connect, and
codec probing), Enigma2 aborts and displays a black screen.

Answering with HTTP 200 immediately using Transfer-Encoding: chunked satisfies the
client's handshake watchdog in milliseconds while FFmpeg spins up.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import LivePlaylist, User
from app.routers.output import _stream_response
from app.services.stream_manager import MANAGER, StreamHandle


@pytest.fixture
async def seeded_live():
    async with SessionLocal() as s:
        user = User(name="stb_user", password="secret", enabled=True, m3u_enabled=True)
        s.add(user)
        channel = LivePlaylist(custom_name="Fast Channel", enabled=True)
        s.add(channel)
        await s.commit()
        cid = channel.id
    return cid


async def test_immediate_stream_returns_response_without_awaiting_first_chunk(seeded_live, monkeypatch):
    """_stream_response must return the StreamingResponse immediately without waiting for gen."""
    cid = seeded_live

    handle = StreamHandle(
        id="test-immediate",
        kind="live",
        item_name="Fast Channel",
        user_name="stb_user",
        template_name="copy",
        command="ffmpeg",
    )

    gen_started = False

    async def slow_generator():
        nonlocal gen_started
        gen_started = True
        yield b"CHUNK_1"

    async def mock_open(*args, **kwargs):
        return handle, slow_generator()

    monkeypatch.setattr(MANAGER, "open", mock_open)
    monkeypatch.setattr(MANAGER, "uses_redirect", lambda *args, **kwargs: asyncio.sleep(0, False))

    class DummyRequest:
        query_params = {}
        headers = {}
        method = "GET"

    user = User(name="stb_user", max_connections=0)
    resp = await _stream_response("live", cid, user, "live #1", DummyRequest())

    # The response object must be created and returned BEFORE the generator has yielded
    assert resp.status_code == 200
    assert resp.media_type == "video/mp2t"
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["X-SPM-Stream"] == handle.id
    assert "Server-Timing" in resp.headers
    assert gen_started is False, "generator was iterated prematurely before returning response"

    # Now read the body iterator:
    chunks = [c async for c in resp.body_iterator]
    assert gen_started is True
    assert chunks == [b"CHUNK_1"]


async def test_guarded_query_param_forces_guarded_wait(seeded_live, monkeypatch):
    """?guarded=1 forces the old guarded wait path."""
    cid = seeded_live

    handle = StreamHandle(
        id="test-guarded",
        kind="live",
        item_name="Fast Channel",
        user_name="stb_user",
        template_name="copy",
        command="ffmpeg",
    )
    handle.dead = False
    handle.busy = False

    async def empty_gen():
        return
        yield b""  # pragma: no cover

    async def mock_open(*args, **kwargs):
        return handle, empty_gen()

    monkeypatch.setattr(MANAGER, "open", mock_open)
    monkeypatch.setattr(MANAGER, "uses_redirect", lambda *args, **kwargs: asyncio.sleep(0, False))
    monkeypatch.setattr("app.routers.output._guard_wait", lambda h=None: 0.05)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/play/live/{cid}.ts?u=stb_user&p=secret&guarded=1")
        assert resp.status_code == 502
        assert "produced no data" in resp.text


async def test_immediate_stream_logs_and_notes_timing_on_first_chunk(seeded_live, monkeypatch):
    """Startup timing is recorded as soon as the first media chunk arrives."""
    cid = seeded_live
    timings = []

    handle = StreamHandle(
        id="test-timing",
        kind="live",
        item_name="Fast Channel",
        user_name="stb_user",
        template_name="copy",
        command="ffmpeg",
    )

    async def gen():
        await asyncio.sleep(0.01)
        yield b"FIRST"
        yield b"SECOND"

    async def mock_open(*args, **kwargs):
        return handle, gen()

    monkeypatch.setattr(MANAGER, "open", mock_open)
    monkeypatch.setattr(MANAGER, "uses_redirect", lambda *args, **kwargs: asyncio.sleep(0, False))
    monkeypatch.setattr(MANAGER, "note_timing", lambda **kw: timings.append(kw))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/play/live/{cid}.ts?u=stb_user&p=secret")
        assert resp.status_code == 200
        assert resp.content == b"FIRSTSECOND"

    assert len(timings) >= 1
    t = timings[0]
    assert t["item"] == "Fast Channel"
    assert t["mode"] == "proxy"
    assert "first_ms" in t


async def test_immediate_stream_empty_generator_terminates_gracefully(seeded_live, monkeypatch):
    """When a stream produces 0 bytes, the 200 response finishes cleanly without hanging."""
    cid = seeded_live
    logs = []

    handle = StreamHandle(
        id="test-empty",
        kind="live",
        item_name="Fast Channel",
        user_name="stb_user",
        template_name="copy",
        command="ffmpeg",
    )
    handle.fail_note = "upstream timed out"

    async def empty_gen():
        return
        yield b""  # pragma: no cover

    async def mock_open(*args, **kwargs):
        return handle, empty_gen()

    async def fake_db_log(lvl, mod, msg):
        logs.append((lvl, mod, msg))

    monkeypatch.setattr(MANAGER, "open", mock_open)
    monkeypatch.setattr(MANAGER, "uses_redirect", lambda *args, **kwargs: asyncio.sleep(0, False))
    monkeypatch.setattr("app.routers.output.db_log", fake_db_log)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/play/live/{cid}.ts?u=stb_user&p=secret")
        assert resp.status_code == 200
        assert resp.content == b""

    # Verify diagnostic log was emitted
    assert any("stream ended without producing data" in msg for _, _, msg in logs)
    assert any("upstream timed out" in msg for _, _, msg in logs)
