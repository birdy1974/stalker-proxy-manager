"""Xtream output against what OwnTV's client reads (see OwnTV_Core XtreamClient.kt).

* `category_id` filters the bulk lists. OwnTV re-asks one category at a time when
  a full list comes back truncated; an unfiltered answer meant every category
  re-downloaded the whole catalogue.
* `get_short_epg` answers the next programmes of one channel, base64 title and
  description, unix-second timestamps - the shape the client decodes.
* `user_info.status` says "Expired" once the expiry date has passed, "Active"
  otherwise (and for accounts without a date, as before).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import (LivePlaylist, Portal, SeriePlaylist, SerieSource, User, VodPlaylist,
                        VodSource)
from app.services import epg_policy
from app.services.playlist_gen import xtream_account_status

BASE = "http://testserver"


ALL_GROUPS = json.dumps({"live": ["News", "Kids", "Documentary"],
                         "vod": ["Action", "Family"], "series": ["Drama", "Comedy"], "local": []})


async def _user(**kw) -> None:
    # A user with no group whitelist sees nothing (deliberate: see _allowed).
    d = dict(name="xtu", password="secret", enabled=True, m3u_enabled=True,
             xtream_enabled=True, max_connections=2, groups_json=ALL_GROUPS)
    d.update(kw)
    async with SessionLocal() as s:
        s.add(User(**d))
        await s.commit()


async def _add_live(name: str, group: str, order: int) -> int:
    async with SessionLocal() as s:
        row = LivePlaylist(custom_name=name, group_name=group, enabled=True, order=order)
        s.add(row)
        await s.commit()
        return row.id


async def _api(params: dict) -> object:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/player_api.php",
                        params={"username": "xtu", "password": "secret", **params})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------- category filter

async def test_live_streams_are_filtered_to_the_requested_category():
    await _user()
    news = await _add_live("CNN", "News", 1)
    kids = await _add_live("Cartoon", "Kids", 2)
    news2 = await _add_live("BBC News", "News", 3)

    cats = {c["category_name"]: c["category_id"]
            for c in await _api({"action": "get_live_categories"})}
    everything = await _api({"action": "get_live_streams"})
    assert {x["stream_id"] for x in everything} == {news, kids, news2}

    only_news = await _api({"action": "get_live_streams", "category_id": cats["News"]})
    assert {x["stream_id"] for x in only_news} == {news, news2}
    assert {x["category_id"] for x in only_news} == {cats["News"]}

    assert await _api({"action": "get_live_streams", "category_id": "999"}) == []


async def test_vod_and_series_honour_category_id():
    await _user()
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/", resolved_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        src = SerieSource(portal_id=portal.id, portal_item_id="1", original_name="Ashes")
        s.add(src)
        await s.flush()
        vsrc = VodSource(portal_id=portal.id, portal_item_id="2", original_name="Heat")
        s.add(vsrc)
        await s.flush()
        s.add(VodPlaylist(vod_source_id=vsrc.id, custom_name="Heat", group_name="Action",
                          enabled=True, order=1))
        s.add(VodPlaylist(vod_source_id=vsrc.id, custom_name="Up", group_name="Family",
                          enabled=True, order=2))
        s.add(SeriePlaylist(serie_source_id=src.id, custom_name="Ashes", group_name="Drama",
                            enabled=True, order=1))
        s.add(SeriePlaylist(serie_source_id=src.id, custom_name="Laugh", group_name="Comedy",
                            enabled=True, order=2))
        await s.commit()

    vcats = {c["category_name"]: c["category_id"]
             for c in await _api({"action": "get_vod_categories"})}
    vod = await _api({"action": "get_vod_streams", "category_id": vcats["Action"]})
    assert [x["name"] for x in vod] == ["Heat"]

    scats = {c["category_name"]: c["category_id"]
             for c in await _api({"action": "get_series_categories"})}
    ser = await _api({"action": "get_series", "category_id": scats["Comedy"]})
    assert [x["name"] for x in ser] == ["Laugh"]

    # no category_id -> the full list, as before
    assert len(await _api({"action": "get_vod_streams"})) == 2
    assert len(await _api({"action": "get_series"})) == 2


# ----------------------------------------------------------------- short EPG

def _event(title: str, start: datetime, minutes: int, desc: str | None = None):
    prog = SimpleNamespace(title=title, desc=desc, sub_title=None, category=None, icon=None)
    return SimpleNamespace(programme=prog, start=start, stop=start + timedelta(minutes=minutes),
                           source_id=1)


async def test_short_epg_returns_the_next_programmes_in_the_clients_shape(monkeypatch):
    await _user()
    chan = await _add_live("BBC News", "News", 1)
    now = datetime.now(timezone.utc)
    events = [
        _event("Ended", now - timedelta(hours=2), 60),                        # over: skipped
        _event("Now", now - timedelta(minutes=10), 40, desc="Today's top stories"),
        _event("Next", now + timedelta(minutes=30), 30),
    ]

    async def fake_load(db, items, now=None, horizon_hours=48):
        return {it.id: events for it in items}, {}, {}, {}

    monkeypatch.setattr(epg_policy, "load_schedules", fake_load)
    body = await _api({"action": "get_short_epg", "stream_id": chan, "limit": 6})
    listings = body["epg_listings"]
    assert [base64.b64decode(x["title"]).decode() for x in listings] == ["Now", "Next"]
    now_entry = listings[0]
    assert base64.b64decode(now_entry["description"]).decode() == "Today's top stories"
    # OwnTV reads unix seconds as strings
    assert int(now_entry["start_timestamp"]) < int(now_entry["stop_timestamp"])
    assert now_entry["start_timestamp"].isdigit() and now_entry["stop_timestamp"].isdigit()

    capped = await _api({"action": "get_short_epg", "stream_id": chan, "limit": 1})
    assert len(capped["epg_listings"]) == 1


async def test_short_epg_for_an_unknown_channel_is_an_empty_list(monkeypatch):
    await _user()

    async def fake_load(db, items, now=None, horizon_hours=48):
        raise AssertionError("no lookup for a channel that does not exist")

    monkeypatch.setattr(epg_policy, "load_schedules", fake_load)
    assert await _api({"action": "get_short_epg", "stream_id": 424242}) == {"epg_listings": []}


# -------------------------------------------------------------------- status

def test_status_is_expired_only_after_the_expiry_date():
    now = int(datetime.now(timezone.utc).timestamp())
    past = (datetime.now() - timedelta(days=3)).date().isoformat()
    future = (datetime.now() + timedelta(days=3)).date().isoformat()
    assert xtream_account_status(User(expire_date=past), now) == "Expired"
    assert xtream_account_status(User(expire_date=future), now) == "Active"
    assert xtream_account_status(User(expire_date=None), now) == "Active"
    assert xtream_account_status(User(expire_date="not a date"), now) == "Active"


async def test_an_expired_account_is_still_refused_at_the_door():
    """Pinned on purpose: `verify` turns an expired account away before any
    Xtream answer is built, so the client sees 403 (not a status of "Expired").
    Changing that is an access decision, not a formatting fix."""
    await _user(expire_date=(datetime.now() - timedelta(days=1)).date().isoformat())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/player_api.php", params={"username": "xtu", "password": "secret"})
    assert r.status_code == 403
