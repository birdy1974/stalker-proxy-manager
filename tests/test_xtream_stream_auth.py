"""Xtream stream URLs use Xtream access and advertise only real formats."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import User
from app.services.playlist_gen import xtream_base


async def _user(name: str, *, m3u: bool, xtream: bool) -> User:
    async with SessionLocal() as session:
        user = User(name=name, password="pw", enabled=True, m3u_enabled=m3u,
                    xtream_enabled=xtream, max_connections=1)
        session.add(user)
        await session.commit()
        return user


async def test_xtream_only_user_can_head_native_and_xtream_stream_urls():
    await _user("xt-only", m3u=False, xtream=True)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        native = await client.head("/play/live/123.ts?u=xt-only&p=pw")
        xtream = await client.head("/live/xt-only/pw/123.ts")
        playlist = await client.get("/playlist.m3u?u=xt-only&p=pw")
    assert native.status_code == 200  # /get.php emits these /play URLs
    assert xtream.status_code == 200
    assert playlist.status_code == 403  # native playlist access remains disabled


async def test_m3u_only_user_cannot_use_xtream_namespace():
    await _user("m3u-only", m3u=True, xtream=False)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        native = await client.head("/play/live/123.ts?u=m3u-only&p=pw")
        xtream = await client.head("/live/m3u-only/pw/123.ts")
    assert native.status_code == 200
    assert xtream.status_code == 403


async def test_xtream_identity_does_not_advertise_unimplemented_hls():
    user = await _user("format", m3u=True, xtream=True)
    result = await xtream_base(user, "https://iptv.example.test:8443")
    assert result["user_info"]["allowed_output_formats"] == ["ts"]
    assert result["server_info"]["url"] == "iptv.example.test"
    assert result["server_info"]["port"] == "8443"
    assert result["server_info"]["https"] is True
    assert result["server_info"]["server_protocol"] == "https"

    default_port = await xtream_base(user, "http://iptv.example.test")
    assert default_port["server_info"]["port"] == "80"
