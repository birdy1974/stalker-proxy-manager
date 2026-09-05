"""
GUI Settings must drive the engine without a restart.

Three knobs used to be env-only (`SPM_FALLBACK_STRATEGY`,
`SPM_FETCH_PAGE_BUDGET`, `SPM_OUTPUT_BASE_URL`), so saving the Settings tab
had no effect. The helpers in `runtime_settings` read the DB on every use;
these tests pin that a POST /api/settings is honoured by the next play/fetch
and by the URLs the GUI copies for users.
"""
from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal, Setting,
    User,
)
from app.services.runtime_settings import (
    fallback_strategy, fetch_page_budget, output_base_url,
    vlc_local_network_caching_ms,
)
from app.services.stream_manager import MANAGER
from app.services.fetch_jobs import _paged_upsert
from app.portal.client import Page

BASE = "http://testserver"


async def _put(key: str, value) -> None:
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)
        await s.commit()


async def test_helpers_prefer_the_db_over_env():
    assert await fallback_strategy() in ("macs_first", "portal_first")
    await _put("fallback_strategy", "portal_first")
    assert await fallback_strategy() == "portal_first"
    await _put("fallback_strategy", "macs_first")
    assert await fallback_strategy() == "macs_first"

    await _put("fetch_page_budget", 7)
    assert await fetch_page_budget() == 7
    await _put("fetch_page_budget", "not-a-number")
    # invalid DB value falls back to env/default, which is a positive int
    assert await fetch_page_budget() >= 1

    await _put("output_base_url", "https://tv.example:8880/")
    assert await output_base_url() == "https://tv.example:8880"
    await _put("output_base_url", "   ")
    # empty GUI value = no override
    assert await output_base_url() == ""

    await _put("vlc_local_network_caching_ms", 250)
    assert await vlc_local_network_caching_ms() == 250
    await _put("vlc_local_network_caching_ms", -1)
    assert await vlc_local_network_caching_ms() == 0
    await _put("vlc_local_network_caching_ms", "invalid")
    assert await vlc_local_network_caching_ms() == 500


async def _two_mac_channel() -> int:
    """Live playlist item whose portal has two MACs. Returns playlist id."""
    async with SessionLocal() as s:
        portal = Portal(name="p1", base_url="http://p.invalid/c/", enabled=True)
        s.add(portal)
        await s.flush()
        s.add(MacAddress(portal_id=portal.id, mac="00:1A:79:00:00:01", order=0))
        s.add(MacAddress(portal_id=portal.id, mac="00:1A:79:00:00:02", order=1))
        src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                         original_name="Ch", cmd="ffmpeg http://x/1.ts", enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Ch", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id, priority=1))
        await s.commit()
        return pl.id


async def test_fallback_strategy_setting_changes_the_mac_chain_without_restart():
    pid = await _two_mac_channel()

    await _put("fallback_strategy", "macs_first")
    chain, _, _ = await MANAGER._live_chain(pid)
    assert len(chain) == 1
    assert [m.mac for m in chain[0][2]] == ["00:1A:79:00:00:01", "00:1A:79:00:00:02"]

    await _put("fallback_strategy", "portal_first")
    chain, _, _ = await MANAGER._live_chain(pid)
    assert [m.mac for m in chain[0][2]] == ["00:1A:79:00:00:01"], (
        "portal_first must try only the first MAC of the portal")


async def test_page_budget_setting_caps_fetch_without_restart():
    """A GUI budget of 2 pages must stop the walk after page 1 + page 2."""
    await _put("fetch_page_budget", 2)
    calls: list[int] = []

    async def fetch_page(p: int) -> Page:
        calls.append(p)
        return Page(items=[{"id": f"i{p}_{k}"} for k in range(14)], total=14 * 40)

    async def upsert(items):
        pass

    class _Job:
        def __init__(self):
            import asyncio
            self._cancel = asyncio.Event()
            self.detail = ""
            self.done_items = 0

    n, total = await _paged_upsert(_Job(), fetch_page, upsert, "Action", "test")
    assert total == 14 * 40
    assert calls == [1, 2], f"budget=2 should fetch pages 1-2, got {calls}"
    assert n == 28


async def test_output_base_url_setting_rewrites_playlist_and_user_urls():
    async with SessionLocal() as s:
        s.add(User(name="alice", password="pw", enabled=True,
                   m3u_enabled=True, xtream_enabled=True))
        s.add(LivePlaylist(custom_name="NPO 1", enabled=True, group_name="NL"))
        await s.commit()

    await _put("output_base_url", "https://nas.lan:8880")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        m3u = await c.get("/playlist.m3u?u=alice&p=pw")
        assert m3u.status_code == 200, m3u.text
        assert "https://nas.lan:8880/play/live/" in m3u.text
        assert "http://testserver/play/live/" not in m3u.text

        users = await c.get("/api/users")
        assert users.status_code == 200, users.text
        row = users.json()["items"][0]
        assert row["m3u_url"].startswith("https://nas.lan:8880/")
        assert row["xtream"]["url"] == "https://nas.lan:8880"

    # clearing the GUI override falls back to the incoming request host
    await _put("output_base_url", "")
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        m3u = await c.get("/playlist.m3u?u=alice&p=pw")
        assert "http://testserver/play/live/" in m3u.text
