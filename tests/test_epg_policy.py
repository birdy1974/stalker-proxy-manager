"""Guide policies: raw UTC storage, independent feeds, upgrades and shared output."""

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, select

from app.database import (
    SessionLocal,
    engine,
    _add_missing_columns,
    _migrate_epg_programme_key,
)
from app.main import app
from app.models import (
    Base,
    EpgChannel,
    EpgChannelSource,
    EpgProgramme,
    EpgSource,
    LivePlaylist,
    Setting,
    User,
)
from app.routers import api_epg
from app.services import epg, epg_policy as policy
from app.services.playlist_gen import build_m3u, xtream_live

QUEUE_CACHED_REFRESH = api_epg.queue_cached_refresh
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(epg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api_epg, "queue_cached_refresh", lambda: None)
    epg.PROG_BUFFER.clear()


async def seed():
    async with SessionLocal() as db:
        sources = [
            EpgSource(url=f"https://feed{i}.test/epg.xml", last_fetch=NOW)
            for i in range(2)
        ]
        live = LivePlaylist(custom_name="Test channel", epg_id="main", group_name="NL")
        user = User(
            name="guide",
            password="secret",
            m3u_enabled=True,
            xtream_enabled=True,
            groups_json=json.dumps({"live": ["NL"]}),
        )
        db.add_all([*sources, live, user])
        await db.flush()
        for s, cid in zip(sources, ["main", "alias"]):
            db.add(EpgChannel(epg_source_id=s.id, tvg_id=cid, name="Test channel"))
        await db.commit()
        return [s.id for s in sources], live.id, user


def mapping(sid, cid="main", offset=0):
    return {"source_id": sid, "tvg_id": cid, "offset_minutes": offset}


async def programme(sid, cid, start, stop, title):
    async with SessionLocal() as db:
        p = EpgProgramme(
            epg_source_id=sid,
            tvg_id=cid,
            start_ts=NOW + timedelta(minutes=start),
            stop_ts=NOW + timedelta(minutes=stop),
            title=title,
        )
        db.add(p)
        await db.commit()
        return p.id


async def apply(lid, **payload):
    async with client() as c:
        r = await c.put(f"/api/epg/policy/{lid}", json=payload)
        assert r.status_code == 200, r.text
        return r.json()


async def events(lid):
    async with SessionLocal() as db:
        item = await db.get(LivePlaylist, lid)
        schedules, _, _, _ = await policy.load_schedules(db, [item], NOW)
        return [
            (
                e.programme.title,
                int((e.start - NOW).total_seconds() / 60),
                int((e.stop - NOW).total_seconds() / 60),
            )
            for e in schedules[lid]
        ]


def test_gap_fill_splits_and_clips_without_overlap():
    def event(start, stop, ident):
        return policy.GuideEvent(
            SimpleNamespace(id=ident),
            NOW + timedelta(minutes=start),
            NOW + timedelta(minutes=stop),
            ident,
        )

    layers = [[event(10, 20, 1), event(30, 40, 2)], [event(0, 50, 3)]]
    out = policy.fill_gaps(layers)
    assert [
        (
            e.source_id,
            int((e.start - NOW).total_seconds() / 60),
            int((e.stop - NOW).total_seconds() / 60),
        )
        for e in out
    ] == [(3, 0, 10), (1, 10, 20), (3, 20, 30), (2, 30, 40), (3, 40, 50)]
    assert len(policy.fill_gaps(layers, fill=False)) == 2
    assert policy.fill_gaps([[event(1, 1, 4)]]) == []


async def test_alias_priority_gap_disable_source_disable_and_offsets():
    (a, b), lid, _ = await seed()
    await programme(a, "main", 0, 60, "Primary")
    await programme(b, "alias", -30, 120, "Fallback")
    await apply(lid, mappings=[mapping(a), mapping(b, "alias")])
    assert await events(lid) == [
        ("Fallback", -30, 0),
        ("Primary", 0, 60),
        ("Fallback", 60, 120),
    ]
    await apply(lid, mappings=[mapping(a), mapping(b, "alias")], gap_fill=False)
    assert await events(lid) == [("Primary", 0, 60)]
    await apply(
        lid, mappings=[mapping(a, offset=15), mapping(b, "alias")], offset_minutes=30
    )
    assert await events(lid) == [
        ("Fallback", 0, 45),
        ("Primary", 45, 105),
        ("Fallback", 105, 150),
    ]
    async with client() as c:
        assert (
            await c.patch(f"/api/epg/sources/{a}", json={"enabled": False})
        ).status_code == 200
    assert await events(lid) == [("Fallback", 0, 150)]
    await apply(lid, mappings=[mapping(b, "alias"), mapping(a)])
    assert await events(lid) == [("Fallback", -30, 120)]


async def test_automatic_priority_empty_primary_no_fill_and_independent_duplicate_keys():
    (a, b), lid, _ = await seed()
    await programme(a, "main", 0, 60, "Same show")
    await programme(b, "main", 0, 120, "Same show")
    assert await events(lid) == [("Same show", 0, 60), ("Same show", 60, 120)]
    async with SessionLocal() as db:
        await db.execute(delete(EpgProgramme).where(EpgProgramme.epg_source_id == a))
        await db.commit()
    await apply(lid, gap_fill=False)
    assert (
        await events(lid) == []
    )  # known primary with an empty snapshot is not silently skipped
    await apply(lid, gap_fill=True)
    assert await events(lid) == [("Same show", 0, 120)]


async def test_output_ids_cache_invalidation_alias_isolation_and_raw_utc():
    (a, b), lid, user = await seed()
    await programme(a, "main", 0, 60, "Primary & news")
    async with SessionLocal() as db:
        alias = LivePlaylist(custom_name="Delayed", epg_id="main", group_name="NL")
        db.add(alias)
        await db.commit()
        alias_id = alias.id
    assert 'tvg-id="main"' in await build_m3u("http://test", user)
    await apply(lid, mappings=[mapping(a)], offset_minutes=60)
    await apply(alias_id, mappings=[mapping(a)], offset_minutes=-30)
    xml = ET.fromstring(await epg.build_xmltv("http://test", user))
    entries = xml.findall("programme")
    assert {p.attrib["channel"] for p in entries} == {
        f"spm.live.{lid}",
        f"spm.live.{alias_id}",
    }
    assert epg._xmltv_ts(entries[0].attrib["start"]) == NOW + timedelta(hours=1)
    assert epg._xmltv_ts(entries[1].attrib["start"]) == NOW - timedelta(minutes=30)
    m3u = await build_m3u("http://test", user)
    assert 'tvg-id="main"' not in m3u and f'tvg-id="spm.live.{lid}"' in m3u
    assert {r["epg_channel_id"] for r in await xtream_live(user, "http://test")} == {
        f"spm.live.{lid}",
        f"spm.live.{alias_id}",
    }
    async with SessionLocal() as db:
        raw = await db.scalar(select(EpgProgramme))
        assert policy.aware(raw.start_ts) == NOW
    await apply(lid, offset_minutes=60, mappings=[mapping(a)])
    assert await events(lid) == [("Primary & news", 60, 120)]  # no compounding


@pytest.mark.parametrize(
    "payload",
    [
        {"offset_minutes": 1441},
        {"offset_minutes": 1.5},
        {"offset_minutes": True},
        {"gap_fill": "yes"},
        {"mappings": [mapping(999)]},
        {"mappings": [mapping(1, " ")]},
        {"mappings": [mapping(1), mapping(1)]},
    ],
)
async def test_policy_validation_and_atomic_playlist_update(payload):
    _, lid, _ = await seed()
    async with client() as c:
        r = await c.put(
            f"/api/playlist/live/{lid}",
            json={"custom_name": "Must roll back", "epg_policy": payload},
        )
        assert r.status_code in (400, 422), r.text
    async with SessionLocal() as db:
        assert (await db.get(LivePlaylist, lid)).custom_name == "Test channel"
        assert not (await db.scalars(select(EpgChannelSource))).all()


async def test_create_and_read_policy_atomic_and_catalog_search():
    (a, b), _, _ = await seed()
    async with client() as c:
        r = await c.post(
            "/api/playlist/live",
            json={
                "custom_name": "New guide",
                "epg_policy": {
                    "mappings": [mapping(b, "alias")],
                    "offset_minutes": -15,
                },
            },
        )
        assert r.status_code == 200, r.text
        lid = r.json()["id"]
        saved = (await c.get(f"/api/epg/policy/{lid}")).json()
        assert saved["policy"]["offset_minutes"] == -15
        assert saved["policy"]["mappings"] == [mapping(b, "alias")]
        assert saved["output_epg_id"] == f"spm.live.{lid}"
        r = await c.get(f"/api/epg/channels?source_id={b}&q=alias")
        assert [row["tvg_id"] for row in r.json()["rows"]] == ["alias"]
        r = await c.post(
            "/api/playlist/live",
            json={"custom_name": "Invalid", "epg_policy": {"mappings": [mapping(999)]}},
        )
        assert r.status_code == 400
    async with SessionLocal() as db:
        assert not await db.scalar(
            select(LivePlaylist).where(LivePlaylist.custom_name == "Invalid")
        )


@pytest.mark.parametrize("legacy", [False, True])
async def test_deleting_last_configured_source_does_not_enable_arbitrary_fallback(
    legacy,
):
    (a, b), lid, _ = await seed()
    await programme(a, "main", 0, 60, "Primary")
    await programme(b, "main", 0, 60, "Unrequested")
    await apply(lid, mappings=[mapping(a)])
    async with client() as c:
        path = f"/api/epg-sources/{a}" if legacy else f"/api/epg/sources/{a}"
        r = await c.delete(path)
        assert r.status_code == 200, r.text
    assert await events(lid) == []
    async with SessionLocal() as db:
        live = await db.get(LivePlaylist, lid)
        assert live.epg_sources_explicit and live.epg_custom
        assert not (await db.scalars(select(EpgChannelSource))).all()
    await apply(lid)  # explicitly reset to automatic
    assert await events(lid) == [("Unrequested", 0, 60)]


async def test_health_missing_current_gap_stale_and_failed_not_disabled():
    (a, b), lid, _ = await seed()
    await programme(a, "main", -10, 60, "Old feed")
    async with SessionLocal() as db:
        s = await db.get(EpgSource, a)
        s.last_fetch = NOW - timedelta(hours=5)
        s.stale_hours = 2
        s.last_error = "Download failed"
        (await db.get(EpgSource, b)).enabled = False
        db.add_all(
            [
                LivePlaylist(custom_name="Missing", epg_id="none"),
                LivePlaylist(custom_name="Future", epg_id="future"),
                LivePlaylist(custom_name="Disabled", enabled=False),
            ]
        )
        await db.commit()
    await programme(a, "future", 60, 120, "Next")
    async with SessionLocal() as db:
        d = await policy.coverage_report(db, NOW)
    assert d["channels_checked"] == 3
    assert d["counts"] == {"missing": 1, "gap_now": 1, "stale": 2}
    assert len(d["sources"]) == 1 and d["sources"][0]["flags"] == [
        "stale",
        "refresh_failed",
    ]
    async with client() as c:
        r = await c.get("/api/epg/health")
        assert r.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"refresh_hours": -1},
        {"refresh_hours": 169},
        {"refresh_hours": 1.2},
        {"refresh_hours": True},
        {"stale_hours": 0},
        {"stale_hours": 721},
        {"enabled": None},
    ],
)
async def test_source_settings_reject_invalid_values(body):
    (a, _), _, _ = await seed()
    async with client() as c:
        assert (await c.patch(f"/api/epg/sources/{a}", json=body)).status_code == 422


async def test_source_schedules_override_manual_pause_and_failure_backoff(monkeypatch):
    (a, b), _, _ = await seed()
    async with SessionLocal() as db:
        db.add(Setting(key="epg_refresh_hours", value="24"))
        for sid in (a, b):
            s = await db.get(EpgSource, sid)
            s.last_fetch = NOW - timedelta(hours=10)
        await db.commit()
    async with client() as c:
        assert (
            await c.patch(
                f"/api/epg/sources/{a}", json={"refresh_hours": 6, "stale_hours": 12}
            )
        ).status_code == 200
        await c.patch(f"/api/epg/sources/{b}", json={"refresh_hours": 0})
        rows = (await c.get("/api/epg")).json()["sources"]
        assert rows[0]["effective_refresh_hours"] == 6 and rows[0]["next_refresh"]
        assert rows[1]["next_refresh"] is None
    refresh = AsyncMock(return_value={"ok": False})
    monkeypatch.setattr(epg, "refresh_source", refresh)
    await epg.refresh_due()
    refresh.assert_awaited_once_with(a)
    await epg.refresh_due()
    assert refresh.await_count == 1
    async with SessionLocal() as db:
        (await db.get(Setting, "epg_refresh_hours")).value = "0"
        (await db.get(EpgSource, a)).last_attempt = NOW - timedelta(days=2)
        await db.commit()
    assert await epg.refresh_due() == []
    async with client() as c:
        rows = (await c.get("/api/epg")).json()["sources"]
        assert all(r["next_refresh"] is None for r in rows)
        await c.patch(
            f"/api/epg/sources/{a}", json={"refresh_hours": None, "stale_hours": None}
        )
    assert (
        policy.stale_limit(SimpleNamespace(stale_hours=None, refresh_hours=72), 24)
        == 144
    )


async def test_atomic_source_snapshots_cached_alias_ingestion_failure_and_empty(
    monkeypatch,
):
    (a, b), lid, _ = await seed()
    await apply(lid, mappings=[mapping(b, "alias")])

    def xml(title):
        return f'<tv><channel id="alias"><display-name>Alias</display-name></channel><programme channel="alias" start="{epg._fmt_ts(NOW)}" stop="{epg._fmt_ts(NOW+timedelta(hours=1))}"><title>{title}</title></programme></tv>'.encode()

    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=xml("Original")))
    assert (await epg.refresh_source(b))["ok"]
    await programme(a, "alias", 0, 60, "Other source")
    assert await events(lid) == [("Original", 0, 60)]
    monkeypatch.setattr(epg, "_fetch", AsyncMock(side_effect=RuntimeError("offline")))
    assert not (await epg.refresh_source(b))["ok"]
    assert await events(lid) == [("Original", 0, 60)]
    async with SessionLocal() as db:
        row = await db.get(EpgSource, b)
        last_success = row.last_fetch
        assert row.last_error == "offline" and row.last_attempt
    assert (await epg.refresh_source(b, cached=True))["ok"]
    async with SessionLocal() as db:
        row = await db.get(EpgSource, b)
        assert row.last_fetch == last_success and row.last_error == "offline"
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=b"<tv/>"))
    assert (await epg.refresh_source(b))["ok"]
    assert await events(lid) == []
    async with SessionLocal() as db:
        rows = (await db.scalars(select(EpgProgramme))).all()
        assert len(rows) == 1 and rows[0].epg_source_id == a
        assert (await db.get(EpgSource, b)).last_error is None


async def test_sqlite_upgrade_preserves_ids_data_and_orphans_and_is_repeatable():
    (a, b), lid, _ = await seed()
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE epg_programmes")
        await conn.exec_driver_sql("""CREATE TABLE epg_programmes (
            id INTEGER PRIMARY KEY, epg_source_id INTEGER, tvg_id VARCHAR(200) NOT NULL,
            start_ts DATETIME NOT NULL, stop_ts DATETIME NOT NULL, title VARCHAR(400) NOT NULL,
            sub_title VARCHAR(400), desc TEXT, category VARCHAR(200), icon VARCHAR(600),
            CONSTRAINT uq_epg_prog_natural UNIQUE(tvg_id,start_ts,title))""")
        await conn.exec_driver_sql(
            "INSERT INTO epg_programmes(id,epg_source_id,tvg_id,start_ts,stop_ts,title,desc) VALUES (7,?, 'main','2026-09-19 10:00:00','2026-09-19 11:00:00','Original','Keep metadata'),(11,999,'orphan','2026-09-19 10:00:00','2026-09-19 11:00:00','Legacy',NULL)",
            (a,),
        )
        for column in (
            "epg_sources_explicit",
            "epg_custom",
            "epg_gap_fill",
            "epg_offset_minutes",
        ):
            await conn.exec_driver_sql(
                f"ALTER TABLE live_playlist DROP COLUMN {column}"
            )
        for column in ("refresh_hours", "stale_hours", "last_attempt", "last_error"):
            await conn.exec_driver_sql(f"ALTER TABLE epg_sources DROP COLUMN {column}")
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_migrate_epg_programme_key)
        await conn.run_sync(_migrate_epg_programme_key)
        keys = await conn.run_sync(
            lambda c: inspect(c).get_unique_constraints("epg_programmes")
        )
        assert any(
            k["column_names"] == ["epg_source_id", "tvg_id", "start_ts", "title"]
            for k in keys
        )
        await conn.exec_driver_sql(
            "INSERT INTO epg_programmes(epg_source_id,tvg_id,start_ts,stop_ts,title) VALUES (?,'main','2026-09-19 10:00:00','2026-09-19 11:00:00','Original')",
            (b,),
        )
        assert not (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()
    async with SessionLocal() as db:
        assert (await db.get(EpgProgramme, 7)).desc == "Keep metadata"
        assert (await db.get(EpgProgramme, 11)).epg_source_id is None
        live = await db.get(LivePlaylist, lid)
        assert (
            live.epg_gap_fill and not live.epg_custom and live.epg_offset_minutes == 0
        )


async def test_complete_backup_restores_feed_duplicates_and_mapping_references():
    (a, b), lid, _ = await seed()
    await programme(a, "main", 0, 60, "Duplicate")
    await programme(b, "main", 0, 60, "Duplicate")
    await apply(lid, mappings=[mapping(b), mapping(a)], offset_minutes=15)
    async with client() as c:
        data = (await c.post("/api/backup/export", json={})).json()
        async with SessionLocal() as db:
            for table in reversed(Base.metadata.sorted_tables):
                await db.execute(table.delete())
            db.add(EpgSource(id=100, url="https://unrelated.test/epg"))
            await db.commit()
        r = await c.post(
            "/api/backup/restore", json={"data": data, "confirm_add_only": True}
        )
        assert r.status_code == 200, r.text
    async with SessionLocal() as db:
        rows = (await db.scalars(select(EpgProgramme))).all()
        assert len(rows) == 2 and len({r.epg_source_id for r in rows}) == 2
        mappings = (
            await db.scalars(
                select(EpgChannelSource).order_by(EpgChannelSource.priority)
            )
        ).all()
        assert [m.epg_source_id for m in mappings] == [102, 101]
        live = await db.get(LivePlaylist, lid)
        assert live.epg_custom and live.epg_offset_minutes == 15


async def test_cache_rematch_requests_during_ingestion_are_not_lost(monkeypatch):
    # Restore the actual queue function shadowed by this module's isolation fixture.
    queue = QUEUE_CACHED_REFRESH
    monkeypatch.setattr(api_epg, "_cache_task", None)
    monkeypatch.setattr(api_epg, "_refresh_task", None)
    monkeypatch.setattr(api_epg, "_cache_pending", False)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def ingest():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(epg, "reingest_cached_sources", ingest)
    queue()
    await asyncio.wait_for(entered.wait(), 2)
    queue()
    release.set()
    await asyncio.wait_for(api_epg._cache_task, 2)
    assert len(calls) == 2


@pytest.mark.parametrize("offset", [-1440, 1440])
async def test_combined_offset_extremes_select_raw_events_outside_export_window(offset):
    (a, _), lid, _ = await seed()
    await programme(a, "main", -2 * offset, -2 * offset + 60, "Shifted")
    await apply(lid, offset_minutes=offset, mappings=[mapping(a, offset=offset)])
    assert await events(lid) == [("Shifted", 0, 60)]


async def test_mapping_table_only_restore_derives_custom_output_without_overwriting_channel():
    (a, _), lid, user = await seed()
    await apply(lid, mappings=[mapping(a)])
    async with client() as c:
        data = (
            await c.post("/api/backup/export", json={"tables": ["epg_channel_sources"]})
        ).json()
        # A destination channel with the same identity keeps its local settings.
        await apply(lid)
        r = await c.post(
            "/api/backup/restore", json={"data": data, "confirm_add_only": True}
        )
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 1
        result = (await c.get(f"/api/epg/policy/{lid}")).json()
        assert result["output_epg_id"] == f"spm.live.{lid}"
        assert result["policy"]["mappings"] == [mapping(a)]
    async with SessionLocal() as db:
        live = await db.get(LivePlaylist, lid)
        assert not live.epg_custom and not live.epg_sources_explicit
        assert live.epg_has_mappings  # derived, not a changed stored setting
    assert f'tvg-id="spm.live.{lid}"' in await build_m3u("http://test", user)
    await apply(lid)  # reset must not retain the pre-save derived flag
    async with client() as c:
        assert (await c.get(f"/api/epg/policy/{lid}")).json()["output_epg_id"] == "main"


async def test_disabled_portal_is_not_scheduled_or_reported_as_due(monkeypatch):
    from app.models import Portal

    async with SessionLocal() as db:
        portal = Portal(name="Disabled", base_url="https://portal.test", enabled=False)
        db.add(portal)
        await db.flush()
        source = EpgSource(
            url="portal://disabled", portal_id=portal.id, refresh_hours=1
        )
        db.add(source)
        await db.commit()
    refresh = AsyncMock()
    monkeypatch.setattr(epg, "refresh_source", refresh)
    assert await epg.refresh_due() == []
    refresh.assert_not_awaited()
    async with client() as c:
        assert (await c.get("/api/epg")).json()["sources"][0]["next_refresh"] is None


async def test_cache_upgrade_preserves_legacy_attempt_and_error_once(monkeypatch):
    (a, b), _, _ = await seed()
    async with SessionLocal() as db:
        source = await db.get(EpgSource, a)
        source.status = "failed: old download failed"
        db.add(
            Setting(
                key=f"epg_attempt_{epg._source_key(source.url)}",
                value=json.dumps(NOW.timestamp()),
            )
        )
        other = await db.get(EpgSource, b)
        db.add(
            Setting(key=f"epg_attempt_{epg._source_key(other.url)}", value="invalid")
        )
        await db.commit()
    reindex = AsyncMock()
    monkeypatch.setattr(epg, "reingest_cached_sources", reindex)
    await epg.reindex_source_snapshots()
    await epg.reindex_source_snapshots()
    reindex.assert_awaited_once()
    async with SessionLocal() as db:
        source = await db.get(EpgSource, a)
        assert policy.aware(source.last_attempt) == NOW
        assert source.last_error == "old download failed"
        assert (await db.get(EpgSource, b)).last_attempt is None
