"""
Genre → channels popup (Edit/Add portal popup, genre NAME click):

* `GET /api/portals/{pid}/items?genre_id=…` narrows to ONE genre through the
  right FK per kind (live/vod/series), still respects `q` + the portal scope,
  and carries the popup's `number` / `channel_id` columns;
* the popup's genre switch and the pane's switches share ONE server contract —
  `POST /{pid}/genres/toggle` — pinned here so neither window can invent its
  own truth.
"""
from __future__ import annotations

import httpx
from httpx import ASGITransport

from app.database import SessionLocal
from app.main import app
from app.models import (LiveGenre, LiveSource, MacAddress, Portal, SerieGenre, SerieSource,
                        VodGenre, VodSource)

BASE = "http://test"


async def _seed():
    """Two portals: p1 has a News genre (2 channels), a Sport genre, a loose
    channel without a genre, a Movies vod genre and a loose film; p2 exists
    so cross-portal leakage is provable. Returns (p1, p2, news_gid, sport_gid,
    movies_gid)."""
    async with SessionLocal() as s:
        p1 = Portal(name="p1", base_url="http://p1.invalid/c/", enabled=True)
        p2 = Portal(name="p2", base_url="http://p2.invalid/c/", enabled=True)
        s.add_all([p1, p2])
        await s.flush()
        news = LiveGenre(portal_id=p1.id, genre_portal_id="g1", name="News",
                         enabled=True)
        sport = LiveGenre(portal_id=p1.id, genre_portal_id="g2", name="Sport",
                          enabled=True)
        elsewhere = LiveGenre(portal_id=p2.id, genre_portal_id="g1",
                              name="Elsewhere", enabled=True)
        s.add_all([news, sport, elsewhere])
        await s.flush()

        def chan(genre_id, name, cid, number, portal_id):
            return LiveSource(portal_id=portal_id, live_genre_id=genre_id,
                              portal_channel_id=cid, original_name=name,
                              number=number, cmd="ffmpeg http://x.ts",
                              enabled=True)
        s.add_all([chan(news.id, "News 1", "101", "1", p1.id),
                   chan(news.id, "News 2", "102", "2", p1.id),
                   chan(sport.id, "Sport 1", "201", "3", p1.id),
                   chan(None, "Loose", "9", "9", p1.id),
                   chan(elsewhere.id, "Elsewhere 1", "301", "1", p2.id)])
        movies = VodGenre(portal_id=p1.id, genre_portal_id="v1", name="Movies",
                          enabled=True)
        s.add(movies)
        await s.flush()
        s.add(VodSource(portal_id=p1.id, vod_genre_id=movies.id,
                        portal_item_id="mv1", original_name="Film One",
                        enabled=True))
        s.add(VodSource(portal_id=p1.id, vod_genre_id=None,
                        portal_item_id="mv0", original_name="Loose Film",
                        enabled=True))
        await s.commit()
        return p1.id, p2.id, news.id, sport.id, movies.id


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


async def test_items_narrow_to_one_genre_with_the_popups_columns():
    p1, p2, news, sport, _movies = await _seed()
    async with _client() as c:
        # the popup's own query: one genre of one portal
        r = await c.get(f"/api/portals/{p1}/items",
                        params={"kind": "live", "genre_id": news})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 2
        assert {i["name"] for i in d["items"]} == {"News 1", "News 2"}
        row = next(i for i in d["items"] if i["name"] == "News 1")
        assert row["number"] == "1" and row["channel_id"] == "101"
        assert row["enabled"] is True

        # without genre_id the endpoint keeps its old "everything" behaviour
        r = await c.get(f"/api/portals/{p1}/items", params={"kind": "live"})
        assert r.json()["total"] == 4, "genre-less channels still belong to the portal view"

        # q narrows INSIDE the genre (the popup's filter box)
        r = await c.get(f"/api/portals/{p1}/items",
                        params={"kind": "live", "genre_id": news, "q": "news 2"})
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["name"] == "News 2"

        # another portal's genre id on THIS portal → nothing (portal scope wins)
        r = await c.get(f"/api/portals/{p2}/items",
                        params={"kind": "live", "genre_id": news})
        assert r.json()["total"] == 0

        # sport genre works too
        r = await c.get(f"/api/portals/{p1}/items",
                        params={"kind": "live", "genre_id": sport})
        assert {i["name"] for i in r.json()["items"]} == {"Sport 1"}

        r = await c.get(f"/api/portals/{p1}/items", params={"kind": "nope"})
        assert r.status_code == 400


async def test_vod_items_use_the_vod_genre_column_and_portal_item_id():
    p1, _p2, _news, _sport, movies = await _seed()
    async with _client() as c:
        r = await c.get(f"/api/portals/{p1}/items",
                        params={"kind": "vod", "genre_id": movies})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 1
        row = d["items"][0]
        assert row["name"] == "Film One"
        assert row["channel_id"] == "mv1", "vod/series expose portal_item_id as the id"
        assert row["number"] is None, "vod rows have no channel number to show"
        # genre-less movie still listed without the filter
        r = await c.get(f"/api/portals/{p1}/items", params={"kind": "vod"})
        assert r.json()["total"] == 2


async def test_the_popups_switch_and_the_pane_switch_share_one_toggle_endpoint():
    """Both windows in the UI POST {kind, ids, enabled} to genres/toggle — the
    server side of that contract is one endpoint, one row update."""
    p1, _p2, news, sport, _movies = await _seed()
    async with _client() as c:
        r = await c.post(f"/api/portals/{p1}/genres/toggle",
                         json={"kind": "live", "ids": [news], "enabled": False})
        assert r.status_code == 200 and r.json() == {"ok": True, "count": 1}, r.text
        d = (await c.get(f"/api/portals/{p1}/genres")).json()
        by_name = {g["name"]: g for g in d["live"]}
        assert by_name["News"]["enabled"] is False, "the popup switch disabled it"
        assert by_name["Sport"]["enabled"] is True, "other genres untouched"
        # flip back — the pane switch reads this state on its next loadGenres
        r = await c.post(f"/api/portals/{p1}/genres/toggle",
                         json={"kind": "live", "ids": [news], "enabled": True})
        assert r.json()["count"] == 1
        d = (await c.get(f"/api/portals/{p1}/genres")).json()
        assert {g["name"]: g["enabled"] for g in d["live"]}["News"] is True


async def test_disabled_genre_fetches_channels_first_without_storing_until_enabled(monkeypatch):
    """When a genre is disabled and its channels are not yet fetched:
    1. Clicking the genre name fetches the channels from the portal first.
    2. Channels are NOT stored in the database while the genre is disabled.
    3. Only store the channels in the database if the genre gets enabled.
    """
    from sqlalchemy import func, select
    from tests.mockclient import GOOD, PORTAL, Wired

    Wired(monkeypatch)

    async with SessionLocal() as s:
        p = Portal(name="mockportal", base_url="http://test/mock/c/", resolved_url=PORTAL, enabled=True)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac=GOOD, order=0, status="online", online=True))
        # Genre is disabled and channels not fetched yet
        news = LiveGenre(portal_id=p.id, genre_portal_id="1", name="News",
                         enabled=False, channels_fetched=False)
        s.add(news)
        await s.commit()
        pid, gid = p.id, news.id

    # Verify initial database state: 0 channels stored for this portal/genre
    async with SessionLocal() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(LiveSource).where(
                LiveSource.portal_id == pid, LiveSource.live_genre_id == gid)
        )).scalar()
        assert cnt == 0
        g = await s.get(LiveGenre, gid)
        assert g.enabled is False
        assert g.channels_fetched is False

    async with _client() as c:
        # Step 1: User clicks on the genre name -> GET /items?genre_id=...
        r = await c.get(f"/api/portals/{pid}/items",
                        params={"kind": "live", "genre_id": gid})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["total"] == 4
        assert len(d["items"]) == 4
        names = {i["name"] for i in d["items"]}
        assert names == {"NPO 1", "NPO 2", "RTL Nieuws", "BBC News"}
        first = next(i for i in d["items"] if i["name"] == "NPO 1")
        assert first["channel_id"] == "1001"
        assert first["number"] == "1"

        # Step 2: Channels were fetched first, BUT NOT stored in the database!
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(LiveSource).where(
                    LiveSource.portal_id == pid, LiveSource.live_genre_id == gid)
            )).scalar()
            assert cnt == 0, "channels must NOT be stored in DB while genre is disabled"
            g = await s.get(LiveGenre, gid)
            assert g.channels_fetched is False, "channels_fetched must stay False while disabled"
            assert g.enabled is False

        # Step 3: Filtering q also works from the preview cache without storing to DB
        r_q = await c.get(f"/api/portals/{pid}/items",
                          params={"kind": "live", "genre_id": gid, "q": "npo"})
        assert r_q.status_code == 200
        d_q = r_q.json()
        assert d_q["total"] == 2
        assert {i["name"] for i in d_q["items"]} == {"NPO 1", "NPO 2"}

        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(LiveSource).where(
                    LiveSource.portal_id == pid, LiveSource.live_genre_id == gid)
            )).scalar()
            assert cnt == 0, "filter operations must still not persist to DB"

        # Step 4: User enables the genre -> POST /genres/toggle with enabled=True
        r_tog = await c.post(f"/api/portals/{pid}/genres/toggle",
                             json={"kind": "live", "ids": [gid], "enabled": True})
        assert r_tog.status_code == 200 and r_tog.json() == {"ok": True, "count": 1}

        # Step 5: Now the channels MUST be stored in the database!
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(LiveSource).where(
                    LiveSource.portal_id == pid, LiveSource.live_genre_id == gid)
            )).scalar()
            assert cnt == 4, "channels must now be stored in the database"
            g = await s.get(LiveGenre, gid)
            assert g.enabled is True
            assert g.channels_fetched is True
            assert g.item_count == 4

        # Step 6: Subsequent calls to /items read from the database
        r_stored = await c.get(f"/api/portals/{pid}/items",
                               params={"kind": "live", "genre_id": gid})
        assert r_stored.status_code == 200
        d_stored = r_stored.json()
        assert d_stored["total"] == 4
        assert {i["name"] for i in d_stored["items"]} == {"NPO 1", "NPO 2", "RTL Nieuws", "BBC News"}


async def test_disabled_genre_channels_never_stored_if_closed_without_enabling(monkeypatch):
    """If the user opens the genre popup, channels are fetched for preview,
    but user never enables the genre, nothing is ever written to the database."""
    from sqlalchemy import func, select
    from tests.mockclient import GOOD, PORTAL, Wired

    Wired(monkeypatch)

    async with SessionLocal() as s:
        p = Portal(name="mockportal2", base_url="http://test/mock/c/", resolved_url=PORTAL, enabled=True)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac=GOOD, order=0, status="online", online=True))
        sport = LiveGenre(portal_id=p.id, genre_portal_id="2", name="Sport",
                          enabled=False, channels_fetched=False)
        s.add(sport)
        await s.commit()
        pid, gid = p.id, sport.id

    async with _client() as c:
        r = await c.get(f"/api/portals/{pid}/items",
                        params={"kind": "live", "genre_id": gid})
        assert r.status_code == 200
        assert r.json()["total"] == 4

        # User closes the popup (no toggle)
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(LiveSource).where(
                    LiveSource.portal_id == pid, LiveSource.live_genre_id == gid)
            )).scalar()
            assert cnt == 0
            g = await s.get(LiveGenre, gid)
            assert g.channels_fetched is False
            assert g.enabled is False


async def test_disabled_vod_genre_fetches_first_without_storing_until_enabled(monkeypatch):
    """VOD genre preview: items fetched on demand, not stored until enabled."""
    from sqlalchemy import func, select
    from tests.mockclient import GOOD, PORTAL, Wired

    Wired(monkeypatch)

    async with SessionLocal() as s:
        p = Portal(name="mockportal_vod", base_url="http://test/mock/c/", resolved_url=PORTAL, enabled=True)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac=GOOD, order=0, status="online", online=True))
        action = VodGenre(portal_id=p.id, genre_portal_id="11", name="Action",
                          enabled=False, items_fetched=False)
        s.add(action)
        await s.commit()
        pid, gid = p.id, action.id

    async with _client() as c:
        r = await c.get(f"/api/portals/{pid}/items",
                        params={"kind": "vod", "genre_id": gid})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] > 0
        assert len(d["items"]) > 0

        # NOT stored in DB while disabled
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(VodSource).where(
                    VodSource.portal_id == pid, VodSource.vod_genre_id == gid)
            )).scalar()
            assert cnt == 0
            g = await s.get(VodGenre, gid)
            assert g.items_fetched is False

        # Enable genre
        r_tog = await c.post(f"/api/portals/{pid}/genres/toggle",
                             json={"kind": "vod", "ids": [gid], "enabled": True})
        assert r_tog.status_code == 200

        # Now stored in DB
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(VodSource).where(
                    VodSource.portal_id == pid, VodSource.vod_genre_id == gid)
            )).scalar()
            assert cnt > 0
            g = await s.get(VodGenre, gid)
            assert g.enabled is True
            assert g.items_fetched is True


async def test_disabled_series_genre_fetches_first_without_storing_until_enabled(monkeypatch):
    """Series genre preview: items fetched on demand, not stored until enabled."""
    from sqlalchemy import func, select
    from tests.mockclient import GOOD, PORTAL, Wired

    Wired(monkeypatch)

    async with SessionLocal() as s:
        p = Portal(name="mockportal_ser", base_url="http://test/mock/c/", resolved_url=PORTAL, enabled=True)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac=GOOD, order=0, status="online", online=True))
        drama = SerieGenre(portal_id=p.id, genre_portal_id="22", name="Drama",
                           enabled=False, items_fetched=False)
        s.add(drama)
        await s.commit()
        pid, gid = p.id, drama.id

    async with _client() as c:
        r = await c.get(f"/api/portals/{pid}/items",
                        params={"kind": "series", "genre_id": gid})
        assert r.status_code == 200
        d = r.json()
        assert d["total"] > 0
        assert len(d["items"]) > 0

        # NOT stored in DB while disabled
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(SerieSource).where(
                    SerieSource.portal_id == pid, SerieSource.serie_genre_id == gid)
            )).scalar()
            assert cnt == 0
            g = await s.get(SerieGenre, gid)
            assert g.items_fetched is False

        # Enable genre
        r_tog = await c.post(f"/api/portals/{pid}/genres/toggle",
                             json={"kind": "series", "ids": [gid], "enabled": True})
        assert r_tog.status_code == 200

        # Now stored in DB
        async with SessionLocal() as s:
            cnt = (await s.execute(
                select(func.count()).select_from(SerieSource).where(
                    SerieSource.portal_id == pid, SerieSource.serie_genre_id == gid)
            )).scalar()
            assert cnt > 0
            g = await s.get(SerieGenre, gid)
            assert g.enabled is True
            assert g.items_fetched is True



