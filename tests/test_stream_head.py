"""Player HEAD probes must never allocate a portal/MAC/FFmpeg stream."""
from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import User
from app.services.stream_manager import MANAGER


async def test_head_is_metadata_only_for_every_portal_stream_alias(monkeypatch):
    async with SessionLocal() as s:
        s.add(User(name="head", password="pw", enabled=True, m3u_enabled=True,
                   max_connections=1))
        await s.commit()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("HEAD must not resolve a source or open FFmpeg")

    monkeypatch.setattr(MANAGER, "open", forbidden)
    monkeypatch.setattr(MANAGER, "resolve", forbidden)
    paths = [
        "/play/live/999.ts?u=head&p=pw",
        "/play/vod/999.ts?u=head&p=pw",
        "/play/episode/999.ts?u=head&p=pw",
        "/play/live/999.mkv?u=head&p=pw",
        "/play/vod/999.mkv?u=head&p=pw",
        "/play/episode/999.mkv?u=head&p=pw",
        "/live/head/pw/999.ts",
        "/movie/head/pw/999.ts",
        "/series/head/pw/999.ts",
        "/movie/head/pw/999.mkv",
        "/series/head/pw/999.mkv",
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for path in paths:
            response = await c.head(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["content-type"] in ("video/mp2t", "video/x-matroska")

    assert MANAGER.list() == []


async def test_head_still_requires_valid_credentials():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.head("/play/live/1.ts?u=nope&p=wrong")
    assert response.status_code == 403
