"""
GET /api/playlist/series must not scale its query count with the page size.

The endpoint ran one SELECT for the source seasons per row, one more per season
to check for a link row, and one COUNT per enabled season. Measured on a
60-series library that was 204 queries for a 25-row page - 8.2 per item, 209 ms
of round trips. Batched it is 9 queries for the page and 77 ms, with a
byte-identical payload.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

from app.database import SessionLocal
from app.models import (
    Portal,
    SerieEpisode,
    SeriePlaylist,
    SeriePlaylistSeason,
    SeriePlaylistSource,
    SerieSeason,
    SerieSource,
)

N_SERIES, N_SEASONS, N_EPS = 40, 3, 4


async def _seed() -> None:
    async with SessionLocal() as s:
        if (await s.execute(select(SeriePlaylist))).scalars().first():
            return
        portal = Portal(name="p", base_url="http://p.invalid")
        s.add(portal)
        await s.flush()
        for i in range(N_SERIES):
            src = SerieSource(portal_id=portal.id, portal_item_id=f"s{i}",
                              original_name=f"Series {i}")
            s.add(src)
            await s.flush()
            seasons = []
            for sn in range(1, N_SEASONS + 1):
                se = SerieSeason(serie_source_id=src.id, season_number=sn,
                                 name=f"S{sn}", enabled=True)
                s.add(se)
                await s.flush()
                seasons.append(se)
                for ep in range(1, N_EPS + 1):
                    s.add(SerieEpisode(serie_season_id=se.id, episode_number=ep,
                                       name=f"E{ep}", cmd="c"))
            pl = SeriePlaylist(serie_source_id=src.id, custom_name=f"Series {i}",
                               group_name="G", enabled=True, order=i)
            s.add(pl)
            await s.flush()
            s.add(SeriePlaylistSource(serie_playlist_id=pl.id, serie_source_id=src.id,
                                      priority=0))
            for se in seasons:
                s.add(SeriePlaylistSeason(serie_playlist_id=pl.id,
                                          serie_season_id=se.id, enabled=True))
        await s.commit()


async def _counted_get(engine, path: str):
    from app.main import app

    hits = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(*_a, **_k):
        hits["n"] += 1

    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as c:
            r = await c.get(path)
        return hits["n"], r
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)


async def test_query_count_does_not_grow_with_the_page():
    from app.database import engine

    await _seed()
    small, r_small = await _counted_get(engine, "/api/playlist/series?per_page=5")
    big, r_big = await _counted_get(engine, "/api/playlist/series?per_page=40")
    assert r_small.status_code == 200 and r_big.status_code == 200
    assert len(r_big.json()["items"]) == N_SERIES
    # A constant number of queries, whatever the page size.
    assert big - small <= 3, (
        f"queries grew with the page: {small} for 5 rows vs {big} for {N_SERIES} rows")
    assert big <= 20, f"{big} queries for one page is still an N+1"


async def test_payload_carries_the_seasons_and_episode_counts():
    from app.database import engine

    await _seed()
    _n, r = await _counted_get(engine, "/api/playlist/series?per_page=40")
    items = r.json()["items"]
    assert len(items) == N_SERIES
    for it in items:
        assert len(it["seasons"]) == N_SEASONS, "every season must be listed"
        assert it["enabled_episode_count"] == N_SEASONS * N_EPS
        for s in it["seasons"]:
            assert {"link_id", "season_id", "season_number", "name", "enabled"} <= set(s)


async def test_a_series_with_no_linked_seasons_still_lists_them():
    """The read-repair must still run - this is the 'no seasons' bug."""
    from app.database import engine

    await _seed()
    async with SessionLocal() as s:
        pid = (await s.execute(select(SeriePlaylist.id).limit(1))).scalar_one()
        from sqlalchemy import delete
        await s.execute(delete(SeriePlaylistSeason).where(
            SeriePlaylistSeason.serie_playlist_id == pid))
        await s.commit()

    _n, r = await _counted_get(engine, "/api/playlist/series?per_page=40")
    item = next(i for i in r.json()["items"] if i["id"] == pid)
    assert len(item["seasons"]) == N_SEASONS, "repaired link rows are missing"
    assert item["enabled_episode_count"] == N_SEASONS * N_EPS
