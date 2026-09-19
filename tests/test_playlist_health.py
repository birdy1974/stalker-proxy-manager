"""Passive health follows actual playback, not preview/owning-FK shortcuts."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    Portal,
    MacAddress,
    LivePlaylist,
    LiveSource,
    LivePlaylistSource,
    VodPlaylist,
    VodSource,
    VodPlaylistSource,
    SeriePlaylist,
    SerieSource,
    SerieSeason,
    SerieEpisode,
    SeriePlaylistSource,
    SeriePlaylistSeason,
    LocalSource,
    LocalFile,
    LocalPlaylist,
)
from app.services import playlist_health as health
from app.services.stream_manager import MANAGER


async def portal(db, **kw):
    p = Portal(
        name="Provider",
        base_url="http://provider",
        resolved_url="http://provider/portal.php",
        **kw,
    )
    db.add(p)
    await db.flush()
    db.add(
        MacAddress(portal_id=p.id, mac=f"00:11:22:33:44:{p.id:02x}", status="online")
    )
    await db.flush()
    return p


async def live(db, p=None, **kw):
    p = p or await portal(db)
    source = LiveSource(
        portal_id=p.id,
        portal_channel_id=str(time.monotonic_ns()),
        original_name="Input",
        cmd=kw.pop("cmd", "http://secret/password"),
        enabled=kw.pop("selected", True),
    )
    item = LivePlaylist(custom_name=kw.pop("name", "Channel"), **kw)
    db.add_all([source, item])
    await db.flush()
    db.add(
        LivePlaylistSource(
            live_playlist_id=item.id, live_source_id=source.id, priority=1
        )
    )
    await db.commit()
    return item, source


async def item_report(db, kind="live"):
    return next(i for i in (await health.report(db))["items"] if i["kind"] == kind)


@pytest.mark.asyncio
async def test_empty_and_disabled_playlists():
    async with SessionLocal() as db:
        db.add_all(
            [
                LivePlaylist(custom_name="Missing"),
                LivePlaylist(custom_name="Disabled", enabled=False),
            ]
        )
        await db.commit()
        report = await health.report(db)
    assert report["counts"]["live"] == dict(
        checked=1, unavailable=1, warning=0, healthy=0, unverified=0
    )
    assert "No playback source links" in report["items"][0]["reasons"][0]
    assert all(report["counts"][k]["checked"] == 0 for k in ("vod", "series", "local"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected,reason",
    [
        ("untested", "unverified", "not been verified"),
        ("blank", "unavailable", "no stream command"),
        ("portal_disabled", "unavailable", "Portal is disabled"),
        ("no_mac", "unavailable", "No MAC accounts"),
        ("expired", "unavailable", "banned or expired"),
        ("banned", "unavailable", "banned or expired"),
        ("offline", "warning", "recorded connection"),
        ("unauthorized", "warning", "recorded connection"),
        ("deselected", "warning", "deselected"),
        ("busy", "warning", "busy (temporary)"),
        ("unresolved", "warning", "not resolved"),
        ("past_expiry", "warning", "expiry in the past"),
    ],
)
async def test_live_configuration(case, expected, reason):
    from sqlalchemy import select

    async with SessionLocal() as db:
        p = await portal(db)
        _, source = await live(db, p)
        mac = await db.scalar(select(MacAddress).where(MacAddress.portal_id == p.id))
        if case == "blank":
            source.cmd = " "
        elif case == "portal_disabled":
            p.enabled = False
        elif case == "no_mac":
            await db.delete(mac)
        elif case in ("expired", "banned", "offline", "unauthorized"):
            mac.status = case
        elif case == "deselected":
            source.enabled = False
        elif case == "busy":
            MANAGER.lease_mac(mac.id, seconds=60)
        elif case == "unresolved":
            p.resolved_url = None
        elif case == "past_expiry":
            mac.expire_date = "2001-01-01"
        await db.commit()
        item = await item_report(db)
        assert item["status"] == expected
        assert any(reason in r for r in item["sources"][0]["reasons"])
        assert "secret/password" not in json.dumps(item, default=str)


@pytest.mark.asyncio
async def test_vod_owning_source_does_not_replace_missing_playback_links():
    async with SessionLocal() as db:
        p = await portal(db)
        src = VodSource(
            portal_id=p.id,
            portal_item_id="1",
            original_name="Movie",
            cmd="http://source",
            enabled=True,
        )
        db.add(src)
        await db.flush()
        pl = VodPlaylist(vod_source_id=src.id, custom_name="Movie")
        db.add(pl)
        await db.commit()
        assert (await item_report(db, "vod"))["status"] == "unavailable"
        db.add(
            VodPlaylistSource(vod_playlist_id=pl.id, vod_source_id=src.id, priority=1)
        )
        await db.commit()
        assert (await item_report(db, "vod"))["status"] == "unverified"


@pytest.mark.asyncio
async def test_evidence_expiry_command_changes_and_fallback(monkeypatch):
    from app.services import runtime_settings

    monkeypatch.setattr(
        runtime_settings, "fallback_strategy", AsyncMock(return_value="macs_first")
    )
    async with SessionLocal() as db:
        item, source = await live(db)
        health.record_probe(health.signature(source), {"video": {"codec": "h264"}})
        assert (await item_report(db))["status"] == "healthy"
        stamp, state, ttl, method = health._OBSERVATIONS[health.signature(source)]
        health._OBSERVATIONS[health.signature(source)] = (
            stamp - ttl - 1,
            state,
            ttl,
            method,
        )
        assert (await item_report(db))["status"] == "unverified"
        MANAGER.route_health.succeeded(
            ("live", item.id), source, SimpleNamespace(id=1), verified_media=True
        )
        assert (await item_report(db))["status"] == "healthy"
        source.cmd = "http://changed"
        await db.commit()
        assert (await item_report(db))["status"] == "unverified"
        MANAGER.route_health.failed(source)  # one failure isn't a broken chain
        assert (await item_report(db))["status"] == "unverified"
        MANAGER.route_health.failed(source)
        assert (await item_report(db))["status"] == "warning"
        alt = LiveSource(
            portal_id=source.portal_id,
            portal_channel_id="2",
            original_name="Fallback",
            cmd="http://fallback",
            enabled=True,
        )
        db.add(alt)
        await db.flush()
        db.add(
            LivePlaylistSource(
                live_playlist_id=item.id, live_source_id=alt.id, priority=2
            )
        )
        await db.commit()
        assert (await item_report(db))[
            "status"
        ] == "warning"  # failed primary + untested fallback
        health.record_probe(health.signature(source), {"error": "timeout"})
        health.record_probe(health.signature(alt), {"error": "timeout"})
        assert (await item_report(db))["status"] == "unavailable"
        health.record_probe(health.signature(alt), {"audio": [{"codec": "aac"}]})
        assert (await item_report(db))[
            "status"
        ] == "warning"  # fallback works, primary still needs repair
        monkeypatch.setattr(
            runtime_settings,
            "fallback_strategy",
            AsyncMock(return_value="portal_first"),
        )
        assert (await item_report(db))[
            "status"
        ] == "unavailable"  # same-portal alternative not actually attempted
        MANAGER.route_health.succeeded(
            ("live", item.id), source, SimpleNamespace(id=1), verified_media=True
        )
        assert (await item_report(db))[
            "status"
        ] == "healthy"  # skipped fallback isn't a warning


@pytest.mark.asyncio
async def test_probe_api_records_selected_input_not_entire_chain(monkeypatch):
    from app.services import item_info, technical_probe

    async with SessionLocal() as db:
        item, src = await live(db)
        key = health.signature(src)
    monkeypatch.setattr(
        item_info,
        "resolve_preview_probe",
        AsyncMock(
            return_value={"url": "http://private", "is_url": True, "_health_key": key}
        ),
    )
    monkeypatch.setattr(
        technical_probe,
        "probe_technical",
        AsyncMock(return_value={"error": "Cannot open http://private/secret"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get(
                f"/api/playlist/probe?scope=playlist&kind=live&id={item.id}"
            )
        ).status_code == 200
        report = (await client.get("/api/playlist/health")).json()
    assert report["counts"]["live"]["unavailable"] == 1
    assert "private" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_missing_probe_tool_is_not_source_failure_and_cache_is_bounded():
    health.record_probe(
        ("source", 1), {"error": "Could not start ffprobe: missing binary"}
    )
    assert not health._OBSERVATIONS
    for n in range(4200):
        health.record_probe(("source", n), {"audio": [{}]})
    assert len(health._OBSERVATIONS) == 4096


async def series(db):
    p = await portal(db)
    primary = SerieSource(
        portal_id=p.id, portal_item_id="1", original_name="Series", enabled=True
    )
    fallback = SerieSource(
        portal_id=p.id, portal_item_id="2", original_name="Fallback", enabled=True
    )
    db.add_all([primary, fallback])
    await db.flush()
    pl = SeriePlaylist(serie_source_id=primary.id, custom_name="Series")
    s1 = SerieSeason(serie_source_id=primary.id, season_number=1, enabled=False)
    s2 = SerieSeason(serie_source_id=fallback.id, season_number=1)
    db.add_all([pl, s1, s2])
    await db.flush()
    db.add_all(
        [
            SeriePlaylistSource(
                serie_playlist_id=pl.id, serie_source_id=primary.id, priority=1
            ),
            SeriePlaylistSource(
                serie_playlist_id=pl.id, serie_source_id=fallback.id, priority=2
            ),
            SeriePlaylistSeason(
                serie_playlist_id=pl.id, serie_season_id=s1.id, enabled=True
            ),
        ]
    )
    first = SerieEpisode(serie_season_id=s1.id, episode_number=1, cmd="http://one")
    missing = SerieEpisode(serie_season_id=s1.id, episode_number=2, cmd="")
    db.add_all([first, missing])
    await db.commit()
    return pl, s1, s2, first, missing


@pytest.mark.asyncio
async def test_series_checks_every_exported_episode_and_matching_fallback(monkeypatch):
    from app.services import runtime_settings

    monkeypatch.setattr(
        runtime_settings, "fallback_strategy", AsyncMock(return_value="macs_first")
    )
    async with SessionLocal() as db:
        pl, s1, s2, first, missing = await series(db)
        health.record_probe(health.signature(first), {"video": {"codec": "h264"}})
        item = await item_report(db, "series")
        assert item["status"] == "warning"
        assert (
            item["episodes_checked"] == 2
        )  # SerieSeason.enabled is not an output gate
        assert item["episodes_unavailable"] == 1
        assert item["problem_episodes"] == ["S01E02"]
        fallback = SerieEpisode(
            serie_season_id=s2.id, episode_number=2, cmd="http://two"
        )
        db.add(fallback)
        await db.commit()
        item = await item_report(db, "series")
        assert item["episodes_unavailable"] == 0
        assert any(
            s["id"] == fallback.id and s["kind"] == "episode" for s in item["sources"]
        )
        # Empty primary still matters, even after a fallback is verified.
        health.record_probe(health.signature(fallback), {"audio": [{"codec": "aac"}]})
        assert (await item_report(db, "series"))["status"] == "warning"


@pytest.mark.asyncio
async def test_series_no_seasons_empty_season_and_ambiguous_owner():
    from sqlalchemy import select

    async with SessionLocal() as db:
        pl, s1, s2, first, missing = await series(db)
        for ep in [first, missing]:
            await db.delete(ep)
        await db.commit()
        item = await item_report(db, "series")
        assert (
            item["status"] == "unavailable"
            and item["episodes_checked"] == 0
            and item["episodes_unavailable"] == 0
        )
        link = await db.scalar(select(SeriePlaylistSeason))
        link.enabled = False
        await db.commit()
        assert (
            "No enabled playlist seasons"
            in (await item_report(db, "series"))["reasons"]
        )
        link.enabled = True
        db.add(
            SerieEpisode(serie_season_id=s1.id, episode_number=3, cmd="http://three")
        )
        db.add(
            SeriePlaylist(serie_source_id=pl.serie_source_id, custom_name="Duplicate")
        )
        await db.commit()
        assert (await item_report(db, "series"))["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected",
    [
        ("readable", "healthy"),
        ("missing", "unavailable"),
        ("empty", "unavailable"),
        ("directory", "unavailable"),
        ("deselected", "warning"),
        ("denied", "unavailable"),
        ("pending", "unverified"),
    ],
)
async def test_local_files(tmp_path, monkeypatch, case, expected):
    folder = tmp_path / "media"
    folder.mkdir()
    path = folder / "movie.ts"
    if case == "directory":
        path.mkdir()
    elif case != "missing":
        path.write_bytes(b"media" if case != "empty" else b"")
    if case == "denied":
        monkeypatch.setattr(health.os, "access", lambda *a: False)
    if case == "pending":
        monkeypatch.setattr(health, "inspect_files", AsyncMock(return_value={}))
    async with SessionLocal() as db:
        directory = LocalSource(directory=str(folder))
        db.add(directory)
        await db.flush()
        file = LocalFile(
            local_source_id=directory.id,
            relative_path="movie.ts",
            filename="Movie",
            enabled=case != "deselected",
        )
        db.add(file)
        await db.flush()
        db.add(LocalPlaylist(local_file_id=file.id, custom_name="Local movie"))
        await db.commit()
        assert (await item_report(db, "local"))["status"] == expected


@pytest.mark.asyncio
async def test_filesystem_timeout_reuses_single_worker(monkeypatch):
    gate = asyncio.Event()

    async def slow(*a):
        await gate.wait()
        return {"/file": None}

    fn = AsyncMock(side_effect=slow)
    monkeypatch.setattr(health.asyncio, "to_thread", fn)
    real_wait = asyncio.wait_for

    async def fast_wait(task, timeout):
        return await real_wait(task, 0.001)

    monkeypatch.setattr(health.asyncio, "wait_for", fast_wait)
    assert await health.inspect_files(["/file"]) == {}
    assert await health.inspect_files(["/file"]) == {}
    assert await health.inspect_files(["/other"]) == {}
    assert fn.await_count == 1
    gate.set()
    await health._files_task
    assert await health.inspect_files(["/file"]) == {"/file": None}


@pytest.mark.asyncio
async def test_api_filters_exact_editor_lookup_validation_and_admin_guard(monkeypatch):
    async with SessionLocal() as db:
        for i in range(30):
            db.add(LivePlaylist(custom_name=f"Missing {i}", group_name="Sports"))
        await db.commit()
        item, _ = await live(db, name="Unique")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = (
            await client.get(
                "/api/playlist/health?status=all&kind=live&page=2&per_page=10"
            )
        ).json()
        assert (
            data["total"] == 31
            and len(data["items"]) == 10
            and data["counts"]["live"]["checked"] == 31
        )
        data = (await client.get("/api/playlist/health?q=sports")).json()
        assert data["total"] == 30
        exact = (await client.get(f"/api/playlist/live?item_id={item.id}")).json()
        assert [r["id"] for r in exact["items"]] == [item.id]
        for query in ("kind=garbage", "status=unknown", "page=0", "per_page=101"):
            assert (
                await client.get("/api/playlist/health?" + query)
            ).status_code == 422
        import app.security as auth

        monkeypatch.setattr(auth, "SKIP_LOGIN", False)
        assert (await client.get("/api/playlist/health")).status_code == 401


@pytest.mark.asyncio
async def test_query_count_is_batched_and_no_network(monkeypatch):
    from app.portal.pool import POOL

    monkeypatch.setattr(
        POOL,
        "get",
        AsyncMock(side_effect=AssertionError("Health must not contact portals")),
    )
    async with SessionLocal() as db:
        p = await portal(db)
        for n in range(55):
            await live(db, p, name=f"Channel {n}")
        statements = []

        def count(*args):
            statements.append(args[2])

        event.listen(engine.sync_engine, "before_cursor_execute", count)
        try:
            report = await health.report(db)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert report["counts"]["live"]["unverified"] == 55
    assert len(statements) < 18


@pytest.mark.asyncio
async def test_redirect_link_is_not_verified_media_and_failure_warning_expires():
    async with SessionLocal() as db:
        item, src = await live(db)
        MANAGER.route_health.succeeded(("live", item.id), src, SimpleNamespace(id=1))
        assert (await item_report(db))["status"] == "unverified"
        MANAGER.route_health.failed(src)
        MANAGER.route_health.failed(src)
        assert (await item_report(db))["status"] == "warning"
        key = health.signature(src)
        stamp, state, ttl, method = health._OBSERVATIONS[key]
        health._OBSERVATIONS[key] = (stamp - ttl - 1, state, ttl, method)
        assert (await item_report(db))["status"] == "unverified"


@pytest.mark.asyncio
async def test_actual_probe_resolvers_preserve_source_identity(monkeypatch):
    from app.services import item_info

    monkeypatch.setattr(
        item_info, "playable_url", AsyncMock(return_value="http://resolved")
    )
    async with SessionLocal() as db:
        item, src = await live(db)
        resolved = await item_info.resolve_preview_probe(
            db, "playlist", "live", item.id
        )
        assert resolved["_health_key"] == health.signature(src)
        resolved = await item_info.resolve_preview_probe(db, "source", "live", src.id)
        assert resolved["_health_key"] == health.signature(src)
        pl, _, _, first, _ = await series(db)
        resolved = await item_info.resolve_preview_probe(
            db, "playlist", "series", pl.id
        )
        assert resolved["_health_key"] == health.signature(first)


@pytest.mark.asyncio
async def test_local_relative_root_and_failed_probe_can_be_retried(
    tmp_path, monkeypatch
):
    from app import config
    from app.services import item_info

    monkeypatch.setattr(config, "MEDIA_ROOT", tmp_path)
    (tmp_path / "library").mkdir()
    path = tmp_path / "library" / "movie.ts"
    path.write_bytes(b"not-valid-media")
    async with SessionLocal() as db:
        directory = LocalSource(directory="library")
        db.add(directory)
        await db.flush()
        f = LocalFile(
            local_source_id=directory.id, relative_path="movie.ts", filename="Movie"
        )
        db.add(f)
        await db.flush()
        pl = LocalPlaylist(local_file_id=f.id, custom_name="Local")
        db.add(pl)
        await db.commit()
        resolved = await item_info.resolve_preview_probe(db, "playlist", "local", pl.id)
        assert resolved["_health_key"] == health.signature(f, str(path))
        health.record_probe(resolved["_health_key"], {"error": "No media detected"})
        item = await item_report(db, "local")
        assert item["status"] == "unavailable"
        assert item["sources"][0]["status"] == "failed"
        health.record_probe(resolved["_health_key"], {"video": {"codec": "mpeg2video"}})
        assert (await item_report(db, "local"))["status"] == "healthy"
