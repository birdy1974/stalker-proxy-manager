"""EPG sources, safe fuzzy matching, portal guide import and output agreement."""

import asyncio
import gzip
import json
import lzma
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func

from app.database import SessionLocal
from app.main import app
from app.models import (
    EpgChannel,
    EpgProgramme,
    EpgSource,
    LivePlaylist,
    LivePlaylistSource,
    LiveSource,
    MacAddress,
    Portal,
    Setting,
    User,
)
from app.services import epg
from app.services.playlist_gen import build_m3u, xtream_live
from app.routers import api_epg


@pytest.fixture(autouse=True)
def epg_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(epg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api_epg, "queue_cached_refresh", lambda: None)
    epg.PROG_BUFFER.clear()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def guide(ids=(("npo1.nl", "NPO 1"),), title="Current show"):
    now = datetime.now(timezone.utc)
    root = ET.Element("tv")
    for cid, name in ids:
        channel = ET.SubElement(root, "channel", id=cid)
        ET.SubElement(channel, "display-name").text = name
        programme = ET.SubElement(
            root,
            "programme",
            channel=cid,
            start=epg._fmt_ts(now - timedelta(minutes=10)),
            stop=epg._fmt_ts(now + timedelta(hours=1)),
        )
        ET.SubElement(programme, "title").text = title
    return ET.tostring(root)


async def seed(channels=(("npo1.nl", "NPO 1"),), name="NPO 1 HD", epg_id=None):
    async with SessionLocal() as db:
        source = EpgSource(url="https://guide.test/nl.xml", enabled=True)
        live = LivePlaylist(
            custom_name=name, epg_id=epg_id, group_name="NL", enabled=True
        )
        user = User(
            name="u",
            password="p&word",
            m3u_enabled=True,
            xtream_enabled=False,
            groups_json=json.dumps({"live": ["NL"]}),
        )
        db.add_all([source, live, user])
        await db.flush()
        db.add_all(
            [
                EpgChannel(epg_source_id=source.id, tvg_id=cid, name=label)
                for cid, label in channels
            ]
        )
        await db.commit()
        return source.id, live.id, user


async def test_three_default_feeds_enabled_once_then_user_choice_wins():
    async with SessionLocal() as db:
        db.add(EpgSource(url=epg.DEFAULT_EPG_URLS[0], enabled=False))
        await db.commit()
    await epg.ensure_default_sources()
    async with SessionLocal() as db:
        rows = (await db.scalars(select(EpgSource))).all()
        assert {r.url for r in rows} == set(epg.DEFAULT_EPG_URLS)
        assert all(r.enabled for r in rows)
        rows[0].enabled = False
        await db.commit()
    await epg.ensure_default_sources()
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(EpgSource)) == 3
        assert not (await db.get(EpgSource, rows[0].id)).enabled


@pytest.mark.parametrize("pack", [lambda x: x, gzip.compress, lzma.compress])
def test_xml_gzip_and_xz(pack):
    raw = guide()
    assert epg._decode(pack(raw)) == raw


async def test_ambiguous_not_assigned_and_mirror_ids_deduplicated():
    sid, pid, _ = await seed((("a.nl", "NPO 1"), ("b.nl", "NPO 1")))
    async with SessionLocal() as db:
        mirror = EpgSource(url="http://mirror.test/nl.xz")
        db.add(mirror)
        await db.flush()
        db.add(EpgChannel(epg_source_id=mirror.id, tvg_id="a.nl", name="NPO 1 HD"))
        await db.commit()
    result = await epg.match_report()
    assert result["matched"] == 0 and len(result["ambiguous"]) == 1
    assert len(result["ambiguous"][0]["candidates"]) == 2
    async with SessionLocal() as db:
        assert (await db.get(LivePlaylist, pid)).epg_id is None
        (await db.get(EpgChannel, 2)).tvg_id = "a.nl-removed"
        (await db.get(EpgChannel, 2)).name = "Completely different channel"
        await db.commit()
    assert (await epg.match_report())["matched"] == 1
    async with SessionLocal() as db:
        assert (await db.get(LivePlaylist, pid)).epg_id == "a.nl"


async def test_existing_assignment_preserved_until_explicit_review():
    _, pid, _ = await seed(epg_id="manual.id")
    result = await epg.match_report()
    assert result["matched"] == 0 and result["ambiguous"][0]["epg_id"] == "manual.id"
    async with client() as c:
        r = await c.post(
            "/api/epg/match/assign",
            json={"items": [{"id": pid, "epg_id": "npo1.nl", "previous": "wrong"}]},
        )
        assert r.status_code == 409
        r = await c.post(
            "/api/epg/match/assign",
            json={"items": [{"id": pid, "epg_id": "npo1.nl", "previous": "manual.id"}]},
        )
        assert r.status_code == 200
    assert (await epg.match_report())["ambiguous"] == []
    assert len((await epg.match_report(review_all=True))["ambiguous"]) == 1


async def test_plus_and_numbers_are_not_equivalent():
    await seed(
        (
            ("tv.nl", "Viaplay TV"),
            ("plus.nl", "Viaplay TV+"),
            ("one.nl", "Viaplay 1"),
            ("two.nl", "Viaplay 2"),
        )
    )
    async with SessionLocal() as db:
        candidates = await epg.epg_candidates(db)
    assert [x["tvg_id"] for x in epg.rank_candidates("Viaplay TV+", candidates)] == [
        "plus.nl"
    ]
    assert "two.nl" not in [
        x["tvg_id"] for x in epg.rank_candidates("Viaplay 1", candidates)
    ]


async def test_disabled_guides_not_used_for_matches_or_programmes(monkeypatch):
    sid, pid, user = await seed()
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=guide()))
    assert (await epg.refresh_source(sid))["programmes"] == 1
    async with SessionLocal() as db:
        (await db.get(EpgSource, sid)).enabled = False
        await db.commit()
        assert await epg.epg_candidates(db) == []
    assert (
        ET.fromstring(await epg.build_xmltv("http://test", user)).findall("programme")
        == []
    )


async def test_import_match_cache_rematch_and_m3u_xtream_xmltv_agreement(monkeypatch):
    sid, pid, user = await seed((("one.nl", "NPO 1"), ("other.nl", "NPO 1")))
    download = AsyncMock(
        return_value=guide((("one.nl", "NPO 1"), ("other.nl", "NPO 1")))
    )
    monkeypatch.setattr(epg, "_fetch", download)
    result = await epg.refresh_source(sid)
    assert result["matched"] == 0 and result["programmes"] == 0
    await build_m3u(
        "http://test", user
    )  # prime output cache with unmatched fallback ID
    async with client() as c:
        assert (
            await c.post(
                "/api/epg/match/assign",
                json={"items": [{"id": pid, "epg_id": "one.nl", "previous": None}]},
            )
        ).status_code == 200
    await epg.reingest_cached_sources()
    download.assert_awaited_once()
    text = await build_m3u("http://test", user)
    assert 'tvg-id="one.nl"' in text
    assert (await xtream_live(user, "http://test"))[0]["epg_channel_id"] == "one.nl"
    async with client() as c:
        response = await c.get(
            "/xmltv.php", params={"username": "u", "password": "p&word"}
        )
        assert response.status_code == 200  # M3U-only users can retrieve their guide
        root = ET.fromstring(response.content)
        assert root.find("channel").get("id") == "one.nl"
        assert root.find("programme").get("channel") == "one.nl"
        users = (await c.get("/api/users")).json()["items"]
        assert "password=p%26word" in users[0]["epg_url"]
        assert (await c.get(users[0]["epg_url"])).status_code == 200
        assert (await c.get("/epg.xml?u=u&p=wrong")).status_code == 403


async def test_xmltv_deduplication_enabled_groups_and_fallback_ids(monkeypatch):
    sid, pid, user = await seed(epg_id="npo1.nl")
    async with SessionLocal() as db:
        db.add_all(
            [
                LivePlaylist(
                    custom_name="HD duplicate", epg_id="npo1.nl", group_name="NL"
                ),
                LivePlaylist(
                    custom_name="Disabled",
                    epg_id="disabled",
                    group_name="NL",
                    enabled=False,
                ),
                LivePlaylist(
                    custom_name="Restricted", epg_id="hidden", group_name="Private"
                ),
                LivePlaylist(custom_name="No guide", group_name="NL"),
            ]
        )
        await db.commit()
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=guide()))
    await epg.refresh_source(sid)
    xml = ET.fromstring(await epg.build_xmltv("http://test", user))
    ids = [r.get("id") for r in xml.findall("channel")]
    assert len(ids) == 2 and ids.count("npo1.nl") == 1
    assert len(xml.findall("programme")) == 1
    assert all(
        r["epg_channel_id"] in ids for r in await xtream_live(user, "http://test")
    )
    m3u = await build_m3u("http://test", user)
    assert all(f'tvg-id="{cid}"' in m3u for cid in ids)


async def test_bad_feed_does_not_poison_next_feed_or_last_success(monkeypatch):
    sid, _, _ = await seed()
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=b"<html>Error</html>"))
    result = await epg.refresh_source(sid)
    assert not result["ok"] and "XMLTV" in result["error"]
    async with SessionLocal() as db:
        row = await db.get(EpgSource, sid)
        assert row.last_fetch is None and "failed:" in row.status
    assert epg.PROG_BUFFER == []
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=guide()))
    assert (await epg.refresh_source(sid))["ok"]


async def test_scheduler_pause_validation_and_failure_backoff(monkeypatch):
    sid, _, _ = await seed()
    async with client() as c:
        assert (
            await c.post("/api/settings", json={"epg_refresh_hours": -1})
        ).status_code == 400
        assert (
            await c.post("/api/settings", json={"epg_refresh_hours": 1.5})
        ).status_code == 400
        assert (
            await c.post("/api/settings", json={"epg_refresh_hours": 0})
        ).status_code == 200
    run = AsyncMock(return_value={"ok": False})
    monkeypatch.setattr(epg, "refresh_source", run)
    assert await epg.refresh_due() == []
    run.assert_not_awaited()
    async with client() as c:
        await c.post("/api/settings", json={"epg_refresh_hours": 6})
    assert len(await epg.refresh_due()) == 1
    assert await epg.refresh_due() == []
    run.assert_awaited_once_with(sid)


async def test_portal_bulk_timezone_and_busy_mac(monkeypatch):
    from app.services import portal_epg
    from app.services.stream_manager import MANAGER

    async with SessionLocal() as db:
        portal = Portal(
            name="P",
            base_url="http://p/c/",
            resolved_url="http://p/portal.php",
            stb_timezone="Europe/Amsterdam",
        )
        db.add(portal)
        await db.flush()
        src = LiveSource(
            portal_id=portal.id, portal_channel_id="7", original_name="Portal TV"
        )
        mac = MacAddress(portal_id=portal.id, mac="00:1A:79:AA:AA:07", order=0)
        live = LivePlaylist(custom_name="Portal TV", group_name="NL")
        db.add_all([src, mac, live])
        await db.flush()
        db.add(
            LivePlaylistSource(
                live_playlist_id=live.id, live_source_id=src.id, priority=1
            )
        )
        await db.commit()
        pid, mid = portal.id, mac.id
    client = SimpleNamespace(
        epg_info=AsyncMock(
            return_value={
                "js": {
                    "7": [
                        {
                            "name": "Show",
                            "time": "2026-09-19 12:00:00",
                            "time_to": "2026-09-19 13:00:00",
                        }
                    ]
                }
            }
        ),
        short_epg=AsyncMock(),
        close=AsyncMock(),
    )
    get = AsyncMock(return_value=client)
    monkeypatch.setattr(portal_epg.POOL, "get", get)
    MANAGER.mac_locks[mid] = "another-viewer"
    try:
        with pytest.raises(ValueError, match="no free MAC"):
            await portal_epg.portal_xml(pid, "stable-namespace")
        get.assert_not_awaited()
    finally:
        MANAGER.mac_locks.pop(mid, None)
    raw, note = await portal_epg.portal_xml(pid, "stable-namespace")
    root = ET.fromstring(raw)
    assert root.find("channel").get("id") == "portal.stable-namespace.7"
    assert root.find("programme").get("start") == "20260919100000 +0000"
    client.short_epg.assert_not_awaited()
    assert not MANAGER.is_mac_busy(mid)
    client.close.assert_awaited_once()


async def test_portal_short_fallback_is_bounded_and_uses_declared_timezone(monkeypatch):
    from app.services import portal_epg
    from app.portal.epg import Programme
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(portal_epg, "MAX_SHORT_CHANNELS", 1)
    now = datetime.now(timezone.utc)
    client = SimpleNamespace(
        epg_info=AsyncMock(side_effect=ValueError("unsupported")),
        short_epg=AsyncMock(
            return_value=[
                Programme(title="Live", start=now, stop=now + timedelta(hours=1))
            ]
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr(portal_epg.POOL, "get", AsyncMock(return_value=client))
    sources = [
        SimpleNamespace(portal_channel_id=str(i), original_name="Channel")
        for i in range(4)
    ]
    raw, note = await portal_epg._read(
        SimpleNamespace(timezone="Europe/Amsterdam"), "key", sources
    )
    assert len(ET.fromstring(raw).findall("channel")) == 1
    assert client.short_epg.await_count == 1 and "limit" in note
    assert str(client.short_epg.call_args.kwargs["tz"]) == "Europe/Amsterdam"


def test_naive_database_timestamps_are_utc_and_xml_attributes_are_escaped():
    assert epg._fmt_ts(datetime(2026, 9, 19, 12)) == "20260919120000 +0000"
    assert epg._xmltv_ts("20260919140000 +0200").hour == 12
