"""
Input Sources → Live: Playlist custom-name column.

Replaces the old "Now" column (which hit /api/epg/now and cost one portal
request per visible channel). Editing the name either creates a new custom
channel or attaches the source as a fallback on an existing one.
"""

from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource, Portal
from app.services.playlist_sync import (assign_live_custom_group,
                                        assign_live_custom_name,
                                        live_playlist_links_for)


async def _seed(*, n: int = 3, enable: bool = True) -> list[int]:
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://portal.invalid")
        s.add(p)
        await s.flush()
        g = LiveGenre(portal_id=p.id, genre_portal_id="1", name="NL", enabled=True)
        s.add(g)
        await s.flush()
        ids = []
        for i in range(n):
            src = LiveSource(portal_id=p.id, live_genre_id=g.id,
                             portal_channel_id=str(100 + i),
                             original_name=f"NPO {i + 1} HD",
                             number=str(i + 1), cmd=f"ffmpeg http://x/{i}.ts",
                             enabled=enable)
            s.add(src)
            await s.flush()
            ids.append(src.id)
        await s.commit()
        return ids


async def test_live_list_carries_playlist_name_only_when_enabled():
    ids = await _seed(n=2, enable=False)
    async with SessionLocal() as s:
        src = await s.get(LiveSource, ids[0])
        src.enabled = True
        await s.commit()

    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/sources/live?per_page=50")
        assert r.status_code == 200, r.text
        by_id = {it["id"]: it for it in r.json()["items"]}
        on = by_id[ids[0]]
        off = by_id[ids[1]]
        assert on["enabled"] is True
        assert on["playlist_name"] == "NPO 1 HD"          # default = original
        assert on["playlist_group"] == "NL"                # default = source genre
        assert on["playlist_id"] is None                  # not linked yet
        assert off["enabled"] is False
        assert off["playlist_name"] == ""                 # blank when disabled
        assert off["playlist_group"] == ""
        assert off["playlist_id"] is None


async def test_enabling_live_sources_creates_channel_and_same_name_fallback():
    ids = await _seed(n=2, enable=False)
    async with SessionLocal() as s:
        second = await s.get(LiveSource, ids[1])
        second.original_name = "npo 1 hd"  # matching is case-insensitive
        await s.commit()

    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.post("/api/sources/toggle", json={
            "kind": "live", "ids": ids, "enabled": True,
        })
        assert response.status_code == 200, response.text
        sync = response.json()["playlist"]
        assert sync["created"] == 1
        assert sync["fallback"] == 1

        listing = await c.get("/api/sources/live?per_page=50")
        by_id = {item["id"]: item for item in listing.json()["items"]}
        assert by_id[ids[0]]["playlist_name"] == "NPO 1 HD"
        assert by_id[ids[0]]["playlist_is_primary"] is True
        assert by_id[ids[1]]["playlist_name"] == "NPO 1 HD"
        assert by_id[ids[1]]["playlist_is_primary"] is False
        assert by_id[ids[1]]["playlist_priority"] == 2

    async with SessionLocal() as s:
        playlists = (await s.execute(select(LivePlaylist))).scalars().all()
        assert len(playlists) == 1
        links = (await s.execute(select(LivePlaylistSource).order_by(
            LivePlaylistSource.priority))).scalars().all()
        assert [link.live_source_id for link in links] == ids


async def test_reenable_preserves_an_edited_live_custom_name():
    ids = await _seed(n=1)
    async with SessionLocal() as s:
        created = await assign_live_custom_name(s, ids[0], "My edited channel")
        await s.commit()

    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        for enabled in (False, True):
            response = await c.post("/api/sources/toggle", json={
                "kind": "live", "ids": ids, "enabled": enabled,
            })
            assert response.status_code == 200, response.text
        listing = await c.get("/api/sources/live?per_page=50")
        row = next(item for item in listing.json()["items"] if item["id"] == ids[0])
        assert row["playlist_id"] == created["playlist_id"]
        assert row["playlist_name"] == "My edited channel"


async def test_custom_group_updates_playlist_and_all_fallback_rows():
    ids = await _seed(n=2)
    async with SessionLocal() as s:
        first = await assign_live_custom_name(s, ids[0], "Shared")
        await assign_live_custom_name(s, ids[1], "Shared")
        changed = await assign_live_custom_group(s, ids[1], "Dutch Public TV")
        await s.commit()
        assert changed["playlist_id"] == first["playlist_id"]
        playlist = await s.get(LivePlaylist, first["playlist_id"])
        assert playlist.group_name == "Dutch Public TV"

    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        listing = await c.get("/api/sources/live?per_page=50")
        by_id = {item["id"]: item for item in listing.json()["items"]}
        assert by_id[ids[0]]["playlist_group"] == "Dutch Public TV"
        assert by_id[ids[1]]["playlist_group"] == "Dutch Public TV"

        response = await c.post(f"/api/sources/live/{ids[0]}/playlist-group",
                                json={"group_name": "News"})
        assert response.status_code == 200, response.text
        assert response.json()["group_name"] == "News"


async def test_custom_group_adds_a_legacy_unlinked_enabled_source():
    ids = await _seed(n=1)
    async with SessionLocal() as s:
        changed = await assign_live_custom_group(s, ids[0], "Sports")
        await s.commit()
        assert changed["custom_name"] == "NPO 1 HD"
        playlist = await s.get(LivePlaylist, changed["playlist_id"])
        assert playlist.group_name == "Sports"


async def test_unique_name_creates_a_custom_channel():
    ids = await _seed(n=1)
    async with SessionLocal() as s:
        res = await assign_live_custom_name(s, ids[0], "My NPO 1")
        await s.commit()
        assert res["action"] == "created"
        assert res["is_primary"] is True
        pl = await s.get(LivePlaylist, res["playlist_id"])
        assert pl is not None and pl.custom_name == "My NPO 1"
        links = (await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == pl.id))).scalars().all()
        assert len(links) == 1 and links[0].live_source_id == ids[0]
        assert links[0].priority == 1


async def test_existing_name_attaches_as_fallback():
    ids = await _seed(n=2)
    async with SessionLocal() as s:
        first = await assign_live_custom_name(s, ids[0], "Shared News")
        await s.commit()
        second = await assign_live_custom_name(s, ids[1], "shared news")  # case-insensitive
        await s.commit()
        assert first["action"] == "created"
        assert second["action"] == "fallback"
        assert second["playlist_id"] == first["playlist_id"]
        assert second["priority"] == 2
        assert second["is_primary"] is False

        links = (await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == first["playlist_id"])
            .order_by(LivePlaylistSource.priority))).scalars().all()
        assert [l.live_source_id for l in links] == ids
        rows = (await s.execute(select(LivePlaylist))).scalars().all()
        assert len(rows) == 1, "shared name must collapse onto one custom channel"


async def test_renaming_primary_keeps_the_channel_and_chain():
    ids = await _seed(n=2)
    async with SessionLocal() as s:
        a = await assign_live_custom_name(s, ids[0], "News")
        await assign_live_custom_name(s, ids[1], "News")          # fallback
        await s.commit()
        renamed = await assign_live_custom_name(s, ids[0], "News HD")
        await s.commit()
        assert renamed["action"] == "renamed"
        assert renamed["playlist_id"] == a["playlist_id"]
        pl = await s.get(LivePlaylist, a["playlist_id"])
        assert pl.custom_name == "News HD"
        n_links = len((await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == pl.id))).scalars().all())
        assert n_links == 2, "fallback chain must survive a rename"


async def test_endpoint_rejects_disabled_and_empty_name():
    ids = await _seed(n=1, enable=False)
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(f"/api/sources/live/{ids[0]}/playlist-name",
                         json={"custom_name": "X"})
        assert r.status_code == 400
        assert "enable" in r.text.lower()

    async with SessionLocal() as s:
        src = await s.get(LiveSource, ids[0])
        src.enabled = True
        await s.commit()
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(f"/api/sources/live/{ids[0]}/playlist-name",
                         json={"custom_name": "  "})
        assert r.status_code == 400
        ok = await c.post(f"/api/sources/live/{ids[0]}/playlist-name",
                          json={"custom_name": "Created Via API"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["action"] == "created"
        # List payload now reflects the link.
        listing = await c.get("/api/sources/live?per_page=50")
        row = next(i for i in listing.json()["items"] if i["id"] == ids[0])
        assert row["playlist_name"] == "Created Via API"
        assert row["playlist_is_primary"] is True


async def test_links_helper_is_one_batch():
    ids = await _seed(n=4)
    async with SessionLocal() as s:
        await assign_live_custom_name(s, ids[0], "A")
        await assign_live_custom_name(s, ids[1], "A")
        await assign_live_custom_name(s, ids[2], "B")
        await s.commit()
        links = await live_playlist_links_for(s, ids)
    assert set(links) == {ids[0], ids[1], ids[2]}
    assert links[ids[0]]["is_primary"] is True
    assert links[ids[1]]["is_primary"] is False
    assert links[ids[1]]["custom_name"] == "A"
    assert links[ids[1]]["chain_len"] == 2
    assert ids[3] not in links


async def test_epg_now_endpoint_is_gone():
    """The per-page portal short-EPG call is removed entirely."""
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/epg/now?live=1,2,3")
        assert r.status_code == 404
