"""Playback areas: one playlist, per-user groups, per-area FFmpeg overlay."""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, _seed_defaults
from app.models import (
    Area, AreaItemTemplate, FFmpegTemplate, Portal, User, VodPlaylist, VodSource,
)
from app.services.ffmpeg_templates import (
    E2_VOD_REMUX_PRESET_NAME, REDIRECT_PRESET_NAME, REFERENCE_PRESET_NAME,
)
from app.services.playback import template_map_for
from app.services.playlist_gen import build_m3u, clear_m3u_cache
from app.services.stream_manager import MANAGER

ROOT = Path(__file__).resolve().parents[1]
AREAS_HTML = (ROOT / "app/templates/areas.html").read_text()
USERS_HTML = (ROOT / "app/templates/users.html").read_text()
PLAYLIST_HTML = (ROOT / "app/templates/playlist.html").read_text()
BASE = "http://proxy.local"


def test_gui_wires_areas_page_and_user_picker():
    assert "Kind defaults" in AREAS_HTML
    assert "Add exception" in AREAS_HTML
    assert "/api/areas" in AREAS_HTML
    assert "id=\"u-area\"" in USERS_HTML
    assert "FFmpeg tpl (default)" in PLAYLIST_HTML


async def _templates() -> dict[str, int]:
    await _seed_defaults()
    async with SessionLocal() as s:
        rows = (await s.execute(select(FFmpegTemplate))).scalars().all()
        return {r.name: r.id for r in rows}


async def _vod(template_id: int | None = None) -> int:
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://p.invalid")
        s.add(portal)
        await s.flush()
        src = VodSource(portal_id=portal.id, portal_item_id="1",
                        original_name="Film", enabled=True)
        s.add(src)
        await s.flush()
        pl = VodPlaylist(vod_source_id=src.id, custom_name="Film",
                         group_name="Action", enabled=True,
                         ffmpeg_template_id=template_id)
        s.add(pl)
        await s.commit()
        return pl.id


async def test_area_kind_default_overrides_playlist_template():
    names = await _templates()
    vid = await _vod(names[REDIRECT_PRESET_NAME])
    async with SessionLocal() as s:
        area = Area(name="Phone", enabled=True,
                    ffmpeg_template_vod_id=names[REFERENCE_PRESET_NAME])
        s.add(area)
        await s.flush()
        alice = User(name="alice", password="pw", m3u_enabled=True, area_id=area.id)
        bob = User(name="bob", password="pw", m3u_enabled=True)  # no area
        s.add_all([alice, bob])
        await s.commit()
        item = await s.get(VodPlaylist, vid)
        t_alice = (await template_map_for(s, alice)).resolve("vod", item)
        t_bob = (await template_map_for(s, bob)).resolve("vod", item)
    assert t_alice.name == REFERENCE_PRESET_NAME
    assert t_bob.name == REDIRECT_PRESET_NAME
    assert await MANAGER.uses_redirect("vod", vid, "alice") is False
    assert await MANAGER.uses_redirect("vod", vid, "bob") is True
    assert await MANAGER.uses_redirect("vod", vid) is True  # no user = catalog


async def test_area_item_exception_beats_kind_default():
    names = await _templates()
    vid = await _vod(names[REDIRECT_PRESET_NAME])
    async with SessionLocal() as s:
        area = Area(name="Living room", enabled=True,
                    ffmpeg_template_vod_id=names[REFERENCE_PRESET_NAME])
        s.add(area)
        await s.flush()
        s.add(AreaItemTemplate(area_id=area.id, kind="vod", playlist_id=vid,
                               ffmpeg_template_id=names[E2_VOD_REMUX_PRESET_NAME]))
        user = User(name="box", password="pw", m3u_enabled=True, area_id=area.id)
        s.add(user)
        await s.commit()
        item = await s.get(VodPlaylist, vid)
        resolved = (await template_map_for(s, user)).resolve("vod", item)
    assert resolved.name == E2_VOD_REMUX_PRESET_NAME
    assert resolved.container == "mkv"


async def test_m3u_extension_follows_the_users_area():
    names = await _templates()
    vid = await _vod(names[REDIRECT_PRESET_NAME])
    async with SessionLocal() as s:
        area = Area(name="E2", enabled=True,
                    ffmpeg_template_vod_id=names[E2_VOD_REMUX_PRESET_NAME])
        s.add(area)
        await s.flush()
        user = User(name="e2", password="pw", m3u_enabled=True, area_id=area.id)
        plain = User(name="vlc", password="pw", m3u_enabled=True)
        s.add_all([user, plain])
        await s.commit()
        u, p = user, plain
    clear_m3u_cache()
    mkv = await build_m3u(BASE, u)
    ts = await build_m3u(BASE, p)
    assert f"/play/vod/{vid}.mkv?u=e2&p=pw" in mkv
    assert f"/play/vod/{vid}.ts?u=vlc&p=pw" in ts


async def test_areas_api_crud_and_user_assignment():
    names = await _templates()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        created = await c.post("/api/areas", json={
            "name": "Phone", "enabled": True,
            "ffmpeg_template_live_id": names[REFERENCE_PRESET_NAME],
        })
        assert created.status_code == 200, created.text
        aid = created.json()["id"]
        listed = await c.get("/api/areas")
        assert any(a["name"] == "Phone" for a in listed.json()["items"])
        u = await c.post("/api/users", json={"name": "x", "password": "y", "area_id": aid})
        assert u.status_code == 200, u.text
        users = await c.get("/api/users")
        row = next(i for i in users.json()["items"] if i["name"] == "x")
        assert row["area_id"] == aid and row["area_name"] == "Phone"
        await c.put(f"/api/users/{row['id']}", json={"area_id": None})
        users = await c.get("/api/users")
        row = next(i for i in users.json()["items"] if i["name"] == "x")
        assert row["area_id"] is None


async def test_disabled_area_is_ignored():
    names = await _templates()
    vid = await _vod(names[REDIRECT_PRESET_NAME])
    async with SessionLocal() as s:
        area = Area(name="Off", enabled=False,
                    ffmpeg_template_vod_id=names[REFERENCE_PRESET_NAME])
        s.add(area)
        await s.flush()
        user = User(name="z", password="pw", area_id=area.id)
        s.add(user)
        await s.commit()
        item = await s.get(VodPlaylist, vid)
        resolved = (await template_map_for(s, user)).resolve("vod", item)
    assert resolved.name == REDIRECT_PRESET_NAME
