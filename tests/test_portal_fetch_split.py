"""
Portal fetch split (Edit-portal workflow).

The fetch pipeline is split into two stages so the portal popup can offer
"Fetch genres" (genre lists only) followed by "Save -> fetch items of enabled
genres":

  * `fetch_genres`  -> _sync_all_genres(): genre lists only, no items.
  * `fetch_items`   -> _fetch_live/_vod/_series: items of ENABLED genres only.

Two rules are pinned here:
  1. Every genre is created DISABLED (including the synthetic "(All VOD)" /
     "(All series)" rows a portal without categories gets) - the user opts in.
  2. Re-syncing genres must not reset an existing genre's enabled flag.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database import SessionLocal
from app.models import LiveGenre, LiveSource, MacAddress, Portal, SerieGenre, VodGenre
from app.portal.client import Page
from app.services.fetch_jobs import (
    Job, _run_items_fetch, _sync_all_genres,
)


class _FakeClient:
    """Minimal StalkerClient stand-in: records what the fetchers asked for."""

    def __init__(self) -> None:
        self.all_calls = 0
        self.vod_categories: list[str | None] = []
        self.series_categories: list[str | None] = []

    async def live_genres(self):
        return [{"id": "g1", "title": "News"}, {"id": "g2", "title": "Sports"}]

    async def vod_genres(self):
        return []                       # portal without VOD categories

    async def series_genres(self):
        return [{"id": "s1", "title": "Drama"}]   # portal WITH series categories

    async def all_channels(self):
        self.all_calls += 1
        return [
            {"id": "c1", "name": "News 1", "tv_genre_id": "g1", "cmd": "ffmpeg http://x/1"},
            {"id": "c2", "name": "Sports 1", "tv_genre_id": "g2", "cmd": "ffmpeg http://x/2"},
        ]

    async def vod_list(self, category_id, page):
        self.vod_categories.append(category_id)
        return Page(items=[], total=0)

    async def series_list(self, category_id, page):
        self.series_categories.append(category_id)
        return Page(items=[], total=0)


async def _portal() -> int:
    async with SessionLocal() as s:
        p = Portal(name="t", base_url="http://portal.invalid/c/", enabled=True)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac="00:11:22:33:44:55"))
        await s.commit()
        return p.id


async def _genres(kind_model, pid):
    async with SessionLocal() as s:
        return (await s.execute(select(kind_model).where(
            kind_model.portal_id == pid).order_by(kind_model.name))).scalars().all()


async def test_sync_all_genres_creates_every_genre_disabled():
    pid = await _portal()
    job = Job(id="t", kind="fetch_genres", portal_id=pid)
    await _sync_all_genres(job, _FakeClient(), pid, "t")

    live = await _genres(LiveGenre, pid)
    assert {g.name for g in live} == {"News", "Sports"}
    assert all(g.enabled is False for g in live)

    vod = await _genres(VodGenre, pid)
    assert [g.name for g in vod] == ["(All VOD)"]          # synthetic, disabled
    assert vod[0].enabled is False

    ser = await _genres(SerieGenre, pid)
    assert [g.name for g in ser] == ["Drama"]
    assert ser[0].enabled is False


async def test_resync_preserves_a_genre_the_user_enabled():
    pid = await _portal()
    job = Job(id="t", kind="fetch_genres", portal_id=pid)
    await _sync_all_genres(job, _FakeClient(), pid, "t")

    async with SessionLocal() as s:
        v = (await s.execute(select(VodGenre).where(
            VodGenre.portal_id == pid))).scalar_one()
        v.enabled = True               # user opts in
        await s.commit()

    await _sync_all_genres(job, _FakeClient(), pid, "t")   # genre refresh
    vod = await _genres(VodGenre, pid)
    assert vod[0].enabled is True                          # not reset


async def test_items_fetch_only_pulls_enabled_genres():
    pid = await _portal()
    client = _FakeClient()
    await _sync_all_genres(Job(id="a", kind="fetch_genres", portal_id=pid),
                           client, pid, "t")

    # enable only live:News and the synthetic VOD genre
    async with SessionLocal() as s:
        for m, cond in ((LiveGenre, lambda g: g.name == "News"),
                        (VodGenre, lambda g: g.name == "(All VOD)")):
            row = (await s.execute(select(m).where(m.portal_id == pid))).scalars().all()
            for g in row:
                if cond(g):
                    g.enabled = True
        await s.commit()

    await _run_items_fetch(Job(id="b", kind="fetch_items", portal_id=pid),
                           client, "t")

    # live fast path: only the enabled genre's channels are stored
    async with SessionLocal() as s:
        names = sorted((await s.execute(select(LiveSource.original_name).where(
            LiveSource.portal_id == pid))).scalars().all())
        assert names == ["News 1"]                         # "Sports 1" excluded

        live = {g.name: g for g in await _genres(LiveGenre, pid)}
        assert live["News"].channels_fetched is True and live["News"].item_count == 1
        assert live["Sports"].channels_fetched is False    # never fetched

    assert client.all_calls == 1
    assert client.vod_categories == [None]                 # synthetic "" -> all
    assert client.series_categories == []                  # series genre not enabled


async def test_items_fetch_with_nothing_enabled_does_not_call_the_portal():
    pid = await _portal()
    client = _FakeClient()
    await _sync_all_genres(Job(id="a", kind="fetch_genres", portal_id=pid),
                           client, pid, "t")

    await _run_items_fetch(Job(id="b", kind="fetch_items", portal_id=pid),
                           client, "t")

    assert client.all_calls == 0
    assert client.vod_categories == []
    assert client.series_categories == []
