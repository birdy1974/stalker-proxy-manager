"""FFmpeg tab: demo a template against an enabled playlist source.

The old 'Demo (testsrc)' button always ran lavfi testsrc2. Operators need to
pick a real enabled playlist item and see the actual ffmpeg argv + stderr.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, Portal, VodPlaylist, VodSource,
)
from app.services.ffmpeg_templates import URL_PLACEHOLDER
from app.services.item_info import resolve_playlist_input

BASE = "http://testserver"
CMD = f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


async def _seed_live(*, enabled_pl=True, enabled_src=True, name="BBC One"):
    async with SessionLocal() as s:
        s.add(Portal(name="p1", base_url="http://p.test/c/",
                     resolved_url="http://p.test/c/portal.php", enabled=True))
        await s.flush()
        portal = (await s.execute(select(Portal))).scalar_one()
        src = LiveSource(portal_id=portal.id, portal_channel_id="101",
                         original_name=name, cmd="ffmpeg http://cdn.test/101.ts",
                         enabled=enabled_src)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name=name, group_name="UK", enabled=enabled_pl)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id, priority=1))
        await s.commit()
        return pl.id, src.id


async def test_demo_sources_lists_only_enabled_playlist_items():
    on_id, _ = await _seed_live(enabled_pl=True, name="On Air")
    async with SessionLocal() as s:
        s.add(Portal(name="p2", base_url="http://p2.test/c/", enabled=True))
        await s.flush()
        portal = (await s.execute(select(Portal).where(Portal.name == "p2"))).scalar_one()
        src = LiveSource(portal_id=portal.id, portal_channel_id="202",
                         original_name="Off Air", cmd="ffmpeg http://cdn.test/202.ts",
                         enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Off Air", enabled=False)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id, priority=1))
        await s.commit()

    async with await _client() as c:
        r = await c.get("/api/ffmpeg/demo-sources", params={"kind": "live"})
    assert r.status_code == 200, r.text
    names = [i["name"] for i in r.json()["items"]]
    assert "On Air" in names
    assert "Off Air" not in names
    hit = next(i for i in r.json()["items"] if i["id"] == on_id)
    assert hit["kind"] == "live"
    assert hit["source"] == "On Air"
    assert hit["portal"] == "p1"


async def test_demo_sources_filters_by_name():
    await _seed_live(name="CNN International")
    async with await _client() as c:
        r = await c.get("/api/ffmpeg/demo-sources", params={"kind": "live", "q": "cnn"})
    assert [i["name"] for i in r.json()["items"]] == ["CNN International"]
    async with await _client() as c:
        r = await c.get("/api/ffmpeg/demo-sources", params={"kind": "live", "q": "zzzz"})
    assert r.json()["items"] == []


async def test_demo_sources_rejects_unknown_kind():
    async with await _client() as c:
        r = await c.get("/api/ffmpeg/demo-sources", params={"kind": "nope"})
    assert r.status_code == 400


async def test_resolve_playlist_input_uses_stored_cmd_when_portal_has_no_mac():
    pid, _ = await _seed_live(name="No MAC")
    async with SessionLocal() as s:
        got = await resolve_playlist_input(s, "live", pid)
    assert got["url"] == "http://cdn.test/101.ts"
    assert got["name"] == "No MAC"
    assert got["kind"] == "live"


async def test_resolve_playlist_input_rejects_disabled_item():
    pid, _ = await _seed_live(enabled_pl=False, name="Disabled")
    async with SessionLocal() as s:
        try:
            await resolve_playlist_input(s, "live", pid)
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert "disabled" in str(exc)


async def test_demo_playlist_mode_runs_against_resolved_url(monkeypatch):
    pid, _ = await _seed_live(name="Demo Chan")
    captured = {}

    async def fake_run_demo(command, mode="lavfi", url=None, source_label=None):
        captured.update(command=command, mode=mode, url=url, source_label=source_label)
        return {"ok": True, "mode": mode, "detail": "ok", "bytes": 12, "rc": 0,
                "stderr": "ffmpeg version test\nInput #0\n", "ms": 9,
                "source": source_label, "argv": ["ffmpeg", "-i", url, "-t", "2"],
                "argv_text": f"ffmpeg -i {url} -t 2"}

    monkeypatch.setattr("app.routers.api_ffmpeg.run_demo", fake_run_demo)
    async with await _client() as c:
        r = await c.post("/api/ffmpeg/demo", json={
            "command": CMD, "mode": "playlist", "kind": "live", "id": pid,
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert captured["mode"] == "playlist"
    assert captured["url"] == "http://cdn.test/101.ts"
    assert "Demo Chan" in (captured["source_label"] or "")
    assert body["playlist"]["id"] == pid
    assert body["playlist"]["url"] == "http://cdn.test/101.ts"
    assert body["argv_text"].startswith("ffmpeg")
    assert "ffmpeg version test" in body["stderr"]


async def test_demo_playlist_missing_item_is_400():
    async with await _client() as c:
        r = await c.post("/api/ffmpeg/demo", json={
            "command": CMD, "mode": "playlist", "kind": "live", "id": 999999,
        })
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


async def test_run_demo_result_includes_argv_even_without_binary(monkeypatch):
    from app.services import ffmpeg_validate as fv

    async def boom(*_a, **_k):
        raise FileNotFoundError

    monkeypatch.setattr(fv.asyncio, "create_subprocess_exec", boom)
    r = await fv.run_demo(CMD, mode="url", url="http://x.test/a.ts")
    assert r["ok"] is False
    assert "ffmpeg not found" in r["detail"]
    assert r["argv"][0]
    assert "http://x.test/a.ts" in r["argv"]
    assert "-t" in r["argv"]
    assert r["argv_text"]


async def test_demo_sources_vod_enabled_only():
    async with SessionLocal() as s:
        s.add(Portal(name="pv", base_url="http://v.test/c/", enabled=True))
        await s.flush()
        portal = (await s.execute(select(Portal))).scalar_one()
        src = VodSource(portal_id=portal.id, portal_item_id="9",
                        original_name="Movie", cmd="ffmpeg http://cdn.test/m.mp4",
                        enabled=True)
        s.add(src)
        await s.flush()
        s.add(VodPlaylist(vod_source_id=src.id, custom_name="Movie",
                          group_name="Films", enabled=True))
        s.add(VodPlaylist(vod_source_id=src.id, custom_name="Hidden",
                          enabled=False))
        await s.commit()
    async with await _client() as c:
        r = await c.get("/api/ffmpeg/demo-sources", params={"kind": "vod"})
    names = [i["name"] for i in r.json()["items"]]
    assert "Movie" in names
    assert "Hidden" not in names
