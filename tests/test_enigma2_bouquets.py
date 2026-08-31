"""
Enigma2 bouquet generation (E2).

The receiver is dumb on purpose: it only ever plays URLs this proxy already
serves. Everything that decides HOW those URLs are written - which player the
box uses (the leading number of the service reference), which container the URL
asks for, how the catalogue is split into bouquets - lives here, so these tests
are the specification of the file format.

The two rules that break a real box if we get them wrong, and are therefore
pinned hardest:

  * a service reference is split on ':', so URL colons must be `%3a` and the
    display name may not contain one at all;
  * `bouquets.tv` is the user's own file (it lists their satellite bouquets):
    merging must be idempotent and must never drop a line that is not ours.
"""

from __future__ import annotations

import io
import tarfile

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Enigma2Profile, LivePlaylist, Portal, SerieEpisode, SeriePlaylist,
    SeriePlaylistSeason, SerieSeason, SerieSource, User, VodPlaylist, VodSource,
)
from app.services import enigma2_bouquets as e2

BASE = "http://testserver"
NAS = "http://nas:8880"


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def test_service_reference_escapes_the_url_and_cleans_the_name():
    lines = e2.service_line("5002", 42, f"{NAS}/play/vod/42.mkv?u=box&p=pw",
                            "Alien: Resurrection")
    ref = lines[0]
    assert ref.startswith("#SERVICE 5002:0:1:2A:0:0:0:0:0:0:")
    # every colon of the URL is escaped, the query string is untouched
    assert "http%3a//nas%3a8880/play/vod/42.mkv?u=box&p=pw" in ref
    # ...and the name that follows carries no colon of its own
    name = ref.split(":")[-1]
    assert name == "Alien - Resurrection"
    assert lines[1] == "#DESCRIPTION Alien - Resurrection"


def test_the_sid_field_is_the_stable_playlist_id():
    """Regenerating a bouquet must not renumber services: the box's own
    favourites, the picon names and (later) the EPG channel map all key on the
    reference string."""
    a = e2.service_line("4097", 4095, f"{NAS}/play/live/4095.ts", "X")[0]
    assert ":FFF:" in a


def test_marker_lines_are_not_playable():
    m = e2.marker_line("Sport")
    assert m[0] == "#SERVICE 1:64:0:0:0:0:0:0:0:0::Sport"


def test_stream_url_picks_the_alias_and_the_mode():
    u = User(name="box", password="pw")
    assert e2.stream_url(NAS, "vod", 7, "mkv", u) == \
        f"{NAS}/play/vod/7.mkv?u=box&p=pw"
    assert e2.stream_url(NAS, "live", 7, "ts", u, "redirect") == \
        f"{NAS}/play/live/7.ts?u=box&p=pw&mode=redirect"
    # no user -> no credentials (the endpoint will 403, and the preview says so)
    assert e2.stream_url(NAS, "live", 7, "ts", None) == f"{NAS}/play/live/7.ts"


def test_bouquets_tv_merge_is_idempotent_and_keeps_foreign_lines():
    existing = "\n".join([
        "#NAME Bouquets (TV)",
        '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.favourites.tv" ORDER BY bouquet',
        '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.spm_live_old.tv" ORDER BY bouquet',
        '#SERVICE 1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.sat_de.tv" ORDER BY bouquet',
    ])
    out = e2.merge_bouquets_tv(existing, ["userbouquet.spm_live_news.tv"], "spm")
    assert out.splitlines()[0] == "#NAME Bouquets (TV)"
    assert "userbouquet.favourites.tv" in out and "userbouquet.sat_de.tv" in out
    assert "userbouquet.spm_live_old.tv" not in out          # stale ours: gone
    assert "userbouquet.spm_live_news.tv" in out
    # running it again changes nothing
    assert e2.merge_bouquets_tv(out, ["userbouquet.spm_live_news.tv"], "spm") == out


def test_merge_survives_a_box_without_bouquets_tv():
    out = e2.merge_bouquets_tv("", ["userbouquet.spm_live.tv"], "spm")
    assert out.startswith("#NAME Bouquets (TV)")
    assert out.count("FROM BOUQUET") == 1


# --------------------------------------------------------------------------- #
# rendering a catalogue
# --------------------------------------------------------------------------- #
async def _catalogue() -> User:
    """A small library: 3 live channels in 2 groups, 2 movies, 1 series with
    two seasons."""
    async with SessionLocal() as s:
        user = User(name="box", password="pw", enabled=True, m3u_enabled=True)
        s.add(user)
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        s.add_all([
            LivePlaylist(custom_name="News One", group_name="News", enabled=True, order=1),
            LivePlaylist(custom_name="News Two", group_name="News", enabled=True, order=2),
            LivePlaylist(custom_name="Sport One", group_name="Sport", enabled=True, order=3),
        ])
        for title in ("Zulu", "Alpha"):
            src = VodSource(portal_id=portal.id, portal_item_id=title,
                            original_name=title)
            s.add(src)
            await s.flush()
            s.add(VodPlaylist(vod_source_id=src.id, custom_name=title,
                              group_name="Action", enabled=True, order=1))
        serie_src = SerieSource(portal_id=portal.id, portal_item_id="1",
                                original_name="Show")
        s.add(serie_src)
        await s.flush()
        sp = SeriePlaylist(serie_source_id=serie_src.id, custom_name="Show",
                           group_name="Drama", enabled=True, order=1)
        s.add(sp)
        await s.flush()
        for season_no in (1, 2):
            season = SerieSeason(serie_source_id=serie_src.id, season_number=season_no,
                                 name=f"Season {season_no}")
            s.add(season)
            await s.flush()
            s.add(SeriePlaylistSeason(serie_playlist_id=sp.id,
                                      serie_season_id=season.id, enabled=True))
            for ep in (1, 2):
                s.add(SerieEpisode(serie_season_id=season.id, episode_number=ep,
                                   name=f"E{ep}", cmd="ffmpeg http://x"))
        await s.commit()
        return user


def _profile(user: User, **kw) -> Enigma2Profile:
    base = dict(name="Duo2", token="tok", user_id=user.id, bouquet_prefix="spm",
                player_live="4097", player_vod="5002", player_series="5002",
                container_live="ts", container_vod="mkv", container_series="mkv",
                delivery_mode="template", include_live=True, include_vod=True,
                include_series=True, include_local=False, layout="group_markers",
                max_entries=1500)
    return Enigma2Profile(**(base | kw))


async def test_group_markers_layout_is_one_bouquet_per_group():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user), NAS)
    names = {f.name for f in bundle.files}
    assert "userbouquet.spm_live_news.tv" in names
    assert "userbouquet.spm_live_sport.tv" in names
    assert "userbouquet.spm_vod_action.tv" in names
    assert "userbouquet.spm_series_drama.tv" in names
    assert bundle.services == 3 + 2 + 4          # live + vod + episodes


async def test_live_and_vod_get_their_configured_player_and_container():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user), NAS)
    live = next(f for f in bundle.files if "live_news" in f.name).text
    vod = next(f for f in bundle.files if "vod_action" in f.name).text
    assert "#SERVICE 4097:" in live and "/play/live/" in live and ".ts?u=box&p=pw" in live
    # the whole point: VOD is exteplayer3 + Matroska, so text subtitles exist
    assert "#SERVICE 5002:" in vod and ".mkv?u=box&p=pw" in vod


async def test_vod_is_sorted_with_letter_markers():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user), NAS)
    text = next(f for f in bundle.files if "vod_action" in f.name).text
    assert text.index("Alpha") < text.index("Zulu")          # sorted by title
    assert "#SERVICE 1:64:0:0:0:0:0:0:0:0::A" in text        # letter markers


async def test_series_markers_name_the_show_and_every_season():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user), NAS)
    text = next(f for f in bundle.files if "series_drama" in f.name).text
    assert "::Show" in text
    assert "::Show - Season 1" in text and "::Show - Season 2" in text
    assert "Show S01E01" in text and "Show S02E02" in text
    assert text.count("#SERVICE 5002:") == 4                 # four episodes


async def test_per_series_layout_makes_one_bouquet_per_show():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user, layout="per_series"), NAS)
    assert any(f.name == "userbouquet.spm_series_show.tv" for f in bundle.files)


async def test_flat_layout_collapses_everything_per_kind():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user, layout="flat"), NAS)
    names = {f.name for f in bundle.files}
    assert names == {"userbouquet.spm_live.tv", "userbouquet.spm_vod.tv",
                     "userbouquet.spm_series.tv"}


async def test_big_bouquets_are_split_into_numbered_parts():
    """Enigma2 redraws the whole list on every zap, so an unbounded bouquet is
    a slideshow. Splitting is what keeps a 10 000-title VOD library usable."""
    async with SessionLocal() as s:
        user = User(name="box2", password="pw", enabled=True)
        s.add(user)
        portal = Portal(name="p2", base_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        for i in range(7):
            src = VodSource(portal_id=portal.id, portal_item_id=f"m{i}",
                            original_name=f"Movie {i:02d}")
            s.add(src)
            await s.flush()
            s.add(VodPlaylist(vod_source_id=src.id, custom_name=f"Movie {i:02d}",
                              group_name="Action", enabled=True, order=i))
        await s.commit()
    bundle = await e2.build_bundle(_profile(user, max_entries=50, layout="flat"), NAS)
    # max_entries has a floor of 50, so force the split through the writer
    w = e2._Writer("spm", "vod", "SPM - VOD", 50)
    for i in range(120):
        w.service("5002", i, f"{NAS}/play/vod/{i}.mkv", f"M{i}")
    files = w.done()
    assert [f.name for f in files] == ["userbouquet.spm_vod.tv",
                                       "userbouquet.spm_vod_2.tv",
                                       "userbouquet.spm_vod_3.tv"]
    assert [f.services for f in files] == [50, 50, 20]
    assert bundle.services == 7


async def test_group_filters_of_user_and_profile_both_apply():
    user = await _catalogue()
    async with SessionLocal() as s:
        u = await s.get(User, user.id)
        u.groups_json = '{"live": ["News"], "vod": [], "series": [], "local": []}'
        await s.commit()
    bundle = await e2.build_bundle(_profile(user), NAS)
    live_files = [f for f in bundle.files if "_live_" in f.name]
    assert [f.name for f in live_files] == ["userbouquet.spm_live_news.tv"]
    # the profile can narrow it further, never widen it
    bundle2 = await e2.build_bundle(
        _profile(user, groups_json='{"live": ["Sport"]}'), NAS)
    assert not [f for f in bundle2.files if "_live_" in f.name]


async def test_content_types_can_be_switched_off():
    user = await _catalogue()
    bundle = await e2.build_bundle(
        _profile(user, include_vod=False, include_series=False), NAS)
    assert all("_live_" in f.name for f in bundle.files)


async def test_warnings_flag_the_combination_that_silently_loses_subtitles():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user, player_vod="4097"), NAS)
    assert any("exteplayer3" in w for w in bundle.warnings)
    assert not any("exteplayer3" in w for w in
                   (await e2.build_bundle(_profile(user), NAS)).warnings)


async def test_delivery_mode_overrides_the_per_item_template():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user, delivery_mode="redirect"), NAS)
    text = next(f for f in bundle.files if "live_news" in f.name).text
    assert "&mode=redirect" in text


async def test_tarball_carries_the_files_and_the_index_lines():
    user = await _catalogue()
    bundle = await e2.build_bundle(_profile(user), NAS)
    blob = e2.tarball_bytes(bundle, "spm")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = tar.getnames()
        add = tar.extractfile(e2.BOUQUET_ADD_FILE).read().decode()
    assert "userbouquet.spm_live_news.tv" in members
    assert e2.BOUQUET_ADD_FILE in members
    assert 'FROM BOUQUET "userbouquet.spm_live_news.tv" ORDER BY bouquet' in add
    # bouquets.tv itself is NOT shipped: only the box knows its own bouquets
    assert "bouquets.tv" not in members


def test_installer_merges_instead_of_overwriting_bouquets_tv():
    sh = e2.install_script(NAS, "tok", "spm")
    assert "bouquets.tv.spm-backup" in sh                      # backup first
    assert "grep -v 'userbouquet\\.spm_'" in sh                # keep foreign lines
    assert "servicelistreload?mode=2" in sh                    # reload at the end
    assert f"{NAS}/enigma2/tok/bouquets.tar.gz" in sh


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
async def test_profile_crud_preview_and_public_pull():
    user = await _catalogue()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        meta = (await c.get("/api/enigma2/meta")).json()
        assert "5002" in meta["players"] and "box" in [u["name"] for u in meta["users"]]

        r = await c.post("/api/enigma2/profiles",
                         json={"name": "Wohnzimmer", "user_id": user.id})
        item = r.json()["item"]
        pid, token = item["id"], item["token"]
        assert item["player_vod"] == "5002" and item["container_vod"] == "mkv"

        # secrets never travel back to the browser
        await c.put(f"/api/enigma2/profiles/{pid}", json={"password": "boxpw"})
        again = (await c.get("/api/enigma2/profiles")).json()["items"][0]
        assert again["password"] == "" and again["has_password"] is True
        # ...and an empty field keeps the stored one
        await c.put(f"/api/enigma2/profiles/{pid}", json={"host": "192.168.1.50"})
        async with SessionLocal() as s:
            assert (await s.get(Enigma2Profile, pid)).password == "boxpw"

        # invalid enum values are refused instead of writing a broken bouquet
        assert (await c.put(f"/api/enigma2/profiles/{pid}",
                            json={"player_vod": "9999"})).status_code == 400

        pv = (await c.post(f"/api/enigma2/profiles/{pid}/preview")).json()
        assert pv["summary"]["services"] == 9
        assert any(f["name"] == "userbouquet.spm_live_news.tv" for f in pv["files"])
        assert pv["install_url"].endswith(f"/enigma2/{token}/install.sh")

        # the box pulls with its token, no admin session involved
        tar_r = await c.get(f"/enigma2/{token}/bouquets.tar.gz")
        assert tar_r.status_code == 200
        with tarfile.open(fileobj=io.BytesIO(tar_r.content), mode="r:gz") as tar:
            assert any(n.startswith("userbouquet.spm_") for n in tar.getnames())
        sh = await c.get(f"/enigma2/{token}/install.sh")
        assert sh.status_code == 200 and sh.text.startswith("#!/bin/sh")
        assert (await c.get("/enigma2/nope/install.sh")).status_code == 404

        # rotating the token invalidates the old URL
        new_token = (await c.post(f"/api/enigma2/profiles/{pid}/token")
                     ).json()["item"]["token"]
        assert new_token != token
        assert (await c.get(f"/enigma2/{token}/bouquets.tar.gz")).status_code == 404

        # build status is written back for the list view
        async with SessionLocal() as s:
            row = await s.get(Enigma2Profile, pid)
            assert row.service_count == 9 and row.bouquet_count >= 4

        assert (await c.delete(f"/api/enigma2/profiles/{pid}")).status_code == 200
        assert (await c.get("/api/enigma2/profiles")).json()["items"] == []


async def test_disabled_profile_stops_serving_the_box():
    user = await _catalogue()
    async with SessionLocal() as s:
        p = _profile(user, name="Off", token="offtok", enabled=False)
        s.add(p)
        await s.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        assert (await c.get("/enigma2/offtok/bouquets.tar.gz")).status_code == 404


async def test_the_gui_page_renders():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get("/enigma2")
        assert r.status_code == 200 and "Receivers" in r.text
