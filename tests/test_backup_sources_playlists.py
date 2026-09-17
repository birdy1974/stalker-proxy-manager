"""Backup / restore of sources (live, vod, series, local files) and
playlists (live, vod, series, local) - export sections and the restore
semantics of /api/import."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    FFmpegTemplate, LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource,
    LocalFile, LocalPlaylist, LocalSource, Portal, SerieEpisode, SerieGenre,
    SeriePlaylist, SeriePlaylistSeason, SeriePlaylistSource, SerieSeason,
    SerieSource, VodGenre, VodPlaylist, VodPlaylistSource, VodSource,
)


async def _seed():
    """One portal + genres + a full source tree + one playlist of every kind
    with fallback links, the shape a real catalog has after a fetch."""
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://p.invalid")
        s.add(portal)
        await s.flush()
        lg = LiveGenre(portal_id=portal.id, genre_portal_id="g1", name="Sport", enabled=True)
        vg = VodGenre(portal_id=portal.id, genre_portal_id="g2", name="Action", enabled=True)
        sg = SerieGenre(portal_id=portal.id, genre_portal_id="g3", name="Drama", enabled=True)
        tpl = FFmpegTemplate(name="Test tpl")
        s.add_all([lg, vg, sg, tpl])
        await s.flush()
        l1 = LiveSource(portal_id=portal.id, live_genre_id=lg.id, portal_channel_id="101",
                        number=101, original_name="Ch One", cmd="http://p.invalid/101.ts",
                        logo_original="http://p.invalid/101.png", epg_original="Ch One",
                        enabled=True)
        l2 = LiveSource(portal_id=portal.id, live_genre_id=lg.id, portal_channel_id="102",
                        number=102, original_name="Ch Two", cmd="http://p.invalid/102.ts",
                        enabled=False)
        v1 = VodSource(portal_id=portal.id, vod_genre_id=vg.id, portal_item_id="v1",
                       original_name="Film X", cmd="http://p.invalid/v1.mp4",
                       poster="http://p.invalid/v1.jpg", year=2020, description="a film",
                       genre="Action", enabled=True)
        sr = SerieSource(portal_id=portal.id, serie_genre_id=sg.id, portal_item_id="s1",
                         original_name="Show", enabled=True)
        s.add_all([l1, l2, v1, sr])
        await s.flush()
        se1 = SerieSeason(serie_source_id=sr.id, portal_season_id="ps1",
                          season_number=1, name="Season 1", enabled=True)
        se2 = SerieSeason(serie_source_id=sr.id, portal_season_id="ps2",
                          season_number=2, name="Season 2", enabled=False)
        s.add_all([se1, se2])
        await s.flush()
        s.add_all([
            SerieEpisode(serie_season_id=se1.id, portal_item_id="e1", episode_number=1,
                         name="S1E1", cmd="http://p.invalid/e1.mp4", duration=120),
            SerieEpisode(serie_season_id=se1.id, portal_item_id="e2", episode_number=2,
                         name="S1E2", cmd="http://p.invalid/e2.mp4"),
            SerieEpisode(serie_season_id=se2.id, portal_item_id="e3", episode_number=1,
                         name="S2E1", cmd="http://p.invalid/e3.mp4"),
        ])
        d = LocalSource(directory="/media/movies", enabled=True, recursive=False)
        s.add(d)
        await s.flush()
        f1 = LocalFile(local_source_id=d.id, relative_path="a.mp4", filename="a.mp4",
                       size_bytes=10, mtime=1, duration_s=100, enabled=True)
        f2 = LocalFile(local_source_id=d.id, relative_path="sub/b.mkv", filename="b.mkv",
                       size_bytes=20, mtime=2, duration_s=200, enabled=False)
        s.add_all([f1, f2])
        await s.flush()
        lp = LivePlaylist(custom_name="LIVE-1", group_name="TV", number=1, epg_id="1",
                          logo="http://p.invalid/l.png", ffmpeg_template_id=tpl.id,
                          enabled=True, order=1)
        s.add(lp)
        await s.flush()
        s.add_all([
            LivePlaylistSource(live_playlist_id=lp.id, live_source_id=l1.id, priority=1),
            LivePlaylistSource(live_playlist_id=lp.id, live_source_id=l2.id, priority=2),
        ])
        vp = VodPlaylist(vod_source_id=v1.id, custom_name="F-X", group_name="Films",
                         poster="http://p.invalid/vp.jpg", year=2020,
                         ffmpeg_template_id=tpl.id, enabled=True, order=1)
        s.add(vp)
        await s.flush()
        s.add(VodPlaylistSource(vod_playlist_id=vp.id, vod_source_id=v1.id, priority=1))
        sp = SeriePlaylist(serie_source_id=sr.id, custom_name="SHOW", group_name="Series",
                           ffmpeg_template_id=tpl.id, enabled=True, order=1)
        s.add(sp)
        await s.flush()
        s.add_all([
            SeriePlaylistSource(serie_playlist_id=sp.id, serie_source_id=sr.id, priority=1),
            SeriePlaylistSeason(serie_playlist_id=sp.id, serie_season_id=se1.id, enabled=True),
            SeriePlaylistSeason(serie_playlist_id=sp.id, serie_season_id=se2.id, enabled=False),
        ])
        s.add(LocalPlaylist(local_file_id=f1.id, custom_name="LOCAL-A", group_name="Local",
                            ffmpeg_template_id=tpl.id, enabled=True, order=1))
        await s.commit()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _export_all() -> dict:
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "all"})
        assert r.status_code == 200, r.text
        return r.json()


async def _wipe_catalog():
    """delete every source/playlist row (and genres) - the 'new install' state"""
    async with SessionLocal() as s:
        for m in (LivePlaylistSource, VodPlaylistSource, SeriePlaylistSource,
                  SeriePlaylistSeason, LivePlaylist, VodPlaylist,
                  SeriePlaylist, LocalPlaylist,
                  SerieEpisode, SerieSeason, LiveSource, VodSource,
                  SerieSource, LocalFile, LocalSource,
                  LiveGenre, VodGenre, SerieGenre):
            for row in (await s.execute(select(m))).scalars().all():
                await s.delete(row)
        await s.commit()


async def test_export_sources_section_shape():
    await _seed()
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "sources"})
        assert r.status_code == 200, r.text
        src = r.json()["sources"]
    assert src["live"][0]["portal"] == "p"
    assert src["live"][0]["genre"] == "Sport"
    assert src["live"][0]["genre_portal_id"] == "g1"
    assert src["live"][0]["portal_channel_id"] == "101"
    assert src["live"][0]["cmd"] == "http://p.invalid/101.ts"
    assert [row["enabled"] for row in src["live"]] == [True, False]
    assert src["vod"][0]["genre_text"] == "Action"
    assert src["vod"][0]["description"] == "a film"
    show = src["series"][0]
    assert show["portal_item_id"] == "s1"
    assert [st["season_number"] for st in show["seasons"]] == [1, 2]
    assert [ep["episode_number"] for ep in show["seasons"][0]["episodes"]] == [1, 2]
    assert show["seasons"][1]["enabled"] is False
    assert [f["relative_path"] for f in src["local"][0]["files"]] == ["a.mp4", "sub/b.mkv"]
    assert src["local"][0]["files"][1]["enabled"] is False
    # name-keyed genre rows travel with the sources for a portable restore
    assert {"portal": "p", "genre_portal_id": "g1", "name": "Sport", "enabled": True} in \
        src["live_genres_by_name"]


async def test_export_playlists_section_shape():
    await _seed()
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "playlists"})
        assert r.status_code == 200, r.text
        pls = r.json()["playlists"]
    live = pls["live"][0]
    assert live["custom_name"] == "LIVE-1"
    assert live["ffmpeg_template"] == "Test tpl"
    assert [(s["portal_channel_id"], s["priority"]) for s in live["sources"]] == \
        [("101", 1), ("102", 2)]
    vod = pls["vod"][0]
    assert vod["primary"]["portal_item_id"] == "v1"
    assert vod["sources"][0]["priority"] == 1
    show = pls["series"][0]
    assert show["primary"]["portal_item_id"] == "s1"
    # a season pick names the serie it belongs to
    assert {"portal": "p", "portal_item_id": "s1", "season_number": 1,
            "enabled": True} in show["seasons"]
    loc = pls["local"][0]
    assert loc["file"] == {"directory": "/media/movies",
                           "relative_path": "a.mp4", "filename": "a.mp4"}


async def test_export_all_includes_sources_and_playlists():
    await _seed()
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "all"})
        assert r.status_code == 200, r.text
        data = r.json()
    assert "sources" in data and "playlists" in data
    assert len(data["sources"]["live"]) == 2
    assert len(data["playlists"]["live"]) == 1


async def test_full_restore_onto_fresh_id_space():
    """the point of the feature: wipe every row (a new install keeps only its
    id space), import the backup, and the catalog - with its name bindings -
    comes back whole."""
    await _seed()
    backup = await _export_all()
    # the id-keyed genre arrays reference the OLD install's id space - drop
    # them so only the portable name-keyed copy can do the work
    for key in ("live_genres", "vod_genres", "serie_genres"):
        backup.pop(key, None)
    # a fresh install: also drop the portal + template so name-rebinding runs
    async with SessionLocal() as s:
        for m in (Portal, FFmpegTemplate):
            for row in (await s.execute(select(m))).scalars().all():
                await s.delete(row)
        await s.commit()
    await _wipe_catalog()
    # occupy id 1 so the restored portal lands on a DIFFERENT id than the
    # backup was written on - the restore must not lean on id equality
    async with SessionLocal() as s:
        s.add(Portal(name="zz", base_url="http://zz.invalid"))
        await s.commit()

    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": backup})
        assert r.status_code == 200, r.text
        applied = r.json()
    assert applied["updated"] == 0
    assert not [x for x in applied["skipped"] if "(portal missing)" in x]

    async with SessionLocal() as s:
        portal = (await s.execute(select(Portal).where(Portal.name == "p"))).scalar_one()
        tpl = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == "Test tpl"))).scalar_one()
        lg = (await s.execute(select(LiveGenre).where(
            LiveGenre.portal_id == portal.id,
            LiveGenre.genre_portal_id == "g1"))).scalar_one()
        ch1 = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == portal.id,
            LiveSource.portal_channel_id == "101"))).scalar_one()
        assert ch1.live_genre_id == lg.id           # genre re-bound by id
        assert ch1.logo_original == "http://p.invalid/101.png"
        ch2 = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == portal.id,
            LiveSource.portal_channel_id == "102"))).scalar_one()
        assert ch2.enabled is False                 # curation survived
        v = (await s.execute(select(VodSource).where(
            VodSource.portal_id == portal.id,
            VodSource.portal_item_id == "v1"))).scalar_one()
        assert v.description == "a film" and v.genre == "Action"
        sr = (await s.execute(select(SerieSource).where(
            SerieSource.portal_id == portal.id,
            SerieSource.portal_item_id == "s1"))).scalar_one()
        seasons = (await s.execute(select(SerieSeason).where(
            SerieSeason.serie_source_id == sr.id))).scalars().all()
        assert sorted(st.season_number for st in seasons) == [1, 2]
        se1 = next(st for st in seasons if st.season_number == 1)
        eps = (await s.execute(select(SerieEpisode).where(
            SerieEpisode.serie_season_id == se1.id))).scalars().all()
        assert sorted(ep.episode_number for ep in eps) == [1, 2]
        d = (await s.execute(select(LocalSource).where(
            LocalSource.directory == "/media/movies"))).scalar_one()
        files = (await s.execute(select(LocalFile).where(
            LocalFile.local_source_id == d.id))).scalars().all()
        assert sorted(f.relative_path for f in files) == ["a.mp4", "sub/b.mkv"]
        # playlists: template re-bound BY NAME, links + picks re-bound by id
        lp = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "LIVE-1"))).scalar_one()
        assert lp.ffmpeg_template_id == tpl.id
        assert lp.epg_id == "1"
        links = (await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == lp.id))).scalars().all()
        assert sorted(l.priority for l in links) == [1, 2]
        vp = (await s.execute(select(VodPlaylist).where(
            VodPlaylist.custom_name == "F-X"))).scalar_one()
        assert vp.vod_source_id == v.id             # primary FK re-bound
        sp = (await s.execute(select(SeriePlaylist).where(
            SeriePlaylist.custom_name == "SHOW"))).scalar_one()
        picks = (await s.execute(select(SeriePlaylistSeason).where(
            SeriePlaylistSeason.serie_playlist_id == sp.id))).scalars().all()
        assert sorted(p.serie_season_id for p in picks) == sorted(
            st.id for st in seasons)
        a = next(f for f in files if f.relative_path == "a.mp4")
        plp = (await s.execute(select(LocalPlaylist).where(
            LocalPlaylist.custom_name == "LOCAL-A"))).scalar_one()
        assert plp.local_file_id == a.id


async def test_restore_updates_existing_rows_to_backup_state():
    await _seed()
    backup = await _export_all()
    # the local install drifted: a channel disabled, a group renamed, a
    # priority swapped - a restore must put the backup's values back
    async with SessionLocal() as s:
        ch1 = (await s.execute(select(LiveSource).where(
            LiveSource.portal_channel_id == "101"))).scalar_one()
        ch1.enabled = False
        lp = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "LIVE-1"))).scalar_one()
        lp.group_name = "Drifted"
        link = (await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == lp.id,
            LivePlaylistSource.priority == 1))).scalar_one()
        link.priority = 9
        await s.commit()

    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": backup})
        assert r.status_code == 200, r.text
        applied = r.json()
    assert applied["updated"] > 0

    async with SessionLocal() as s:
        ch1 = (await s.execute(select(LiveSource).where(
            LiveSource.portal_channel_id == "101"))).scalar_one()
        assert ch1.enabled is True
        lp = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "LIVE-1"))).scalar_one()
        assert lp.group_name == "TV"
        link = (await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == lp.id))).scalars().all()
        assert sorted(l.priority for l in link) == [1, 2]
        # no duplication: the restore updated, it did not append
        assert len(link) == 2


async def test_import_notes_missing_portals_and_sources():
    await _seed()
    backup = await _export_all()
    backup["sources"]["live"].append({
        "portal": "ghost", "portal_channel_id": "999", "original_name": "Ghost",
        "cmd": "http://ghost.invalid/x.ts", "enabled": True,
    })
    backup["playlists"]["live"].append({
        "custom_name": "GHOST", "group_name": "TV", "enabled": True, "order": 9,
        "sources": [
            {"portal": "ghost", "portal_channel_id": "999",
             "original_name": "Ghost", "priority": 1},
        ],
    })
    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": backup})
        assert r.status_code == 200, r.text
        applied = r.json()
    assert any("portal missing" in x and "Ghost" in x for x in applied["skipped"])
    assert any(x.startswith("live-link:GHOST->") for x in applied["skipped"])
    async with SessionLocal() as s:
        assert (await s.execute(select(LiveSource).where(
            LiveSource.portal_channel_id == "999"))).scalar_one_or_none() is None


async def test_new_playlist_with_unresolvable_primary_is_skipped():
    await _seed()
    data = {"playlists": {"vod": [{
        "custom_name": "NO-PRIMARY", "group_name": "Films", "enabled": True,
        "order": 1,
        "primary": {"portal": "p", "portal_item_id": "does-not-exist"},
        "sources": [],
    }]}}
    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": data})
        assert r.status_code == 200, r.text
        applied = r.json()
    assert any("NO-PRIMARY" in x and "primary source missing" in x
               for x in applied["skipped"])
    async with SessionLocal() as s:
        assert (await s.execute(select(VodPlaylist).where(
            VodPlaylist.custom_name == "NO-PRIMARY"))).scalar_one_or_none() is None


async def test_legacy_live_playlist_section_still_imports():
    """files written before the `playlists` section carry `live_playlist`
    rows without source links; importing them must still work."""
    await _seed()
    async with SessionLocal() as s:
        tpl = (await s.execute(select(FFmpegTemplate))).scalars().first()
        assert tpl is not None
        tpl_id = tpl.id
    data = {"live_playlist": [{
        "custom_name": "LIVE-1", "group_name": "Legacy", "number": 7,
        "epg_id": "77", "logo": "http://p.invalid/leg.png",
        "ffmpeg_template_id": tpl_id, "enabled": True, "order": 3,
    }]}
    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": data})
        assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        lp = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "LIVE-1"))).scalar_one()
        assert lp.group_name == "Legacy"
        assert lp.number == 7
        assert lp.ffmpeg_template_id == tpl_id
        # the seeded links survived the legacy restore (nothing is deleted)
        assert (await s.execute(select(LivePlaylistSource).where(
            LivePlaylistSource.live_playlist_id == lp.id))).scalars().all()


async def test_live_number_lock_roundtrip():
    """a locked channel number rides along in the playlists backup and is
    restored - a moved install keeps its frozen positions."""
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://p.invalid")
        s.add(portal)
        await s.flush()
        lg = LiveGenre(portal_id=portal.id, genre_portal_id="g1", name="Sport", enabled=True)
        s.add(lg)
        await s.flush()
        l1 = LiveSource(portal_id=portal.id, live_genre_id=lg.id, portal_channel_id="101",
                        number=101, original_name="Ch One", cmd="http://p.invalid/101.ts",
                        enabled=True)
        l2 = LiveSource(portal_id=portal.id, live_genre_id=lg.id, portal_channel_id="102",
                        number=102, original_name="Ch Two", cmd="http://p.invalid/102.ts",
                        enabled=True)
        s.add_all([l1, l2])
        await s.flush()
        lp1 = LivePlaylist(custom_name="LIVE-A", number=1, enabled=True, order=1)
        lp2 = LivePlaylist(custom_name="LIVE-B", number=2, lock_number=True,
                           enabled=True, order=2)
        s.add_all([lp1, lp2])
        await s.flush()
        s.add_all([
            LivePlaylistSource(live_playlist_id=lp1.id, live_source_id=l1.id, priority=1),
            LivePlaylistSource(live_playlist_id=lp2.id, live_source_id=l2.id, priority=1),
        ])
        await s.commit()

    async with _client() as c:
        r = await c.get("/api/export", params={"section": "playlists"})
        assert r.status_code == 200, r.text
        pls = r.json()["playlists"]
    by_name = {e["custom_name"]: e for e in pls["live"]}
    assert by_name["LIVE-A"]["lock_number"] is False
    assert by_name["LIVE-B"]["lock_number"] is True

    # the playlist rows are gone; a restore brings the lock back
    async with SessionLocal() as s:
        for m in (LivePlaylistSource, LivePlaylist):
            for row in (await s.execute(select(m))).scalars().all():
                await s.delete(row)
        await s.commit()
    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": {"playlists": pls}})
        assert r.status_code == 200, r.text
    async with SessionLocal() as s:
        a = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "LIVE-A"))).scalar_one()
        b = (await s.execute(select(LivePlaylist).where(
            LivePlaylist.custom_name == "LIVE-B"))).scalar_one()
    assert a.lock_number is False
    assert b.lock_number is True and b.number == 2

# ---------------------------------------------------------------- areas / enigma2
from app.models import Area, AreaItemTemplate, Enigma2Profile, FFmpegTemplate, User  # noqa: E402


async def _seed_area_and_enigma2():
    async with SessionLocal() as s:
        tpl = FFmpegTemplate(name="E2 tpl")
        s.add(tpl)
        await s.flush()
        area = Area(name="Phone", enabled=True, notes="handset",
                    ffmpeg_template_vod_id=tpl.id)
        s.add(area)
        await s.flush()
        s.add(User(name="alice", password="pw"))
        await s.flush()
        s.add(AreaItemTemplate(area_id=area.id, kind="live", playlist_id=1,
                               ffmpeg_template_id=tpl.id))
        prof = Enigma2Profile(name="Vu+ Duo2", enabled=True, user_id=None,
                              token="tok-abc123", host="192.168.1.50",
                              player_vod="5002", player_series="5002",
                              groups_json='["Sports"]')
        s.add(prof)
        await s.commit()
        prof2 = (await s.execute(select(Enigma2Profile))).scalars().first()
        u = (await s.execute(select(User).where(User.name == "alice"))).scalar_one()
        prof2.user_id = u.id
        await s.commit()


async def test_export_areas_section_shape():
    await _seed_area_and_enigma2()
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "areas"})
        assert r.status_code == 200, r.text
        data = r.json()
    assert "users" not in data                       # standalone section
    a = data["areas"][0]
    assert a["name"] == "Phone" and a["notes"] == "handset"
    assert a["ffmpeg_template_vod"] == "E2 tpl"      # template by NAME
    assert "ffmpeg_template_vod_id" not in a
    ex = data["area_item_templates"][0]
    assert ex["area"] == "Phone" and ex["kind"] == "live"
    assert ex["ffmpeg_template"] == "E2 tpl"


async def test_export_enigma2_section_shape():
    await _seed_area_and_enigma2()
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "enigma2"})
        assert r.status_code == 200, r.text
        data = r.json()
    p = data["enigma2_profiles"][0]
    assert p["name"] == "Vu+ Duo2"
    assert p["user"] == "alice"                      # user by NAME
    assert "user_id" not in p
    assert p["token"] == "tok-abc123"                # the box's install URL
    assert p["groups_json"] == ["Sports"]
    assert p["host"] == "192.168.1.50"
    # runtime state is machine state, not backup state
    for key in ("last_build_at", "last_push_at", "bouquet_count",
                "service_count", "last_push_result"):
        assert key not in p


async def test_export_all_includes_enigma2():
    await _seed_area_and_enigma2()
    async with _client() as c:
        r = await c.get("/api/export", params={"section": "all"})
        assert r.status_code == 200, r.text
        data = r.json()
    assert "enigma2_profiles" in data
    assert "areas" in data


async def test_import_restores_areas_and_enigma2_onto_fresh_id_space():
    await _seed_area_and_enigma2()
    backup = await _export_all()
    # a fresh install: drop everything the restore should bring back
    async with SessionLocal() as s:
        for m in (Enigma2Profile, AreaItemTemplate, Area, User,
                  FFmpegTemplate, Portal):
            for row in (await s.execute(select(m))).scalars().all():
                await s.delete(row)
        await s.commit()

    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": backup})
        assert r.status_code == 200, r.text
        applied = r.json()
    assert not applied["skipped"]

    async with SessionLocal() as s:
        tpl = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == "E2 tpl"))).scalar_one()
        area = (await s.execute(select(Area).where(Area.name == "Phone"))).scalar_one()
        assert area.ffmpeg_template_vod_id == tpl.id   # re-bound by name
        ex = (await s.execute(select(AreaItemTemplate))).scalar_one()
        assert ex.area_id == area.id
        assert ex.ffmpeg_template_id == tpl.id
        alice = (await s.execute(select(User).where(User.name == "alice"))).scalar_one()
        prof = (await s.execute(select(Enigma2Profile).where(
            Enigma2Profile.name == "Vu+ Duo2"))).scalar_one()
        assert prof.user_id == alice.id                # re-bound by name
        assert prof.token == "tok-abc123"              # install URL survives
        assert prof.host == "192.168.1.50"
        import json as _json
        assert _json.loads(prof.groups_json) == ["Sports"]

    # a second import merges: the profile is recognised, not duplicated
    async with _client() as c:
        r = await c.post("/api/import", json={"mode": "merge", "data": backup})
        assert r.status_code == 200, r.text
        applied = r.json()
    assert any(x.startswith("enigma2:Vu+ Duo2") for x in applied["skipped"])
    assert any(x.startswith("area:Phone") for x in applied["skipped"])
    async with SessionLocal() as s:
        assert len((await s.execute(select(Enigma2Profile))).scalars().all()) == 1
