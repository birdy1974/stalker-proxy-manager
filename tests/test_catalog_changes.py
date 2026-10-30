"""Enabled-genre catalogue comparisons and the Review Changes workflow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    CatalogChange, LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource,
    Portal, SerieEpisode, SerieGenre, SeriePlaylist, SeriePlaylistSeason,
    SerieSeason, SerieSource, Setting, VodGenre, VodPlaylist,
    VodPlaylistSource, VodSource,
)
from app.portal.client import Page
from app.services import fetch_jobs
from app.services.catalog_scheduler import refresh_due_once
from app.services.fetch_jobs import _bulk_upsert, _live_fields


async def _portal_with_live_genre(*, fetched: bool = True) -> tuple[int, int]:
    async with SessionLocal() as db:
        portal = Portal(name="catalog-test", base_url="http://portal.invalid/c/", enabled=True)
        db.add(portal)
        await db.flush()
        genre = LiveGenre(portal_id=portal.id, genre_portal_id="10", name="News",
                          enabled=True, channels_fetched=fetched, catalog_baseline=fetched)
        db.add(genre)
        await db.commit()
        return portal.id, genre.id


async def _upsert_live(pid: int, genre_id: int, items: list[dict], *, track: bool) -> None:
    async with SessionLocal() as db:
        await _bulk_upsert(
            db, LiveSource, pid, LiveSource.portal_channel_id, items,
            lambda item: str(item.get("id") or ""),
            lambda row, item: _live_fields(row, item, genre_id),
            kind="live", track_changes=track,
        )


async def test_first_fetch_is_baseline_and_subsequent_fetch_records_safe_diffs():
    pid, genre_id = await _portal_with_live_genre(fetched=False)
    await _upsert_live(pid, genre_id, [
        {"id": "101", "name": "News", "cmd": "ffmpeg http://user:old-secret@host/1"},
    ], track=False)
    async with SessionLocal() as db:
        assert (await db.execute(select(CatalogChange))).scalars().all() == []
        genre = await db.get(LiveGenre, genre_id)
        genre.channels_fetched = True
        genre.catalog_baseline = True
        await db.commit()

    await _upsert_live(pid, genre_id, [
        {"id": "101", "name": "News HD", "number": "2",
         "logo": "https://user:pw@img.example/news.png?token=logo-secret",
         "cmd": "ffmpeg http://user:new-secret@host/1"},
        {"id": "102", "name": "Sports", "cmd": "ffmpeg http://host/2"},
    ], track=True)
    async with SessionLocal() as db:
        changes = (await db.execute(select(CatalogChange).order_by(CatalogChange.source_id))).scalars().all()
        assert [(c.source_id, c.change_type, c.status) for c in changes] == [
            (1, "changed", "pending"), (2, "new", "pending")]
        details = json.loads(changes[0].changes_json)
        assert details["Name"] == {"before": "News", "after": "News HD"}
        assert "Playback link" in details
        assert "old-secret" not in changes[0].changes_json
        assert "new-secret" not in changes[0].changes_json
        assert "logo-secret" not in changes[0].changes_json
        assert "user:pw" not in changes[0].changes_json

    # A still-pending new item remains NEW even if its metadata changes again.
    await _upsert_live(pid, genre_id, [
        {"id": "102", "name": "Sports HD", "cmd": "ffmpeg http://host/2"},
    ], track=True)
    async with SessionLocal() as db:
        new_event = (await db.execute(select(CatalogChange).where(
            CatalogChange.source_id == 2))).scalar_one()
        assert new_event.change_type == "new"
        assert json.loads(new_event.changes_json)["Name"] == {
            "before": "Sports", "after": "Sports HD"}
        new_event.status = "reviewed"
        await db.commit()

    # A later update after review is a fresh pending CHANGED record.
    await _upsert_live(pid, genre_id, [
        {"id": "102", "name": "Sports 24", "cmd": "ffmpeg http://host/2"},
    ], track=True)
    async with SessionLocal() as db:
        event = (await db.execute(select(CatalogChange).where(
            CatalogChange.source_id == 2))).scalar_one()
        assert (event.change_type, event.status) == ("changed", "pending")


async def test_partial_initial_scan_does_not_establish_baseline(monkeypatch):
    async with SessionLocal() as db:
        portal = Portal(name="partial-baseline", base_url="http://portal.invalid/c/", enabled=True)
        db.add(portal)
        await db.flush()
        genre = VodGenre(portal_id=portal.id, genre_portal_id="20", name="Movies", enabled=True)
        db.add(genre)
        await db.commit()
        pid, genre_id = portal.id, genre.id

    async def no_pacing(_portal_id):
        return False

    async def page_budget():
        return fake.budget

    monkeypatch.setattr(fetch_jobs, "pace_for_playback", no_pacing)
    monkeypatch.setattr("app.services.runtime_settings.fetch_page_budget", page_budget)

    def make_items(start, end, *, changed_id=None):
        return [{"id": str(item_id),
                 "name": "Film One Updated" if item_id == changed_id else f"Film {item_id}"}
                for item_id in range(start, end + 1)]

    class FakePortal:
        mode = "partial"
        budget = 1

        async def vod_list(self, _category, page):
            if self.mode == "partial":
                return Page(items=make_items(1, 14), total=42)
            if self.mode == "full":
                start = (page - 1) * 14 + 1
                return Page(items=make_items(start, min(start + 13, 42)), total=42)
            if page == 1:
                return Page(items=make_items(1, 14, changed_id=1), total=15)
            return Page(items=make_items(43, 43), total=15)

    fake = FakePortal()

    async def fetch():
        await fetch_jobs._fetch_vod(
            fetch_jobs.Job(id="test", kind="fetch_items", portal_id=pid), fake, pid, "portal")

    # A budget-limited first pass may store useful rows, but it is not a safe
    # baseline and therefore cannot mark later unseen rows as NEW.
    await fetch()
    async with SessionLocal() as db:
        genre = await db.get(VodGenre, genre_id)
        assert genre.items_fetched is True
        assert genre.catalog_baseline is False
        assert (await db.execute(select(CatalogChange))).scalars().all() == []

    # A subsequent complete walk establishes the baseline without emitting
    # events for rows that were simply beyond the earlier page budget.
    fake.mode, fake.budget = "full", 10
    await fetch()
    async with SessionLocal() as db:
        genre = await db.get(VodGenre, genre_id)
        assert genre.catalog_baseline is True
        assert (await db.execute(select(CatalogChange))).scalars().all() == []

    # Only a later complete walk reports actual changes and additions.
    fake.mode = "changes"
    await fetch()
    async with SessionLocal() as db:
        changes = (await db.execute(select(CatalogChange).order_by(CatalogChange.id))).scalars().all()
        assert sorted(change.change_type for change in changes) == ["changed", "new"]
        assert all(change.status == "pending" for change in changes)


async def test_review_api_lists_counts_adds_live_as_fallback_and_marks_reviewed():
    from app.main import app

    async with SessionLocal() as db:
        portal = Portal(name="review", base_url="http://portal.invalid/c/")
        db.add(portal)
        await db.flush()
        genre = LiveGenre(portal_id=portal.id, genre_portal_id="10", name="News", enabled=True)
        source = LiveSource(portal_id=portal.id, live_genre_id=genre.id,
                            portal_channel_id="101", original_name="News", cmd="ffmpeg /news")
        playlist = LivePlaylist(custom_name="News", group_name="TV", enabled=True, order=1)
        db.add_all([genre, source, playlist])
        await db.flush()
        event = CatalogChange(portal_id=portal.id, kind="live", source_id=source.id,
                              change_type="new", status="pending", changes_json="{}")
        db.add(event)
        await db.flush()
        pid, change_id = portal.id, event.id
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/api/portals/{pid}/catalog-changes?kind=live")
        assert listed.status_code == 200
        result = listed.json()
        assert result["total"] == 1
        assert result["items"][0]["name"] == "News"
        assert result["items"][0]["change_type"] == "new"

        overview = await client.get("/api/portals")
        portal_row = next(row for row in overview.json()["items"] if row["id"] == pid)
        assert portal_row["catalog_pending"] == 1
        assert portal_row["catalog_changes"]["live"]["new"] == 1

        added = await client.post(f"/api/portals/{pid}/catalog-changes/review", json={
            "action": "add", "kind": "live", "ids": [change_id],
            "live_match_existing": True,
        })
        assert added.status_code == 200, added.text
        assert added.json()["fallback"] == 1
        assert added.json()["added"] == 0

        async with SessionLocal() as db:
            genre = (await db.execute(select(LiveGenre).where(
                LiveGenre.portal_id == pid))).scalar_one()
            second = LiveSource(portal_id=pid, live_genre_id=genre.id,
                                portal_channel_id="102", original_name="News 2", cmd="ffmpeg /news2")
            db.add(second)
            await db.flush()
            second_event = CatalogChange(portal_id=pid, kind="live", source_id=second.id,
                                         change_type="new", status="pending", changes_json="{}")
            db.add(second_event)
            await db.flush()
            second_event_id = second_event.id
            await db.commit()
        separate = await client.post(f"/api/portals/{pid}/catalog-changes/review", json={
            "action": "add", "kind": "live", "ids": [second_event_id],
            "live_match_existing": False,
        })
        assert separate.status_code == 200, separate.text
        assert separate.json()["added"] == 1
        assert separate.json()["fallback"] == 0

    async with SessionLocal() as db:
        sources = (await db.execute(select(LiveSource).where(
            LiveSource.portal_id == pid).order_by(LiveSource.id))).scalars().all()
        assert all(source.enabled is True for source in sources)
        playlist = (await db.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "News"))).scalar_one()
        link = (await db.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == playlist.id))).scalar_one()
        assert link.live_source_id == sources[0].id
        playlists = (await db.execute(select(LivePlaylist))).scalars().all()
        assert len(playlists) == 2
        event = await db.get(CatalogChange, change_id)
        assert event.status == "reviewed"


async def test_review_updates_existing_live_playlist_and_previews_exact_fields():
    from app.main import app

    async with SessionLocal() as db:
        portal = Portal(name="review-live-update", base_url="http://portal.invalid/c/")
        db.add(portal)
        await db.flush()
        old_genre = LiveGenre(portal_id=portal.id, genre_portal_id="9", name="Old News", enabled=True)
        genre = LiveGenre(portal_id=portal.id, genre_portal_id="10", name="News", enabled=True)
        db.add_all([old_genre, genre])
        await db.flush()
        source = LiveSource(
            portal_id=portal.id, live_genre_id=genre.id, portal_channel_id="101",
            original_name="News HD", cmd="ffmpeg /news-hd", enabled=True,
            logo_original="https://img.example/news-hd.png", epg_original="news.hd")
        playlist = LivePlaylist(
            custom_name="News Old", group_name="Old News", epg_id="news.old",
            logo="https://img.example/news-old.png", number=7, enabled=True, order=3)
        db.add_all([source, playlist])
        await db.flush()
        db.add(LivePlaylistSource(live_playlist_id=playlist.id, live_source_id=source.id, priority=1))
        event = CatalogChange(
            portal_id=portal.id, kind="live", source_id=source.id, change_type="changed",
            status="pending", changes_json=json.dumps({
                "Name": {"before": "News Old", "after": "News HD"},
                "Logo": {"before": "https://img.example/news-old.png",
                         "after": "https://img.example/news-hd.png"},
                "EPG ID": {"before": "news.old", "after": "news.hd"},
                "Playback link": {"before": "Portal link", "after": "Updated portal link"},
            }))
        db.add(event)
        await db.flush()
        pid, change_id, playlist_id = portal.id, event.id, playlist.id
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/api/portals/{pid}/catalog-changes?kind=live")
        assert listed.status_code == 200, listed.text
        item = listed.json()["items"][0]
        plan = item["playlist_update"]
        assert plan["state"] == "primary"
        assert plan["update_count"] == 4
        assert plan["source_only"] == ["Playback link"]
        updates = {u["field"]: u for u in plan["targets"][0]["updates"]}
        assert updates["custom_name"] == {
            "field": "custom_name", "label": "Playlist name",
            "before": "News Old", "after": "News HD"}
        assert updates["group_name"]["before"] == "Old News"
        assert updates["group_name"]["after"] == "News"
        assert updates["logo"]["after"] == "https://img.example/news-hd.png"
        assert updates["epg_id"]["after"] == "news.hd"

        reviewed = await client.post(f"/api/portals/{pid}/catalog-changes/review", json={
            "action": "reviewed", "kind": "live", "ids": [change_id]})
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["playlist_items_updated"] == 1
        assert reviewed.json()["playlist_fields_updated"] == 4
        assert reviewed.json()["season_links_added"] == 0

    async with SessionLocal() as db:
        playlist = await db.get(LivePlaylist, playlist_id)
        assert playlist.custom_name == "News HD"
        assert playlist.group_name == "News"
        assert playlist.logo == "https://img.example/news-hd.png"
        assert playlist.epg_id == "news.hd"
        assert playlist.number == 7 and playlist.order == 3 and playlist.enabled is True
        event = await db.get(CatalogChange, change_id)
        assert event.status == "reviewed"


async def test_review_does_not_rename_playlist_from_a_fallback_only_source():
    from app.main import app

    async with SessionLocal() as db:
        portal = Portal(name="review-fallback", base_url="http://portal.invalid/c/")
        db.add(portal)
        await db.flush()
        genre = LiveGenre(portal_id=portal.id, genre_portal_id="10", name="News", enabled=True)
        db.add(genre)
        await db.flush()
        primary = LiveSource(portal_id=portal.id, live_genre_id=genre.id, portal_channel_id="101",
                             original_name="Main News", cmd="ffmpeg /main", enabled=True)
        fallback = LiveSource(portal_id=portal.id, live_genre_id=genre.id, portal_channel_id="102",
                              original_name="Fallback HD", cmd="ffmpeg /fallback", enabled=True,
                              logo_original="https://img.example/fallback.png")
        playlist = LivePlaylist(custom_name="Main News", group_name="News",
                                logo="https://img.example/main.png", enabled=True, order=1)
        db.add_all([primary, fallback, playlist])
        await db.flush()
        db.add_all([
            LivePlaylistSource(live_playlist_id=playlist.id, live_source_id=primary.id, priority=1),
            LivePlaylistSource(live_playlist_id=playlist.id, live_source_id=fallback.id, priority=2),
        ])
        event = CatalogChange(
            portal_id=portal.id, kind="live", source_id=fallback.id, change_type="changed",
            status="pending", changes_json=json.dumps({
                "Name": {"before": "Fallback", "after": "Fallback HD"},
                "Logo": {"before": "https://img.example/old.png",
                         "after": "https://img.example/fallback.png"},
            }))
        db.add(event)
        await db.flush()
        pid, change_id, playlist_id = portal.id, event.id, playlist.id
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/api/portals/{pid}/catalog-changes?kind=live")
        plan = listed.json()["items"][0]["playlist_update"]
        assert plan["state"] == "fallback"
        assert plan["targets"] == []
        assert plan["fallback_targets"][0]["priority"] == 2

        reviewed = await client.post(f"/api/portals/{pid}/catalog-changes/review", json={
            "action": "reviewed", "kind": "live", "ids": [change_id]})
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["playlist_items_updated"] == 0
        assert reviewed.json()["playlist_fields_updated"] == 0

    async with SessionLocal() as db:
        playlist = await db.get(LivePlaylist, playlist_id)
        assert playlist.custom_name == "Main News"
        assert playlist.logo == "https://img.example/main.png"


async def test_review_updates_existing_vod_playlist_fields():
    from app.main import app

    async with SessionLocal() as db:
        portal = Portal(name="review-vod-update", base_url="http://portal.invalid/c/")
        db.add(portal)
        await db.flush()
        genre = VodGenre(portal_id=portal.id, genre_portal_id="20", name="Drama", enabled=True)
        db.add(genre)
        await db.flush()
        source = VodSource(
            portal_id=portal.id, vod_genre_id=genre.id, portal_item_id="501",
            original_name="Film New", cmd="/media/501.mpg", enabled=True,
            poster="https://img.example/film-new.jpg", year="2026", rating="8.1",
            description="New plot", genre="Drama", director="New Director")
        db.add(source)
        await db.flush()
        playlist = VodPlaylist(
            vod_source_id=source.id, custom_name="Film Old", group_name="Old Movies",
            poster="https://img.example/film-old.jpg", year="2020", rating="7.0",
            overview="Old plot", logo="https://img.example/tmdb-logo.png", tmdb_id=42,
            enabled=True, order=5)
        db.add(playlist)
        await db.flush()
        db.add(VodPlaylistSource(vod_playlist_id=playlist.id, vod_source_id=source.id, priority=1))
        event = CatalogChange(
            portal_id=portal.id, kind="vod", source_id=source.id, change_type="changed",
            status="pending", changes_json=json.dumps({
                "Title": {"before": "Film Old", "after": "Film New"},
                "Year": {"before": "2020", "after": "2026"},
                "Genre": {"before": "Old Movies", "after": "Drama"},
                "Poster": {"before": "https://img.example/film-old.jpg",
                           "after": "https://img.example/film-new.jpg"},
                "Rating": {"before": "7.0", "after": "8.1"},
                "Description": {"before": "Old plot", "after": "New plot"},
                "Director": {"before": "Old Director", "after": "New Director"},
            }))
        db.add(event)
        await db.flush()
        pid, change_id, playlist_id = portal.id, event.id, playlist.id
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/api/portals/{pid}/catalog-changes?kind=vod")
        plan = listed.json()["items"][0]["playlist_update"]
        assert plan["state"] == "primary"
        assert plan["source_only"] == ["Director"]
        assert {u["field"] for u in plan["targets"][0]["updates"]} == {
            "custom_name", "group_name", "poster", "year", "rating", "overview"}

        reviewed = await client.post(f"/api/portals/{pid}/catalog-changes/review", json={
            "action": "reviewed", "kind": "vod", "ids": [change_id]})
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["playlist_fields_updated"] == 6

    async with SessionLocal() as db:
        playlist = await db.get(VodPlaylist, playlist_id)
        assert playlist.custom_name == "Film New"
        assert playlist.group_name == "Drama"
        assert playlist.poster == "https://img.example/film-new.jpg"
        assert playlist.year == "2026" and playlist.rating == "8.1"
        assert playlist.overview == "New plot"
        assert playlist.logo == "https://img.example/tmdb-logo.png"
        assert playlist.tmdb_id == 42 and playlist.order == 5


async def test_review_adds_missing_series_season_links_to_existing_playlist():
    from app.main import app

    async with SessionLocal() as db:
        portal = Portal(name="review-series-update", base_url="http://portal.invalid/c/")
        db.add(portal)
        await db.flush()
        genre = SerieGenre(portal_id=portal.id, genre_portal_id="30", name="Drama", enabled=True)
        db.add(genre)
        await db.flush()
        source = SerieSource(portal_id=portal.id, serie_genre_id=genre.id, portal_item_id="701",
                             original_name="Show", enabled=True, seasons_fetched=True)
        db.add(source)
        await db.flush()
        season1 = SerieSeason(serie_source_id=source.id, season_number=1, name="Season 1", enabled=True)
        season2 = SerieSeason(serie_source_id=source.id, season_number=2, name="Season 2", enabled=True)
        playlist = SeriePlaylist(serie_source_id=source.id, custom_name="Show", group_name="Drama",
                                 enabled=True, order=2)
        db.add_all([season1, season2, playlist])
        await db.flush()
        db.add_all([
            SerieEpisode(serie_season_id=season2.id, episode_number=1, name="Pilot"),
            SerieEpisode(serie_season_id=season2.id, episode_number=2, name="Second"),
            SeriePlaylistSeason(serie_playlist_id=playlist.id, serie_season_id=season1.id, enabled=True),
        ])
        event = CatalogChange(
            portal_id=portal.id, kind="series", source_id=source.id, change_type="changed",
            status="pending", changes_json=json.dumps({
                "Season metadata": {"before": "Season 1", "after": "Season 1, Season 2"}}))
        db.add(event)
        await db.flush()
        pid, change_id, playlist_id, season2_id = portal.id, event.id, playlist.id, season2.id
        await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(f"/api/portals/{pid}/catalog-changes?kind=series")
        target = listed.json()["items"][0]["playlist_update"]["targets"][0]
        assert target["season_links"] == [{
            "season_id": season2_id, "season_number": 2, "name": "Season 2",
            "enabled": True, "episode_count": 2}]

        reviewed = await client.post(f"/api/portals/{pid}/catalog-changes/review", json={
            "action": "reviewed", "kind": "series", "ids": [change_id]})
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["season_links_added"] == 1

    async with SessionLocal() as db:
        link = (await db.execute(select(SeriePlaylistSeason).where(
            SeriePlaylistSeason.serie_playlist_id == playlist_id,
            SeriePlaylistSeason.serie_season_id == season2_id))).scalar_one()
        assert link.enabled is True


def test_review_popup_shows_playlist_update_preview_and_action():
    template = (Path(__file__).resolve().parents[1]
                / "app/templates/portals.html").read_text(encoding="utf-8")
    assert "Update Playlist & mark reviewed" in template
    assert "Playlist update if reviewed" in template
    assert "playlist_update" in template
    assert "playlist_items_updated" in template


async def test_automatic_catalog_checks_are_off_by_default_and_queue_one_due_portal(monkeypatch):
    from app.services import catalog_scheduler

    async with SessionLocal() as db:
        portal = Portal(name="scheduled", base_url="http://portal.invalid/c/", enabled=True)
        db.add(portal)
        await db.flush()
        db.add(LiveGenre(portal_id=portal.id, genre_portal_id="10", name="News", enabled=True))
        db.add(Setting(key="catalog_refresh_hours", value=json.dumps(24)))
        pid = portal.id
        await db.commit()

    monkeypatch.setattr(fetch_jobs, "JOBS", {})
    submitted = []

    async def fake_submit(kind, portal_id, series_ids=None):
        submitted.append((kind, portal_id))
        return SimpleNamespace(kind=kind, id="scheduled", public=lambda: {"id": "scheduled"})

    monkeypatch.setattr(fetch_jobs, "submit", fake_submit)
    result = await catalog_scheduler.refresh_due_once()
    assert result["queued"] is True
    assert submitted == [("catalog_check", pid)]
    again = await refresh_due_once()
    assert again["queued"] is False
    assert again["reason"] == "not due"


async def test_catalog_interval_setting_is_validated_and_can_be_paused():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bad = await client.post("/api/settings", json={"catalog_refresh_hours": 169})
        assert bad.status_code == 400
        good = await client.post("/api/settings", json={"catalog_refresh_hours": 24})
        assert good.status_code == 200
        saved = await client.get("/api/settings")
        assert saved.json()["settings"]["catalog_refresh_hours"] == 24
        paused = await client.post("/api/settings", json={"catalog_refresh_hours": 0})
        assert paused.status_code == 200
