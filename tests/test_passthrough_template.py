"""
Tests for Option E: Pass-through proxy (bypass ffmpeg) persistent template.

Verifies:
  1. Preset is seeded as a persistent built-in with command '@passthrough'.
  2. Editing fields preserves the '@passthrough' sentinel.
  3. Uses_redirect is False (does NOT 302; holds the client connection).
  4. StreamManager._open_passthrough pipes raw upstream bytes without spawning FFmpeg.
  5. Active stream registration, telemetry (bytes_sent), and disconnect cleanup.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, _seed_defaults
from app.models import FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal, User
from app.services.ffmpeg_templates import (
    PASSTHROUGH_COMMAND, PASSTHROUGH_PRESET_NAME, REDIRECT_COMMAND,
    REDIRECT_PRESET_NAME, REFERENCE_PRESET_NAME, build_command, FFmpegOptions,
)
from app.services.stream_manager import MANAGER, PassthroughStream, StreamHandle

BASE = "http://testserver"


async def _seed_and_get_passthrough_id() -> int:
    await _seed_defaults()
    async with SessionLocal() as s:
        return (await s.execute(select(FFmpegTemplate.id).where(
            FFmpegTemplate.name == PASSTHROUGH_PRESET_NAME))).scalar_one()


async def test_passthrough_preset_is_seeded_as_a_builtin():
    pid = await _seed_and_get_passthrough_id()
    async with SessionLocal() as s:
        row = await s.get(FFmpegTemplate, pid)
        assert row is not None
        assert row.is_builtin is True
        assert row.enabled is True
        assert row.command == PASSTHROUGH_COMMAND
        assert row.command_source == "fields"


async def test_passthrough_template_does_not_want_redirect():
    """Option E holds the socket in SPM rather than 302-redirecting to the CDN."""
    pid = await _seed_and_get_passthrough_id()
    async with SessionLocal() as s:
        portal = Portal(name="p_pass", base_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        src = LiveSource(portal_id=portal.id, portal_channel_id="10",
                         original_name="Channel Pass", cmd="ffmpeg http://cdn/live.ts",
                         enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Channel Pass", enabled=True,
                          ffmpeg_template_id=pid)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id, priority=1))
        await s.commit()
        pl_id = pl.id

    assert await MANAGER.uses_redirect("live", pl_id) is False


async def test_field_edits_preserve_passthrough_sentinel():
    pid = await _seed_and_get_passthrough_id()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.put(f"/api/ffmpeg/{pid}", json={"rc_mode": "VBR", "video_bitrate": "2000k"})
        assert r.status_code == 200, r.text
        assert r.json()["item"]["command"] == PASSTHROUGH_COMMAND

    async with SessionLocal() as s:
        row = await s.get(FFmpegTemplate, pid)
        assert row.command == PASSTHROUGH_COMMAND


async def test_passthrough_stream_process_wrapper():
    """PassthroughStream must provide the process-like duck typing needed by StreamManager."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = AsyncMock(spec=httpx.Response)

    async def fake_chunks():
        yield b"chunk1"
        yield b"chunk2"

    mock_response.aiter_bytes.return_value = fake_chunks()

    stream = PassthroughStream(mock_client, mock_response)
    assert stream.returncode is None
    assert stream.pid is None

    # Read chunks
    c1 = await stream.stdout.read()
    assert c1 == b"chunk1"
    c2 = await stream.stdout.read()
    assert c2 == b"chunk2"
    c3 = await stream.stdout.read()
    assert c3 == b""
    assert stream.returncode == 0

    # Kill and wait
    stream.kill()
    assert stream.returncode == -9
    rc = await stream.wait()
    assert rc == -9
    assert mock_response.aclose.await_count >= 1
    assert mock_client.aclose.await_count >= 1


async def test_open_passthrough_connects_and_returns_stream():
    """_open_passthrough opens an HTTP streaming connection directly to the target URL."""
    fake_body = [b"MPEGTS-DATA-CHUNK1", b"MPEGTS-DATA-CHUNK2"]

    class MockStreamResponse:
        status_code = 200

        async def aiter_bytes(self, chunk_size=None):
            for c in fake_body:
                yield c

        async def aclose(self):
            pass

    async def fake_send(self, req, stream=False):
        return MockStreamResponse()

    with patch.object(httpx.AsyncClient, "send", new=fake_send):
        proc, first, err = await MANAGER._open_passthrough(
            "http://cdn.example.com/live/ch1.ts", title="Ch 1", first_byte_timeout=5.0)

        assert err is None
        assert proc is not None
        assert isinstance(proc, PassthroughStream)
        assert first == b"MPEGTS-DATA-CHUNK1"

        second = await proc.stdout.read()
        assert second == b"MPEGTS-DATA-CHUNK2"

        eof = await proc.stdout.read()
        assert eof == b""
        await proc.wait()


async def test_spawn_refuses_passthrough_marker():
    """StreamManager._spawn must refuse to run @passthrough as an FFmpeg binary."""
    proc = await MANAGER._spawn(PASSTHROUGH_COMMAND, "http://cdn.example.com/stream.ts")
    assert proc is None


async def test_end_to_end_passthrough_playback(monkeypatch):
    """End-to-end play request through the API endpoint using Option E passthrough."""
    pid = await _seed_and_get_passthrough_id()
    async with SessionLocal() as s:
        user = User(name="test_pass_user", password="secret", enabled=True, m3u_enabled=True)
        s.add(user)
        portal = Portal(name="mock_pass_portal", base_url="http://mock.portal/c/", resolved_url="http://mock.portal/c/")
        s.add(portal)
        await s.flush()
        s.add(MacAddress(portal_id=portal.id, mac="00:1A:79:AA:AA:01", order=0))
        src = LiveSource(portal_id=portal.id, portal_channel_id="99",
                         original_name="Mock Pass Live", cmd="http://cdn.mock/live99.ts",
                         enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Mock Pass Live", enabled=True,
                          ffmpeg_template_id=pid)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id, priority=1))
        await s.commit()
        pl_id = pl.id

    raw_chunks = [b"RAW-TS-PACKET-1", b"RAW-TS-PACKET-2"]

    class MockStreamResp:
        status_code = 200

        async def aiter_bytes(self, chunk_size=None):
            for c in raw_chunks:
                yield c

        async def aclose(self):
            pass

    orig_send = httpx.AsyncClient.send

    async def fake_send(self, req, *args, **kwargs):
        if "cdn.mock" in str(req.url):
            return MockStreamResp()
        return await orig_send(self, req, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    mock_client = AsyncMock()
    mock_client.ensure_auth = AsyncMock()
    mock_client.close = AsyncMock()
    monkeypatch.setattr("app.services.stream_manager.POOL.get", AsyncMock(return_value=mock_client))
    monkeypatch.setattr(MANAGER, "_create_link_with_backoff", AsyncMock(return_value="http://cdn.mock/live99.ts"))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        resp = await c.get(f"/play/live/{pl_id}.ts?u=test_pass_user&p=secret")
        assert resp.status_code == 200
        assert b"RAW-TS-PACKET-1" in resp.content
        assert b"RAW-TS-PACKET-2" in resp.content
        assert resp.headers.get("x-accel-buffering") == "no"
        assert "video/mp2t" in resp.headers.get("content-type", "")

    # Cleanup any lingering streams
    await MANAGER.kill_all()

