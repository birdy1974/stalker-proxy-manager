"""FFmpeg tab: demo a template against an enabled playlist source.

The old 'Demo (testsrc)' button always ran lavfi testsrc2. Operators need to
pick a real enabled playlist item and see the actual ffmpeg argv + stderr.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.database import SessionLocal
from app.main import app
from app.models import (
    LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal,
    VodPlaylist, VodSource,
)
from app.services.ffmpeg_templates import URL_PLACEHOLDER
from app.services.item_info import resolve_playlist_input
from app.services.stream_manager import MANAGER

BASE = "http://testserver"
CMD = f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"

MAC_A = "00:1A:79:44:EE:FA"   # the MAC from the bug report
MAC_B = "00:1A:79:44:EE:FB"


@pytest.fixture(autouse=True)
def _clean_manager():
    """MANAGER is a process-global singleton: locks/leases from one test must
    not decide the next one's answers."""
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()


async def _seed_live_with_macs(*, macs=2, name="Formula - F1TV Race Replay"):
    """Portal + MACs + a live channel whose stored cmd is a MAC-parameterised
    direct link (link flags known, none set): the policy plays it as-is once
    %mac% is filled, so the MAC a resolution was asked through is observable
    without a running portal. Returns (playlist_id, [MacAddress rows])."""
    async with SessionLocal() as s:
        s.add(Portal(name="pm", base_url="http://p.test/c/",
                     resolved_url="http://p.test/c/portal.php", enabled=True))
        await s.flush()
        portal = (await s.execute(select(Portal))).scalar_one()
        for i, address in enumerate([MAC_A, MAC_B][:macs]):
            s.add(MacAddress(portal_id=portal.id, mac=address, order=i))
        src = LiveSource(portal_id=portal.id, portal_channel_id="127256",
                         original_name=name,
                         cmd="ffmpeg http://cdn.test/%mac%/127256.ts",
                         link_flags="", enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name=name, group_name="NL", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id, sorted((await s.execute(select(MacAddress))).scalars().all(),
                             key=lambda m: m.order)


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


# --------------------------------------------------------------------------- #
# the demo must not resolve through a MAC that is streaming right now
#
# Reported symptom (live #117 F1TV, box playing the channel while the demo
# ran): the demo picked the portal's first MAC unconditionally; the panel
# holds that MAC's single connection slot for the box's active stream and
# answers the demo's second concurrent media link with HTTP 456, which the
# GUI showed as a bare "rc=8 with no output".
# --------------------------------------------------------------------------- #

async def test_demo_resolves_through_the_first_mac_and_names_it():
    pid, macs = await _seed_live_with_macs()
    async with SessionLocal() as s:
        got = await resolve_playlist_input(s, "live", pid, avoid_busy=True)
    assert got["url"] == f"http://cdn.test/{MAC_A}/127256.ts"
    assert got["mac"] == MAC_A
    assert got["portal"] == "pm"


async def test_demo_avoids_a_mac_that_is_streaming():
    pid, (mac_a, mac_b) = await _seed_live_with_macs()
    MANAGER.mac_locks[mac_a.id] = "stream-box"
    async with SessionLocal() as s:
        got = await resolve_playlist_input(s, "live", pid, avoid_busy=True)
    assert got["mac"] == mac_b.mac
    assert got["url"] == f"http://cdn.test/{mac_b.mac}/127256.ts"


async def test_demo_avoids_a_mac_the_portal_says_is_banned():
    pid, (mac_a, mac_b) = await _seed_live_with_macs()
    async with SessionLocal() as s:
        await s.execute(update(MacAddress).where(MacAddress.mac == MAC_A)
                        .values(status="banned"))
        await s.commit()
        got = await resolve_playlist_input(s, "live", pid, avoid_busy=True)
    assert got["mac"] == mac_b.mac


async def test_demo_reports_occupancy_when_every_mac_is_busy():
    pid, (mac_a,) = await _seed_live_with_macs(macs=1)
    MANAGER.mac_locks[mac_a.id] = "stream-box"
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as excinfo:
            await resolve_playlist_input(s, "live", pid, avoid_busy=True)
    msg = str(excinfo.value)
    assert "no free MAC" in msg
    assert mac_a.mac in msg
    assert "456" in msg


async def test_popup_resolution_keeps_the_first_mac_even_when_busy():
    """avoid_busy is opt-in: the detail popups (read-only views) keep showing
    the URL the first MAC would be played with, even while that MAC streams."""
    pid, (mac_a, _mac_b) = await _seed_live_with_macs()
    MANAGER.mac_locks[mac_a.id] = "stream-box"
    async with SessionLocal() as s:
        got = await resolve_playlist_input(s, "live", pid)
    assert got["url"] == f"http://cdn.test/{mac_a.mac}/127256.ts"


async def test_demo_avoids_a_mac_holding_a_redirect_lease():
    """The box watches a channel via redirect (302, no ffmpeg pipe here): the
    MAC's slot is still held on the panel, so the demo must take another MAC."""
    pid, (mac_a, mac_b) = await _seed_live_with_macs()
    MANAGER.lease_mac(mac_a.id, seconds=120, holder="box", item="Npo 1",
                      kind="live", ref=7)
    async with SessionLocal() as s:
        got = await resolve_playlist_input(s, "live", pid, avoid_busy=True)
    assert got["mac"] == mac_b.mac


async def test_demo_names_the_lease_holder_in_the_error():
    pid, (mac_a,) = await _seed_live_with_macs(macs=1)
    MANAGER.lease_mac(mac_a.id, seconds=120, holder="box",
                      item="Formula - F1TV Race Replay", kind="live", ref=117)
    async with SessionLocal() as s:
        with pytest.raises(ValueError) as excinfo:
            await resolve_playlist_input(s, "live", pid, avoid_busy=True)
    msg = str(excinfo.value)
    assert "no free MAC" in msg
    assert "redirect lease" in msg
    assert "box" in msg
    assert "Formula - F1TV Race Replay" in msg


async def test_demo_api_refuses_a_busy_mac_instead_of_running_ffmpeg():
    pid, (mac_a,) = await _seed_live_with_macs(macs=1)
    MANAGER.mac_locks[mac_a.id] = "stream-box"
    async with await _client() as c:
        r = await c.post("/api/ffmpeg/demo", json={
            "command": CMD, "mode": "playlist", "kind": "live", "id": pid,
        })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "no free MAC" in detail
    assert "456" in detail
