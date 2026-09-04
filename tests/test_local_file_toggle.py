"""Clicking On/Off on a scanned local file must flip enabled and the playlist."""

from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import LocalFile, LocalPlaylist, LocalSource


async def test_toggle_local_file_disables_playlist_row():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with SessionLocal() as s:
        ls = LocalSource(directory="/tmp/spm-local-toggle")
        s.add(ls)
        await s.flush()
        f = LocalFile(local_source_id=ls.id, relative_path="clip.mkv",
                      filename="clip.mkv", enabled=True)
        s.add(f)
        await s.flush()
        pl = LocalPlaylist(local_file_id=f.id, custom_name="clip.mkv",
                           enabled=True, order=1)
        s.add(pl)
        await s.commit()
        fid, pid = f.id, pl.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        listed = await c.get("/api/sources/local/files?per_page=50")
        assert listed.status_code == 200, listed.text
        row = next(i for i in listed.json()["items"] if i["id"] == fid)
        assert row["enabled"] is True

        off = await c.post("/api/sources/local/files/toggle",
                           json={"ids": [fid], "enabled": False})
        assert off.status_code == 200, off.text
        assert off.json()["count"] == 1

        listed = await c.get("/api/sources/local/files?per_page=50")
        row = next(i for i in listed.json()["items"] if i["id"] == fid)
        assert row["enabled"] is False

    async with SessionLocal() as s:
        src = await s.get(LocalFile, fid)
        assert src is not None and src.enabled is False
        pl = await s.get(LocalPlaylist, pid)
        assert pl is not None and pl.enabled is False

    async with AsyncClient(transport=transport, base_url="http://t") as c:
        on = await c.post("/api/sources/local/files/toggle",
                          json={"ids": [fid], "enabled": True})
        assert on.status_code == 200, on.text

    async with SessionLocal() as s:
        src = await s.get(LocalFile, fid)
        assert src.enabled is True
        pl = (await s.execute(select(LocalPlaylist).where(
            LocalPlaylist.local_file_id == fid))).scalars().first()
        assert pl is not None and pl.enabled is True


async def test_toggle_local_files_bulk():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    async with SessionLocal() as s:
        ls = LocalSource(directory="/tmp/spm-local-toggle-bulk")
        s.add(ls)
        await s.flush()
        f1 = LocalFile(local_source_id=ls.id, relative_path="a.mkv",
                       filename="a.mkv", enabled=True)
        f2 = LocalFile(local_source_id=ls.id, relative_path="b.mkv",
                       filename="b.mkv", enabled=True)
        s.add_all([f1, f2])
        await s.flush()
        p1 = LocalPlaylist(local_file_id=f1.id, custom_name="a.mkv",
                           enabled=True, order=1)
        p2 = LocalPlaylist(local_file_id=f2.id, custom_name="b.mkv",
                           enabled=True, order=2)
        s.add_all([p1, p2])
        await s.commit()
        ids = [f1.id, f2.id]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        off = await c.post("/api/sources/local/files/toggle",
                           json={"ids": ids, "enabled": False})
        assert off.status_code == 200, off.text
        assert off.json()["count"] == 2

    async with SessionLocal() as s:
        for fid in ids:
            src = await s.get(LocalFile, fid)
            assert src is not None and src.enabled is False
            pl = (await s.execute(select(LocalPlaylist).where(
                LocalPlaylist.local_file_id == fid))).scalars().first()
            assert pl is not None and pl.enabled is False

    async with AsyncClient(transport=transport, base_url="http://t") as c:
        on = await c.post("/api/sources/local/files/toggle",
                          json={"ids": ids, "enabled": True})
        assert on.status_code == 200, on.text
        assert on.json()["count"] == 2

    async with SessionLocal() as s:
        for fid in ids:
            src = await s.get(LocalFile, fid)
            assert src is not None and src.enabled is True
            pl = (await s.execute(select(LocalPlaylist).where(
                LocalPlaylist.local_file_id == fid))).scalars().first()
            assert pl is not None and pl.enabled is True
