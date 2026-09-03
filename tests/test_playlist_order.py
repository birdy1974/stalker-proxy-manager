"""
Playlist Builder row order: VOD / series / local must stay stable when a
template is assigned, and must be drag-reorderable like live channels.
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import (
    FFmpegTemplate, LocalFile, LocalPlaylist, LocalSource, Portal,
    SeriePlaylist, SerieSource, VodPlaylist, VodSource,
)


async def _client():
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _seed_vod(n: int = 5, *, same_order: int | None = 0) -> list[int]:
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://portal.invalid")
        s.add(p)
        await s.flush()
        ids = []
        for i in range(n):
            src = VodSource(portal_id=p.id, portal_item_id=str(i),
                            original_name=f"Movie {i}", enabled=True)
            s.add(src)
            await s.flush()
            pl = VodPlaylist(vod_source_id=src.id, custom_name=src.original_name,
                             group_name="VOD", enabled=True,
                             order=same_order if same_order is not None else i + 1)
            s.add(pl)
            await s.flush()
            ids.append(pl.id)
        tpl = FFmpegTemplate(name="tpl-a")
        s.add(tpl)
        await s.commit()
        return ids


async def test_vod_list_tiebreaks_equal_order_by_id():
    ids = await _seed_vod(5, same_order=0)
    async with await _client() as c:
        r = await c.get("/api/playlist/vod?per_page=50")
        assert r.status_code == 200, r.text
        got = [it["id"] for it in r.json()["items"]]
        assert got == ids, "equal order values must still list in id order"


async def test_changing_vod_template_does_not_reshuffle():
    ids = await _seed_vod(6, same_order=0)
    async with SessionLocal() as s:
        tpl = (await s.execute(
            __import__("sqlalchemy").select(FFmpegTemplate))).scalars().first()
        tpl_id = tpl.id
    async with await _client() as c:
        before = [it["id"] for it in (await c.get("/api/playlist/vod?per_page=50")).json()["items"]]
        # Assign a template to a row in the middle — the old bug was that this
        # UPDATE made SQLite return equal-order rows in a new sequence.
        mid = before[3]
        put = await c.put(f"/api/playlist/vod/{mid}", json={"ffmpeg_template_id": tpl_id})
        assert put.status_code == 200, put.text
        after = [it["id"] for it in (await c.get("/api/playlist/vod?per_page=50")).json()["items"]]
        assert after == before
        row = next(it for it in (await c.get("/api/playlist/vod?per_page=50")).json()["items"]
                   if it["id"] == mid)
        assert row["ffmpeg_template_id"] == tpl_id


async def test_vod_order_endpoint_permutes():
    ids = await _seed_vod(3, same_order=None)
    async with await _client() as c:
        listed = (await c.get("/api/playlist/vod?per_page=50")).json()["items"]
        assert [it["id"] for it in listed] == ids
        # Reverse the page.
        payload = {"items": [{"id": ids[2], "order": 1},
                             {"id": ids[1], "order": 2},
                             {"id": ids[0], "order": 3}]}
        r = await c.post("/api/playlist/vod/order", json=payload)
        assert r.status_code == 200, r.text
        after = (await c.get("/api/playlist/vod?per_page=50")).json()["items"]
        assert [it["id"] for it in after] == [ids[2], ids[1], ids[0]]
        assert [it["order"] for it in after] == [1, 2, 3]


async def test_series_and_local_order_endpoints():
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://portal.invalid")
        s.add(p)
        await s.flush()
        src = SerieSource(portal_id=p.id, portal_item_id="1",
                          original_name="Show", enabled=True)
        s.add(src)
        await s.flush()
        a = SeriePlaylist(serie_source_id=src.id, custom_name="A", order=1)
        b = SeriePlaylist(serie_source_id=src.id, custom_name="B", order=2)
        s.add_all([a, b])
        ls = LocalSource(directory="/tmp/x")
        s.add(ls)
        await s.flush()
        f1 = LocalFile(local_source_id=ls.id, relative_path="a.mkv", filename="a.mkv")
        f2 = LocalFile(local_source_id=ls.id, relative_path="b.mkv", filename="b.mkv")
        s.add_all([f1, f2])
        await s.flush()
        la = LocalPlaylist(local_file_id=f1.id, custom_name="a", order=1)
        lb = LocalPlaylist(local_file_id=f2.id, custom_name="b", order=2)
        s.add_all([la, lb])
        await s.commit()
        sa, sb, lia, lib = a.id, b.id, la.id, lb.id

    async with await _client() as c:
        r = await c.post("/api/playlist/series/order",
                         json={"items": [{"id": sa, "order": 20}, {"id": sb, "order": 10}]})
        assert r.status_code == 200, r.text
        series = (await c.get("/api/playlist/series?per_page=50")).json()["items"]
        assert [it["id"] for it in series] == [sb, sa]

        r = await c.post("/api/playlist/local/order",
                         json={"items": [{"id": lia, "order": 20}, {"id": lib, "order": 10}]})
        assert r.status_code == 200, r.text
        local = (await c.get("/api/playlist/local?per_page=50")).json()["items"]
        assert [it["id"] for it in local] == [lib, lia]
