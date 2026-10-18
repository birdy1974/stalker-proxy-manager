"""Enabled-genre catalogue comparisons and the Review Changes workflow."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    CatalogChange, LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource,
    Portal, Setting, VodGenre,
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
