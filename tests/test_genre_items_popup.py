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
from app.models import (LiveGenre, LiveSource, Portal, SerieGenre, SerieSource,
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
