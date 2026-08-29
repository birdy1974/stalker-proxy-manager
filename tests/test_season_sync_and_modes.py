"""
Season link reconciliation + stream output mode.

These pin two reported bugs:
  * "there is no possibility to fetch and select seasons for series" - playlist
    series whose seasons were fetched *after* they were added got no
    serie_playlist_seasons rows, so they contributed no episodes to any
    playlist. The only thing that created those rows was a read-repair inside
    GET /api/playlist/series, i.e. opening the Series tab in the GUI.
  * the two "stream direct" options - proxy through ffmpeg (default) vs.
    redirect straight to the portal URL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Portal,
    SerieEpisode,
    SeriePlaylist,
    SeriePlaylistSeason,
    SeriePlaylistSource,
    SerieSeason,
    SerieSource,
    User,
)
from app.services.playlist_gen import build_m3u
from app.services.playlist_sync import sync_season_links

BASE = "http://testserver"


async def sync_season_links_using_new_session() -> int:
    async with SessionLocal() as s:
        return await sync_season_links(s)


async def _series_without_season_links() -> tuple[int, int]:
    """A playlist-series whose season rows exist but are NOT linked. Returns (pid, sid)."""
    async with SessionLocal() as s:
        portal = Portal(name="s", base_url="http://s.invalid")
        s.add(portal)
        await s.flush()
        src = SerieSource(portal_id=portal.id, portal_item_id="s1", original_name="Bandwidth")
        s.add(src)
        await s.flush()
        season = SerieSeason(serie_source_id=src.id, season_number=1, name="S1", enabled=True)
        s.add(season)
        await s.flush()
        s.add(SerieEpisode(serie_season_id=season.id, episode_number=1, name="E1", cmd="c"))
        s.add(SerieEpisode(serie_season_id=season.id, episode_number=2, name="E2", cmd="c"))
        pl = SeriePlaylist(serie_source_id=src.id, custom_name="Bandwidth",
                           group_name="SciFi", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(SeriePlaylistSource(serie_playlist_id=pl.id, serie_source_id=src.id, priority=0))
        await s.commit()
        return pl.id, season.id


async def test_season_links_are_created_when_missing():
    pid, season_id = await _series_without_season_links()
    async with SessionLocal() as s:
        assert (await s.execute(select(SeriePlaylistSeason))).scalars().all() == []

    added = await sync_season_links_using_new_session()
    assert added == 1

    async with SessionLocal() as s:
        rows = (await s.execute(select(SeriePlaylistSeason))).scalars().all()
        assert len(rows) == 1
        assert rows[0].serie_playlist_id == pid
        assert rows[0].serie_season_id == season_id
        assert rows[0].enabled is True       # follows the source season


async def test_season_link_sync_is_idempotent():
    await _series_without_season_links()
    assert await sync_season_links_using_new_session() == 1
    assert await sync_season_links_using_new_session() == 0
    async with SessionLocal() as s:
        from sqlalchemy import select
        assert len((await s.execute(select(SeriePlaylistSeason))).scalars().all()) == 1


async def test_unlinked_series_contribute_no_episodes_until_synced():
    """The bug, end to end: the playlist is silently missing the whole series."""
    await _series_without_season_links()
    async with SessionLocal() as s:
        user = User(name="u1", password="x")
        s.add(user)
        await s.commit()
        await s.refresh(user)

    before = await build_m3u(BASE, user)
    assert "/play/episode/" not in before, "episodes appeared without any season link"

    assert await sync_season_links_using_new_session() == 1
    after = await build_m3u(BASE, user)
    assert after.count("/play/episode/") == 2, "both episodes should now be in the playlist"


# ---------------------------------------------------------------------------
# stream output mode
# ---------------------------------------------------------------------------

async def test_wants_redirect_explicit_mode_param_wins():
    from app.routers.output import _wants_redirect

    assert await _wants_redirect("live", 1, "redirect") is True
    assert await _wants_redirect("live", 1, "proxy") is False
    # a bad value falls through to the per-channel template check; with no
    # item in the DB there is nothing to redirect
    assert await _wants_redirect("live", 1, "nonsense") is False
    assert await _wants_redirect("live", 999999, "") is False


async def test_redirect_mode_fails_loudly_when_no_source_resolves():
    """
    A redirect that cannot resolve must answer 502, not hang the client or
    fake a 200. Uses real credentials so the request actually reaches
    `_stream_response` -> `MANAGER.resolve` instead of dying at auth.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.models import SeriePlaylistSeason

    async with SessionLocal() as s:
        user = User(name="redirecter", password="pw", enabled=True, m3u_enabled=True)
        s.add(user)
        await s.flush()
        portal = Portal(name="dead", base_url="http://127.0.0.1:1/c/")   # no MAC, no route
        s.add(portal)
        await s.flush()
        src = SerieSource(portal_id=portal.id, portal_item_id="gone", original_name="Gone")
        s.add(src)
        await s.flush()
        season = SerieSeason(serie_source_id=src.id, season_number=1, enabled=True)
        s.add(season)
        await s.flush()
        s.add(SerieEpisode(serie_season_id=season.id, episode_number=1, name="E1", cmd="c"))
        pl = SeriePlaylist(serie_source_id=src.id, custom_name="Gone", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(SeriePlaylistSeason(serie_playlist_id=pl.id, serie_season_id=season.id,
                                  enabled=True))
        s.add(SeriePlaylistSource(serie_playlist_id=pl.id, serie_source_id=src.id, priority=0))
        ep_id = (await s.execute(select(SerieEpisode.id))).scalars().first()
        await s.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        # sanity: bad credentials still 403, so the 502 below is not an auth error
        bad = await c.get(f"/play/episode/{ep_id}.ts?u=redirecter&p=WRONG&mode=redirect")
        assert bad.status_code == 403

        r = await c.get(f"/play/episode/{ep_id}.ts?u=redirecter&p=pw&mode=redirect")
        assert r.status_code == 502, f"expected 502 from an unresolvable redirect, got {r.status_code}"
        assert "no source produced a link" in r.text


async def test_boot_hook_links_seasons_that_have_none():
    """
    Exercises the function that actually runs at startup (`app.main`
    `_heal_season_links`), not just the helper it calls - so an unhooked boot
    step cannot pass silently.
    """
    from app.main import _heal_season_links

    pid, season_id = await _series_without_season_links()
    async with SessionLocal() as s:
        assert (await s.execute(select(SeriePlaylistSeason).where(
            SeriePlaylistSeason.serie_playlist_id == pid))).scalars().all() == []

    await _heal_season_links()          # exactly what startup() calls

    async with SessionLocal() as s:
        rows = (await s.execute(select(SeriePlaylistSeason).where(
            SeriePlaylistSeason.serie_playlist_id == pid))).scalars().all()
        assert [r.serie_season_id for r in rows] == [season_id]


# ---------------------------------------------------------------------------
# the black-screen bug: a stream that produces nothing must not answer 200
# ---------------------------------------------------------------------------

async def test_guarded_stream_rejects_an_empty_body():
    """
    `200 OK` + `content-type: video/mp2t` + zero bytes is exactly what rendered
    as "player popup, black screen, nothing playing" - the browser has a valid
    response and no error to show. _guarded turns that into a 502 with a reason.
    """
    from fastapi import HTTPException

    from app.routers.output import _guarded

    async def empty():
        return
        yield b""                                  # pragma: no cover

    with pytest.raises(HTTPException) as ei:
        await _guarded(empty(), "test stream", "thing")
    assert ei.value.status_code == 502
    assert "produced no data" in ei.value.detail


async def test_guarded_stream_passes_a_working_one_through_untouched():
    from app.routers.output import _guarded

    async def good():
        for i in range(4):
            yield b"chunk%d" % i

    body = await _guarded(good(), "test stream", "thing")
    got = [c async for c in body]
    assert got == [b"chunk0", b"chunk1", b"chunk2", b"chunk3"]


async def test_guarded_stream_reports_a_pipe_that_raises():
    from fastapi import HTTPException

    from app.routers.output import _guarded

    async def broken():
        yield b""
        raise RuntimeError("ffmpeg died")

    with pytest.raises(HTTPException) as ei:
        await _guarded(broken(), "test stream", "thing")
    assert ei.value.status_code == 502
    assert "ffmpeg died" in ei.value.detail


async def test_player_libraries_are_served_locally_not_from_a_cdn():
    """
    The popup loads hls.js/mpegts.js. When they came from jsdelivr, a box that
    cannot reach the CDN left `window.mpegts` undefined, the player fell back to
    `video.src = <raw mpeg-ts>` (undecodable in any browser) and showed black.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        page = (await c.get("/dashboard")).text
        assert "cdn.jsdelivr.net/npm/mpegts" not in page
        assert "cdn.jsdelivr.net/npm/hls" not in page
        for path in ("/static/vendor/mpegts.js", "/static/vendor/hls.min.js"):
            r = await c.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert len(r.content) > 50_000, f"{path} looks truncated: {len(r.content)} bytes"
