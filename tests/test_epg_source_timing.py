"""Source timezone interpretation, DST, non-compounding corrections and preview."""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal, engine, _add_missing_columns
from app.main import app
from app.models import EpgSource, EpgProgramme, LivePlaylist, Portal, User
from app.routers import api_epg
from app.services import epg, epg_policy
from app.services.epg_timing import (
    SourceTiming,
    parse_time,
    programme_times,
    timing_pending,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(epg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api_epg, "queue_cached_refresh", lambda: None)
    epg.PROG_BUFFER.clear()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "stamp,mode,zone,expected",
    [
        ("20260719120000 +0200", "auto", None, "2026-07-19T10:00:00+00:00"),
        ("20260719120000", "auto", None, "2026-07-19T12:00:00+00:00"),
        ("20260719120000 Z", "auto", None, "2026-07-19T12:00:00+00:00"),
        ("20260719120000", "missing", "Europe/Amsterdam", "2026-07-19T10:00:00+00:00"),
        ("20260119120000", "missing", "Europe/Amsterdam", "2026-01-19T11:00:00+00:00"),
        (
            "20260719120000 +0000",
            "missing",
            "Europe/Amsterdam",
            "2026-07-19T12:00:00+00:00",
        ),
        (
            "20260719120000 +0000",
            "override",
            "Europe/Amsterdam",
            "2026-07-19T10:00:00+00:00",
        ),
        (
            "20260119120000 -0500",
            "override",
            "Europe/Amsterdam",
            "2026-01-19T11:00:00+00:00",
        ),
        ("20260719120000", "missing", "Asia/Kathmandu", "2026-07-19T06:15:00+00:00"),
    ],
)
def test_xmltv_timezone_modes_and_seasonal_offsets(stamp, mode, zone, expected):
    settings = SourceTiming(timezone_mode=mode, timezone_name=zone)
    assert parse_time(stamp, settings).utc.isoformat() == expected


def test_dst_hole_and_repeated_hour_are_explicit():
    settings = SourceTiming(timezone_mode="missing", timezone_name="Europe/Amsterdam")
    with pytest.raises(ValueError, match="Nonexistent"):
        parse_time("20260329023000", settings)
    result = parse_time("20261025023000", settings)
    assert result.utc.isoformat() == "2026-10-25T00:30:00+00:00"
    assert "first occurrence" in result.warning
    # An explicit offset disambiguates the second occurrence in missing-only mode.
    assert parse_time("20261025023000 +0100", settings).utc.hour == 1


@pytest.mark.parametrize(
    "stamp",
    ["20260230090000", "20260919120000 +2460", "20260919120000 +0060", "not a date"],
)
def test_invalid_xmltv_timestamps_are_not_guessed(stamp):
    assert epg._xmltv_ts(stamp) is None


@pytest.mark.parametrize(
    "stamp,mode,expected",
    [
        ("2026-07-19 12:00:00", "auto", "2026-07-19T10:00:00+00:00"),
        ("2026-07-19 12:00:00", "missing", "2026-07-19T11:00:00+00:00"),
        ("2026-07-19T12:00:00+02:00", "missing", "2026-07-19T10:00:00+00:00"),
        ("2026-07-19T12:00:00+02:00", "override", "2026-07-19T11:00:00+00:00"),
    ],
)
def test_original_portal_wall_clock_interpretation(stamp, mode, expected):
    settings = SourceTiming(timezone_mode=mode, timezone_name="Europe/London")
    assert (
        parse_time(
            stamp, settings, portal=True, default_timezone="Europe/Amsterdam"
        ).utc.isoformat()
        == expected
    )


def test_unix_portal_times_remain_absolute_and_old_cache_is_not_reinterpreted():
    settings = SourceTiming(timezone_mode="override", timezone_name="Europe/Amsterdam")
    stamp = datetime(2026, 7, 19, 12, tzinfo=timezone.utc).timestamp()
    result = parse_time(str(stamp), settings, portal=True)
    assert result.utc.hour == 12 and "absolute" in result.warning
    attrs = {"start": "20260719120000 +0000", "stop": "20260719130000 +0000"}
    with pytest.raises(ValueError, match="refresh the portal"):
        programme_times(attrs, {}, settings, portal=True)
    assert programme_times(attrs, {}, SourceTiming(), portal=True)[0].utc.hour == 12


async def seed(*, portal=False):
    async with SessionLocal() as db:
        src = EpgSource(
            url="portal://test" if portal else "https://timing.test/guide.xml"
        )
        live = LivePlaylist(custom_name="Timing channel", epg_id="one", group_name="NL")
        user = User(
            name="timing",
            password="secret",
            m3u_enabled=True,
            groups_json=json.dumps({"live": ["NL"]}),
        )
        db.add_all([src, live, user])
        await db.commit()
        return src, live, user


def guide(start, stop, *, portal=False):
    root = ET.Element(
        "tv",
        {"spm-raw-times": "1", "spm-timezone": "Europe/Amsterdam"} if portal else {},
    )
    channel = ET.SubElement(root, "channel", id="one")
    ET.SubElement(channel, "display-name").text = "Timing channel"
    attrs = {"channel": "one", "start": start, "stop": stop}
    if portal:
        attrs.update(
            {
                "spm-start": start,
                "spm-stop": stop,
                "start": "20260719100000 +0000",
                "stop": "20260719110000 +0000",
            }
        )
    prog = ET.SubElement(root, "programme", attrs)
    ET.SubElement(prog, "title").text = "Clock test <safe>"
    return ET.tostring(root)


@pytest.mark.parametrize(
    "body",
    [
        {"timezone_mode": "guess"},
        {"timezone_mode": None},
        {"timezone_mode": "missing"},
        {"timezone_mode": "override", "timezone_name": "Not/AZone"},
        {"timezone_name": "/etc/passwd"},
        {"offset_minutes": True},
        {"offset_minutes": 1.5},
        {"offset_minutes": 1441},
        {"offset_minutes": -1441},
        {"offset_minutes": None},
    ],
)
async def test_invalid_settings_rejected_atomically(body):
    src, _, _ = await seed()
    async with client() as c:
        r = await c.patch(
            f"/api/epg/sources/{src.id}", json={**body, "refresh_hours": 6}
        )
        assert r.status_code == 422, r.text
    async with SessionLocal() as db:
        saved = await db.get(EpgSource, src.id)
        assert (
            saved.refresh_hours is None
            and saved.timezone_mode == "auto"
            and saved.offset_minutes == 0
        )


async def test_source_offset_combines_with_channel_mapping_and_health_without_rewriting_utc(
    monkeypatch,
):
    src, live, user = await seed()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    raw = guide(
        epg._fmt_ts(now - timedelta(minutes=10)),
        epg._fmt_ts(now + timedelta(minutes=40)),
    )
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=raw))
    assert (await epg.refresh_source(src.id))["ok"]
    async with client() as c:
        assert (
            await c.patch(f"/api/epg/sources/{src.id}", json={"offset_minutes": 60})
        ).status_code == 200
        assert (
            await c.put(
                f"/api/epg/policy/{live.id}",
                json={
                    "offset_minutes": 15,
                    "mappings": [
                        {"source_id": src.id, "tvg_id": "one", "offset_minutes": -5}
                    ],
                },
            )
        ).status_code == 200
    for _ in range(2):
        assert (await epg.refresh_source(src.id, cached=True))["ok"]
        xml = ET.fromstring(await epg.build_xmltv("http://test", user))
        assert epg._xmltv_ts(xml.find("programme").attrib["start"]) == now + timedelta(
            minutes=60
        )
        async with SessionLocal() as db:
            stored = await db.scalar(select(EpgProgramme))
            assert epg_policy.aware(stored.start_ts) == now - timedelta(minutes=10)
            report = await epg_policy.coverage_report(db, now)
            assert report["counts"]["gap_now"] == 1
    async with client() as c:
        await c.patch(f"/api/epg/sources/{src.id}", json={"offset_minutes": -10})
        health = (await c.get("/api/epg/health")).json()
        assert health["counts"]["gap_now"] == 0


async def test_timezone_change_reprocesses_original_cache_once_and_preserves_freshness(
    monkeypatch,
):
    src, _, _ = await seed()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    local = now.astimezone(ZoneInfo("Europe/Amsterdam"))
    raw = guide(
        local.strftime("%Y%m%d%H%M%S"),
        (local + timedelta(hours=1)).strftime("%Y%m%d%H%M%S"),
    )
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=raw))
    await epg.refresh_source(src.id)
    async with SessionLocal() as db:
        before = (await db.get(EpgSource, src.id)).last_fetch
    queued = []
    monkeypatch.setattr(api_epg, "queue_cached_refresh", lambda: queued.append(1))
    async with client() as c:
        r = await c.patch(
            f"/api/epg/sources/{src.id}",
            json={"timezone_mode": "missing", "timezone_name": "Europe/Amsterdam"},
        )
        assert (
            r.json()["timing_pending"]
            and "queued" in r.json()["message"]
            and queued == [1]
        )
        assert (
            "timing_pending"
            in (await c.get("/api/epg/health")).json()["sources"][0]["flags"]
        )
    for _ in range(2):
        assert (await epg.refresh_source(src.id, cached=True))["ok"]
        async with SessionLocal() as db:
            saved = await db.get(EpgSource, src.id)
            assert not timing_pending(saved) and saved.last_fetch == before
            assert (
                epg_policy.aware((await db.scalar(select(EpgProgramme))).start_ts)
                == now
            )
    async with client() as c:
        await c.patch(
            f"/api/epg/sources/{src.id}",
            json={"timezone_mode": "auto", "timezone_name": None, "offset_minutes": 0},
        )
    await epg.refresh_source(src.id, cached=True)
    async with SessionLocal() as db:
        assert epg_policy.aware(
            (await db.scalar(select(EpgProgramme))).start_ts
        ) == local.replace(tzinfo=timezone.utc)


async def test_preview_is_offline_read_only_uses_original_and_accepts_entered_example(
    monkeypatch,
):
    src, _, _ = await seed()
    epg._cache_write(
        src.id, src.url, guide("20260719120000 +0000", "20260719130000 +0000")
    )
    fetch = AsyncMock(side_effect=AssertionError("preview must not fetch"))
    monkeypatch.setattr(epg, "_fetch", fetch)
    async with client() as c:
        r = await c.post(
            f"/api/epg/sources/{src.id}/timing-preview",
            json={
                "timezone_mode": "override",
                "timezone_name": "Europe/Amsterdam",
                "offset_minutes": 30,
            },
        )
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["original_start"] == "20260719120000 +0000"
        assert result["automatic_utc"] == "2026-07-19T12:00:00+00:00"
        assert result["interpreted_utc"] == "2026-07-19T10:00:00+00:00"
        assert result["corrected_start"] == "2026-07-19T10:30:00+00:00"
        assert result["title"] == "Clock test <safe>"
        r = await c.post(
            f"/api/epg/sources/{src.id}/timing-preview",
            json={
                "timezone_mode": "missing",
                "timezone_name": "Europe/Amsterdam",
                "sample_start": "20260329023000",
            },
        )
        assert r.status_code == 422 and "Nonexistent" in r.text
        zones = (await c.get("/api/epg/timezones")).json()["timezones"]
        assert "Europe/Amsterdam" in zones and "UTC" in zones
    fetch.assert_not_awaited()
    async with SessionLocal() as db:
        saved = await db.get(EpgSource, src.id)
        assert (
            saved.timezone_mode == "auto"
            and saved.offset_minutes == 0
            and saved.last_attempt is None
        )
        assert not (await db.scalars(select(EpgProgramme))).all()


async def test_no_cache_disables_neither_save_nor_manual_preview():
    src, _, _ = await seed()
    async with client() as c:
        r = await c.post(f"/api/epg/sources/{src.id}/timing-preview", json={})
        assert not r.json()["available"]
        r = await c.patch(
            f"/api/epg/sources/{src.id}",
            json={"timezone_mode": "override", "timezone_name": "Europe/London"},
        )
        assert "No cached guide" in r.json()["message"] and r.json()["timing_pending"]
        r = await c.post(
            f"/api/epg/sources/{src.id}/timing-preview",
            json={"sample_start": "20260119120000 +0000", "offset_minutes": -60},
        )
        assert r.json()["corrected_start"] == "2026-01-19T11:00:00+00:00"


async def test_original_portal_cache_can_be_reinterpreted_but_legacy_cache_requires_refresh():
    src, _, _ = await seed(portal=True)
    raw = guide("2026-07-19 12:00:00", "2026-07-19 13:00:00", portal=True)
    epg._cache_write(src.id, src.url, raw)
    async with client() as c:
        r = await c.post(
            f"/api/epg/sources/{src.id}/timing-preview",
            json={"timezone_mode": "override", "timezone_name": "Europe/London"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["original_start"] == "2026-07-19 12:00:00"
        assert r.json()["corrected_start"] == "2026-07-19T11:00:00+00:00"
        await c.patch(
            f"/api/epg/sources/{src.id}",
            json={"timezone_mode": "override", "timezone_name": "Europe/London"},
        )
    epg._cache_write(
        src.id, src.url, guide("20260719100000 +0000", "20260719110000 +0000")
    )
    r = await epg.refresh_source(src.id, cached=True)
    assert not r["ok"] and "refresh the portal" in r["error"]
    async with SessionLocal() as db:
        assert timing_pending(await db.get(EpgSource, src.id))


@pytest.mark.parametrize("offset", [-1440, 1440])
async def test_three_day_combined_corrections_have_sufficient_ingestion_and_query_window(
    monkeypatch, offset
):
    src, live, user = await seed()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=3 * offset)
    raw = guide(epg._fmt_ts(start), epg._fmt_ts(start + timedelta(hours=1)))
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=raw))
    async with client() as c:
        await c.patch(f"/api/epg/sources/{src.id}", json={"offset_minutes": offset})
        await c.put(
            f"/api/epg/policy/{live.id}",
            json={
                "offset_minutes": offset,
                "mappings": [
                    {"source_id": src.id, "tvg_id": "one", "offset_minutes": offset}
                ],
            },
        )
    assert (await epg.refresh_source(src.id))["programmes"] == 1
    xml = ET.fromstring(await epg.build_xmltv("http://test", user))
    assert epg._xmltv_ts(xml.find("programme").attrib["start"]) == now


async def test_existing_sources_migrate_to_unchanged_defaults():
    src, _, _ = await seed()
    async with engine.begin() as db:
        for col in (
            "timezone_mode",
            "timezone_name",
            "offset_minutes",
            "applied_timezone_mode",
            "applied_timezone_name",
        ):
            await db.exec_driver_sql(f"ALTER TABLE epg_sources DROP COLUMN {col}")
        await db.run_sync(_add_missing_columns)
        await db.run_sync(_add_missing_columns)
    async with SessionLocal() as db:
        saved = await db.get(EpgSource, src.id)
        assert (
            saved.timezone_mode == "auto"
            and saved.timezone_name is None
            and saved.offset_minutes == 0
        )
        assert not timing_pending(saved)


async def test_portal_import_preserves_original_times_even_for_large_raw_rows(
    monkeypatch,
):
    from app.services import portal_epg

    row = {
        "name": "Portal show",
        "time": "2026-07-19 12:00:00",
        "time_to": "2026-07-19 13:00:00",
        "descr": "Long description " * 200,
    }
    remote = SimpleNamespace(
        epg_info=AsyncMock(return_value={"7": [row]}), close=AsyncMock()
    )
    monkeypatch.setattr(portal_epg.POOL, "get", AsyncMock(return_value=remote))
    raw, _ = await portal_epg._read(
        SimpleNamespace(timezone="Europe/Amsterdam"),
        "namespace",
        [SimpleNamespace(portal_channel_id="7", original_name="Portal channel")],
    )
    root = ET.fromstring(raw)
    assert root.get("spm-timezone") == "Europe/Amsterdam"
    prog = root.find("programme")
    assert prog.get("spm-start") == row["time"]
    assert prog.get("spm-stop") == row["time_to"]
    assert epg._xmltv_ts(prog.get("start")).hour == 10
    settings = SourceTiming(timezone_mode="override", timezone_name="Europe/London")
    assert (
        programme_times(prog.attrib, root.attrib, settings, portal=True)[0].utc.hour
        == 11
    )
    remote.close.assert_awaited_once()


async def test_failed_snapshot_retains_previous_interpretation_and_pending_alert(
    monkeypatch,
):
    src, _, _ = await seed()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    raw = guide(epg._fmt_ts(now), epg._fmt_ts(now + timedelta(hours=1)))
    monkeypatch.setattr(epg, "_fetch", AsyncMock(return_value=raw))
    assert (await epg.refresh_source(src.id))["ok"]
    async with client() as c:
        await c.patch(
            f"/api/epg/sources/{src.id}",
            json={"timezone_mode": "override", "timezone_name": "Europe/Amsterdam"},
        )
    original_upsert = epg._upsert_stmt

    def fail_programmes(model, *args, **kwargs):
        if model is EpgProgramme:
            raise RuntimeError("Simulated database failure")
        return original_upsert(model, *args, **kwargs)

    monkeypatch.setattr(epg, "_upsert_stmt", fail_programmes)
    assert not (await epg.refresh_source(src.id, cached=True))["ok"]
    async with SessionLocal() as db:
        source = await db.get(EpgSource, src.id)
        assert source.applied_timezone_mode == "auto" and timing_pending(source)
        assert epg_policy.aware((await db.scalar(select(EpgProgramme))).start_ts) == now
