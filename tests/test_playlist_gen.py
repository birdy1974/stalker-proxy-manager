"""
Playlist generation: per-user output and query cost.

Two reported problems live here:

  * "loading playlist in vlc takes really long" - `build_m3u` ran one query per
    series (its seasons), one per season (its episodes) and one per local file,
    so a few hundred series meant a thousand Postgres round trips before VLC
    could show a single channel.
  * "final output playlist per user should only include the enabled
    genres/items" - pinned here so the group whitelist and the enabled flags
    cannot regress.
"""

from __future__ import annotations

import json

from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import (LocalFile, LocalPlaylist, LocalSource, Portal, SerieEpisode,
                        SeriePlaylist, SeriePlaylistSeason, SerieSeason, SerieSource,
                        User, VodPlaylist, VodSource, LivePlaylist)
from app.services.playlist_gen import build_m3u

BASE = "http://proxy.local"


async def _seed(n_series: int, *, seasons: int = 3, episodes: int = 4) -> User:
    async with SessionLocal() as s:
        portal = Portal(name="p", base_url="http://portal.invalid")
        s.add(portal)
        await s.flush()

        for i in range(n_series):
            src = SerieSource(portal_id=portal.id, portal_item_id=str(i),
                              original_name=f"Serie {i}")
            s.add(src)
            await s.flush()
            sp = SeriePlaylist(serie_source_id=src.id, custom_name=f"Serie {i}",
                               group_name="Drama" if i % 2 == 0 else "Kids", order=i)
            s.add(sp)
            await s.flush()
            for sn in range(1, seasons + 1):
                season = SerieSeason(serie_source_id=src.id, season_number=sn)
                s.add(season)
                await s.flush()
                # season 3 of every series is switched OFF in the playlist
                s.add(SeriePlaylistSeason(serie_playlist_id=sp.id, serie_season_id=season.id,
                                          enabled=(sn != 3)))
                for ep in range(1, episodes + 1):
                    s.add(SerieEpisode(serie_season_id=season.id, episode_number=ep))

        vs = VodSource(portal_id=portal.id, portal_item_id="v1",
                       original_name="Man of War (2026) [1080p]")
        s.add(vs)
        await s.flush()
        s.add(VodPlaylist(vod_source_id=vs.id, custom_name="Man of War (2026) [1080p]",
                          group_name="Action"))
        s.add(VodPlaylist(vod_source_id=vs.id, custom_name="Hidden Movie",
                          group_name="Action", enabled=False))

        s.add(LivePlaylist(custom_name="192TV 8K+ UHD", group_name="NL", number=192))
        s.add(LivePlaylist(custom_name="Disabled Channel", group_name="NL", enabled=False))

        from sqlalchemy import select as _sel
        ls = (await s.execute(_sel(LocalSource).where(
            LocalSource.directory == "/media"))).scalars().first()
        if ls is None:
            ls = LocalSource(directory="/media")
            s.add(ls)
            await s.flush()
        lf = (await s.execute(_sel(LocalFile).where(
            LocalFile.local_source_id == ls.id,
            LocalFile.relative_path == "a/b.mp4"))).scalars().first()
        if lf is None:
            lf = LocalFile(local_source_id=ls.id, relative_path="a/b.mp4", filename="b.mp4")
            s.add(lf)
            await s.flush()
            s.add(LocalPlaylist(local_file_id=lf.id, custom_name="Home video",
                                group_name="vod-local"))

        from sqlalchemy import select
        user = (await s.execute(select(User).where(User.name == "user1"))).scalar_one_or_none()
        if user is None:
            user = User(name="user1", password="pw", m3u_enabled=True, xtream_enabled=True,
                        groups_json=json.dumps({"live": [], "vod": [], "series": [], "local": []}))
            s.add(user)
        await s.commit()
        return user


def _count_queries() -> tuple[list[str], object]:
    """Attach a counter to the engine; returns (list, remove_fn)."""
    seen: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _hook(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    def remove():
        event.remove(engine.sync_engine, "before_cursor_execute", _hook)

    return seen, remove


async def test_build_m3u_query_count_does_not_grow_with_the_library():
    await _seed(20)
    async with SessionLocal() as s:
        small_user = (await s.get(User, 1))
    seen, remove = _count_queries()
    try:
        text_small = await build_m3u(BASE, small_user)
    finally:
        remove()
    small_count = len(seen)

    await _seed(40)                     # triple the series count
    async with SessionLocal() as s:
        user = (await s.get(User, 1))
    seen, remove = _count_queries()
    try:
        await build_m3u(BASE, user)
    finally:
        remove()

    assert small_count <= 12, f"playlist request issued {small_count} queries"
    # 180 series with 3 seasons each: the old code needed ~700 round trips.
    assert len(seen) <= 12, (
        f"query count grew with the library ({len(seen)} queries) - this is what "
        f"made VLC wait forever for the playlist")
    assert text_small.count("/play/episode/") > 0


async def test_playlist_only_contains_enabled_items_and_the_users_genres():
    await _seed(6)
    async with SessionLocal() as s:
        user = await s.get(User, 1)
    text = await build_m3u(BASE, user)

    assert "Man of War (2026) [1080p]" in text
    assert "192TV 8K+ UHD" in text
    assert "Home video" in text
    # disabled items must never reach the output
    assert "Hidden Movie" not in text
    assert "Disabled Channel" not in text
    # season 3 is switched off for every series
    assert "S01E01" in text and "S02E01" in text
    assert "S03E01" not in text

    # now restrict the user to one genre per type
    user.groups_json = json.dumps({"live": ["NL"], "vod": ["Action"],
                                   "series": ["Kids"], "local": ["Not my stuff"]})
    async with SessionLocal() as s:
        s.add(user)
        await s.commit()
        user = await s.get(User, 1)
    restricted = await build_m3u(BASE, user)

    assert "192TV 8K+ UHD" in restricted, "allowed live group must stay"
    assert "Man of War (2026) [1080p]" in restricted, "allowed vod group must stay"
    assert "Serie 1 " in restricted, "allowed series group must stay"
    assert "Serie 0 " not in restricted, "Drama is not in the user's whitelist"
    assert "Home video" not in restricted, "'vod-local' is not in the user's local whitelist"


async def test_empty_whitelist_means_all_groups_allowed():
    """An empty list per type = 'everything', not 'nothing' (documented model)."""
    await _seed(2)
    async with SessionLocal() as s:
        user = await s.get(User, 1)
    text = await build_m3u(BASE, user)
    assert "Serie 0 " in text and "Serie 1 " in text
    assert "Home video" in text


async def test_full_titles_reach_the_playlist_and_carry_tvg_name():
    """
    "VOD title not complete in playlist": the m3u always carried the whole
    `custom_name` after the comma, but VOD/series/local entries had no
    `tvg-name`, and plenty of players prefer that attribute and otherwise fall
    back to the URL. Both halves are pinned here.
    """
    long_title = "Man of War (2026) [1080p] Extended Director's Cut, uncut"
    await _seed(0)                                   # gives us the user row
    async with SessionLocal() as s:
        from app.models import Portal, VodPlaylist, VodSource
        portal = Portal(name="t", base_url="http://p.invalid")
        s.add(portal); await s.flush()
        src = VodSource(portal_id=portal.id, portal_item_id="long", original_name=long_title)
        s.add(src); await s.flush()
        s.add(VodPlaylist(vod_source_id=src.id, custom_name=long_title, group_name="Action"))
        await s.commit()
        user = await s.get(User, 1)

    text = await build_m3u(BASE, user)
    lines = [ln for ln in text.splitlines() if long_title in ln]
    assert lines, "the full title is missing from the playlist"
    extinf = [ln for ln in lines if ln.startswith("#EXTINF")][0]
    assert extinf.endswith("," + long_title), f"title truncated after the comma: {extinf!r}"
    assert f'tvg-name="{long_title}"' in extinf, f"no tvg-name: {extinf!r}"


async def test_year_only_custom_name_uses_full_source_title():
    """Portal `name` is often just the year; `o_name` holds '**Man of War - 2026'."""
    full = "**Man of War - 2026"
    await _seed(0)
    async with SessionLocal() as s:
        portal = Portal(name="t2", base_url="http://p2.invalid")
        s.add(portal); await s.flush()
        src = VodSource(portal_id=portal.id, portal_item_id="yearonly", original_name=full)
        s.add(src); await s.flush()
        s.add(VodPlaylist(vod_source_id=src.id, custom_name="2026", group_name="Action"))
        await s.commit()
        user = await s.get(User, 1)
    text = await build_m3u(BASE, user)
    # The words survive; ASCII ' - ' is rewritten so VLC does not split the title.
    extinf = [ln for ln in text.splitlines()
              if ln.startswith("#EXTINF") and "**Man of War" in ln]
    assert extinf, text
    title = extinf[0].rsplit(",", 1)[-1]
    assert "Man of War" in title and "2026" in title
    assert " - " not in title
    assert "\u2013" in title
    assert not any(ln.endswith(",2026") for ln in text.splitlines() if ln.startswith("#EXTINF"))


async def test_minus_in_title_is_kept_whole_for_vlc():
    """VLC 3.0 parseEXTINF splits 'Artist - Title' so 'Foo - Bar' became just 'Bar'."""
    from app.services.titles import m3u_display_title
    raw = "Radio Paloma - 100% Deutscher Schlager!"
    await _seed(0)
    async with SessionLocal() as s:
        s.add(LivePlaylist(custom_name=raw, group_name="NL", number=11))
        await s.commit()
        user = await s.get(User, 1)
    text = await build_m3u(BASE, user)
    safe = m3u_display_title(raw)
    extinf = [ln for ln in text.splitlines() if ln.startswith("#EXTINF") and "Radio Paloma" in ln]
    assert extinf, text
    line = extinf[0]
    title = line.rsplit(",", 1)[-1]
    assert title == safe
    assert "Radio Paloma" in title and "Schlager" in title
    assert " - " not in title
    assert f'tvg-name="{safe}"' in line


async def test_m3u_does_not_point_vlc_at_epg():
    """VLC waits on url-tvg until /epg.xml returns or times out (~30s)."""
    await _seed(1)
    async with SessionLocal() as s:
        user = await s.get(User, 1)
    text = await build_m3u(BASE, user)
    header = text.splitlines()[0]
    assert header == "#EXTM3U"
    assert "url-tvg" not in text
    assert "x-tvg-url" not in text
    assert "/epg.xml" not in text


async def test_every_entry_has_a_matching_playable_url():
    await _seed(3)
    async with SessionLocal() as s:
        user = await s.get(User, 1)
    lines = (await build_m3u(BASE, user)).splitlines()

    extinf = [i for i, ln in enumerate(lines) if ln.startswith("#EXTINF")]
    urls = [ln for ln in lines if ln.startswith(BASE + "/play/")]
    assert len(extinf) == len(urls), "EXTINF/URL pairs must line up 1:1"
    for i in extinf:
        # VLC-specific options belong between EXTINF and that entry's URL.
        url_i = i + 2 if lines[i + 1].startswith("#EXTVLCOPT:") else i + 1
        assert lines[url_i].startswith(BASE + "/play/"), f"orphan EXTINF at line {i}"
    assert all("u=user1&p=pw" in u for u in urls), "every url must carry the user's credentials"


async def test_playlist_endpoint_returns_parseable_m3u_for_vlc():
    """
    Regression: `_playlist_revision` used a genexp of awaits, so every
    /playlist.m3u request raised TypeError and answered 500 - VLC then says
    it cannot load the playlist. Also pin the Content-Type / Disposition that
    make VLC treat the body as a playlist rather than a download.
    """
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    await _seed(2)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/playlist.m3u?u=user1&p=pw")
        assert r.status_code == 200, r.text
        body = r.text
        assert body.startswith("#EXTM3U"), body[:80]
        assert "/play/live/" in body or "/play/vod/" in body or "/play/episode/" in body
        ctype = (r.headers.get("content-type") or "").lower()
        assert "mpegurl" in ctype or "m3u" in ctype, ctype
        # Must NOT force a file download - VLC opens the URL as a network stream.
        disp = (r.headers.get("content-disposition") or "").lower()
        assert "attachment" not in disp, disp
        # Newlines inside a channel name must not split the EXTINF line.
        for ln in body.splitlines():
            if ln.startswith("#EXTINF"):
                assert "\n" not in ln
                assert ln.count(",") >= 1

        r2 = await c.get("/get.php?username=user1&password=pw&type=m3u_plus")
        assert r2.status_code == 200, r2.text
        assert r2.text.startswith("#EXTM3U")


async def test_newlines_in_titles_do_not_break_m3u_lines():
    """A portal name with an embedded newline used to split the EXTINF line
    and leave VLC with an orphan URL it cannot pair to a title."""
    await _seed(0)
    async with SessionLocal() as s:
        s.add(LivePlaylist(custom_name="Broken\nChannel", group_name="NL", number=7))
        await s.commit()
        user = await s.get(User, 1)
    text = await build_m3u(BASE, user)
    lines = text.splitlines()
    extinf = [ln for ln in lines if ln.startswith("#EXTINF") and "Broken" in ln]
    assert extinf, text
    assert "Broken Channel" in extinf[0]
    assert "\n" not in extinf[0]
    idx = lines.index(extinf[0])
    assert lines[idx + 1].startswith(BASE + "/play/live/")