"""
FULL-FIDELITY preview end-to-end: real mock portal over real HTTP, the app's
real handshake/pump, the REAL ffmpeg binary, and the real /preview route on a
real uvicorn socket - the whole chain the GUI preview popup exercises.

Guards the 'preview popup gets no input' bug: on a fresh install the DEFAULT
template is the redirect marker (@redirect); before the fix the preview tried
to spawn it, produced no bytes and 502'd after 25s. Skipped when no ffmpeg
binary exists (the mock portal's stream endpoint needs one too).
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest

from app.config import FFMPEG_BIN
from app.database import SessionLocal
from app.models import FFmpegTemplate, LiveSource, MacAddress, Portal, VodSource
from app.services.stream_manager import MANAGER, URL_PLACEHOLDER

pytestmark = pytest.mark.skipif(
    not os.path.exists(FFMPEG_BIN) or FFMPEG_BIN == "ffmpeg",
    reason="needs a real ffmpeg binary (the mock portal streams via ffmpeg)")


async def _seed_preview_fixture():
    from app.main import _seed_defaults
    from sqlalchemy import select

    from app.portal.client import StalkerClient  # noqa: F401  (import sanity)

    await _seed_defaults()
    async with SessionLocal() as s:
        # exact fresh-install state: redirect marker IS the default template
        marker = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.is_default.is_(True)))).scalar_one()
        assert marker.command == "@redirect", \
            "precondition: this test pins the shipped default (redirect marker)"
        p = Portal(name="e2emock", base_url=_portal_url(), resolved_url=_portal_url())
        s.add(p)
        await s.commit()
        s.add(MacAddress(portal_id=p.id, mac="00:1A:79:AA:AA:01", order=0))
        s.add(LiveSource(portal_id=p.id, portal_channel_id="7",
                         original_name="E2E Live", cmd="ffmpeg http://mock/ts/7.ts"))
        s.add(VodSource(portal_id=p.id, portal_item_id="2007",
                        original_name="E2E Vod", cmd="ffmpeg http://mock/vod/2007.mp4"))
        await s.commit()
        sid = (await s.execute(select(LiveSource))).scalars().first().id
        vid = (await s.execute(select(VodSource))).scalars().first().id
    return sid, vid


def _free_port() -> int:
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    return port


def _portal_url() -> str:
    return f"http://127.0.0.1:{_MOCK_PORT}/mock/c/portal.php"


async def _wait_port(port: int) -> None:
    for _ in range(100):
        try:
            sk = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            sk.close()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError(f"server on {port} never came up")


#: one mock portal + one SPM instance per pytest session (both real servers)
_MOCK_PORT = _free_port()
_SPM_PORT = _free_port()
_STARTED = False


async def _start_servers() -> None:
    global _STARTED
    if _STARTED:
        return
    import uvicorn
    from fastapi import FastAPI

    from app.main import app
    from app.portal.mock_portal import router as MOCK_ROUTER

    mock = FastAPI()
    mock.include_router(MOCK_ROUTER)
    for conf, port in ((uvicorn.Config(mock, host="127.0.0.1", port=_MOCK_PORT,
                                       log_level="error"), _MOCK_PORT),
                       (uvicorn.Config(app, host="127.0.0.1", port=_SPM_PORT,
                                       log_level="error"), _SPM_PORT)):
        srv = uvicorn.Server(conf)
        threading.Thread(target=srv.run, daemon=True).start()
        await _wait_port(port)
    _STARTED = True


async def test_preview_popup_chain_end_to_end_with_real_ffmpeg():
    import httpx

    await _start_servers()
    sid, vid = await _seed_preview_fixture()

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{_SPM_PORT}",
                                 timeout=90) as c:
        for label, url, want in (("live", f"/preview/live/{sid}.ts", 150_000),
                                 ("vod", f"/preview/vod/{vid}.ts", 100_000)):
            buf = bytearray()
            async with c.stream("GET", url) as r:
                assert r.status_code == 200, f"{label}: {r.status_code} {r.text[:200]}"
                assert r.headers["content-type"].startswith("video/mp2t")
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) >= want:
                        break
            assert len(buf) >= want, f"{label}: only {len(buf)} bytes streamed"
            # real MPEG-TS: 188-byte packets, 0x47 sync byte on every packet
            stride = range(0, min(len(buf), want), 188)
            assert len(stride) > 100 and all(buf[i] == 0x47 for i in stride), \
                f"{label}: payload is not MPEG-TS"

    # the stream the popup would have received before the fix was none at all:
    # assert the registry is clean afterwards (watchdog tore both down)
    await asyncio.sleep(1.0)
    assert MANAGER.list() == [], "preview streams leaked in the registry"
