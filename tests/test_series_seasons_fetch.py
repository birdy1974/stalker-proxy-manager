"""
Unit tests for series season and episode extraction improvements:
- Validates that generic VOD movie dumps (like Jet Pilot) are rejected by season validation.
- Validates that parent series metadata (e.g. series=[1..14]) extracts all 14 seasons.
- Validates multi-strategy season and episode queries.
"""

from __future__ import annotations

import json
import pytest
from app.portal.client import _is_valid_season_item, _is_valid_episode_item
from app.services.fetch_jobs import extract_seasons_from_meta, _season_number, _episode_number, _episode_season


def test_vod_movie_dump_is_rejected_as_season():
    # A generic VOD movie returned when a portal ignores movie_id
    movie_item = {
        "id": "9999",
        "name": "**┃EN┃Jet Pilot (1957)",
        "cmd": "ffmpeg http://cdn.invalid/vod/9999.mp4",
        "is_series": 0,
        "is_season": 0,
    }
    assert _is_valid_season_item(movie_item, "12345") is False


def test_valid_season_item_is_accepted():
    season_item1 = {
        "id": "12345:1",
        "series_id": "12345",
        "season_id": "1",
        "name": "Season 1",
        "is_season": 1,
    }
    assert _is_valid_season_item(season_item1, "12345") is True

    season_item2 = {
        "id": "27657",
        "name": "Season 2",
        "season_id": "2",
        "is_series": 1,
    }
    assert _is_valid_season_item(season_item2, "12345") is True


def test_extract_seasons_from_meta_list_of_ints():
    # Like Gold Rush with 14 seasons: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    meta = list(range(1, 15))
    seasons = extract_seasons_from_meta(meta)
    assert len(seasons) == 14
    assert seasons[0] == (1, "1", "Season 1")
    assert seasons[13] == (14, "14", "Season 14")


def test_extract_seasons_from_meta_json_string():
    meta = json.dumps(list(range(1, 15)))
    seasons = extract_seasons_from_meta(meta)
    assert len(seasons) == 14
    assert seasons[0] == (1, "1", "Season 1")
    assert seasons[13] == (14, "14", "Season 14")


def test_extract_seasons_from_meta_count_integer():
    seasons = extract_seasons_from_meta(14)
    assert len(seasons) == 14
    assert seasons[0] == (1, "1", "Season 1")
    assert seasons[13] == (14, "14", "Season 14")


def test_extract_seasons_from_meta_list_of_dicts():
    meta = [
        {"id": "s1", "season_id": "1", "name": "Season 1"},
        {"id": "s2", "season_id": "2", "name": "Season 2"},
    ]
    seasons = extract_seasons_from_meta(meta)
    assert len(seasons) == 2
    assert seasons[0] == (1, "1", "Season 1")
    assert seasons[1] == (2, "2", "Season 2")


def test_season_number_and_episode_parsing():
    assert _season_number({"name": "Season 14"}) == 14
    assert _season_number({"name": "S14"}) == 14
    assert _season_number({"season_id": "14"}) == 14
    assert _season_number({"season_number": 14}) == 14

    assert _episode_number({"series": [14, 5]}) == 5
    assert _episode_number({"series_number": "5"}) == 5
    assert _episode_number({"name": "S14E05 - Gold Miners"}) == 5

    assert _episode_season({"series": [14, 5]}) == 14
    assert _episode_season({"season_id": "14"}) == 14
    assert _episode_season({"name": "S14E05 - Gold Miners"}) == 14


def test_episode_number_prefers_explicit_episode_number_over_ambiguous_series_number():
    """Regression: many portals store season number in `series_number` and the
    actual episode number in `episode_number`. The old code checked `series_number`
    first, which caused all episodes to collapse into the same episode number
    (= the season number), leaving only one episode per season in the playlist.
    """
    # episode_number should always win over series_number
    assert _episode_number({"episode_number": "5", "series_number": "2"}) == 5
    assert _episode_number({"episode_number": "1", "series_number": "1"}) == 1
    assert _episode_number({"episode_number": "3", "series_number": "3"}) == 3


def test_series_number_rejected_when_it_matches_season_number():
    """When series_number equals season_number, it is most likely the season
    number (not the episode number) and must be ignored so the cursor fallback
    assigns sequential episode numbers instead."""
    # series_number == season_number → ambiguous, rejected
    assert _episode_number({"series_number": "2", "season_number": "2"}) is None
    assert _episode_number({"series_number": "1", "season_number": "1"}) is None
    assert _episode_number({"series_number": "3", "season_number": "3"}) is None

    # series_number != season_number → likely episode number, accepted
    assert _episode_number({"series_number": "5", "season_number": "2"}) == 5
    assert _episode_number({"series_number": "1", "season_number": "3"}) == 1

    # No season_number to compare → accept series_number (best we have)
    assert _episode_number({"series_number": "5"}) == 5


def test_episode_number_fallback_to_name_parsing():
    """When series_number validation fails, name parsing provides fallback."""
    # Bad series_number (matches season), but good names
    episodes = [
        {"series_number": "1", "season_number": "1", "name": "Episode 1"},
        {"series_number": "1", "season_number": "1", "name": "Episode 2"},
        {"series_number": "1", "season_number": "1", "name": "Episode 3"},
    ]
    results = [_episode_number(ep) for ep in episodes]
    assert results == [1, 2, 3], "Name parsing extracts episode numbers"

    # Bad series_number, generic names → cursor fallback will be used
    episodes = [
        {"series_number": "1", "season_number": "1", "name": "Pilot"},
        {"series_number": "1", "season_number": "1", "name": "Second Coming"},
    ]
    results = [_episode_number(ep) for ep in episodes]
    assert results == [None, None], "Returns None (cursor fallback assigns 1, 2, 3)"


# =========================================================================== #
# Classic-Stalker "regular series" (the IPTVnator port): the season object
# ITSELF carries `series=[1..N]` (its episode-number list) and one cmd for the
# whole season. Storing those objects as episodes is the Gold Rush bug:
# `series[1]` of [1, 2, ..., 20] is 2, so every season collapsed into one
# phantom "SxxE02" row - and the stored cmd (the season container) could not
# play without the `series=<n>` create_link parameter.
# =========================================================================== #
import asyncio

from app.portal.links import link_request_params, plan_for
from app.services.fetch_jobs import Job, _fetch_seasons_episodes, _season_container_episodes


def test_classic_season_container_is_recognized():
    container = {"id": "12345:13", "name": "Season 13",
                 "cmd": "/media/file_12345_13.mpg",
                 "series": list(range(1, 21))}
    assert _season_container_episodes(container) == list(range(1, 21))
    # portals with missing episodes keep the gap
    assert _season_container_episodes({"series": [1, 2, 4, 5]}) == [1, 2, 4, 5]


def test_explicit_episode_objects_are_not_containers():
    assert _season_container_episodes({"series": [1, 2, 3], "is_episode": 1}) == []
    assert _season_container_episodes({"series": [1, 2, 3], "series_number": "3"}) == []
    assert _season_container_episodes({"series": [3, 7]}) == []      # [season, episode] pair
    assert _season_container_episodes({"series": []}) == []
    assert _season_container_episodes({"series": [5, 2, 9]}) == []   # not an enumeration
    assert _season_container_episodes({"name": "Episode 5"}) == []


def test_episode_number_no_longer_reads_the_container_list():
    """series=[1..20] used to be read as a [season, episode] pair -> always 2."""
    assert _episode_number({"series": list(range(1, 21))}) is None
    assert _episode_number({"series": [14, 5]}) == 5                 # a real pair still works


def test_modern_season_objects_are_rejected_as_episodes():
    assert _is_valid_episode_item({"is_season": 1, "season_id": "3"}, "1") is False
    assert _is_valid_episode_item({"is_episode": 1, "id": "9"}, "1") is True


def test_create_link_params_carry_the_episode_selector():
    assert link_request_params(link_flags=None, force_ch_link_check=False, series=7)["series"] == "7"
    assert link_request_params(link_flags=None, force_ch_link_check=False, series=None)["series"] == "0"
    # the faithful-box booleans keep working
    assert link_request_params(link_flags=None, force_ch_link_check=False, series=True)["series"] == "1"


def test_plan_for_a_classic_episode_always_asks_and_carries_series():
    class Ep:  # what plan_for reads off a SerieEpisode row
        cmd = "ffmpeg http://cdn.example/media/file_12345_13.mpg"
        link_flags = ""
        series_param = True
        episode_number = 13

    class Mac:
        mac = "00:1A:79:AA:AA:01"
        force_ch_link_check = False

    plan = plan_for(Ep(), Mac())
    assert plan.policy.create_link, "a classic episode has NO static answer: its cmd is the season"
    assert plan.request_kwargs()["series"] == 13

    class Vod(Ep):
        series_param = False
    assert "series" not in plan_for(Vod(), Mac()).request_kwargs()


class _ClassicClient:
    """A classic panel: seasons carry the episode list; there is no episode call."""

    def __init__(self, seasons=(13, 14), eps_per_season=5):
        self.seasons = seasons
        self.eps = eps_per_season
        self.episode_calls = 0

    async def series_seasons(self, pid):
        return [{"id": f"{pid}:{sn}", "name": f"Season {sn}",
                 "cmd": f"/media/file_{pid}_{sn}.mpg",
                 "series": list(range(1, self.eps + 1))} for sn in self.seasons]

    async def series_episodes(self, pid, season_id):
        # a classic panel ignores season_id and answers with the season list
        # again; the fetch must not even need to ask
        self.episode_calls += 1
        return await self.series_seasons(pid)


async def test_gold_rush_seasons_expand_into_full_episode_lists():
    """End to end through _fetch_seasons_episodes: enabling S13+S14 must yield
    S13E01..E05 and S14E01..E05 - not one 'E02' per season 1..14."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Portal, SerieEpisode, SerieSeason, SerieSource

    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://test/mock/c/", enabled=True)
        s.add(p)
        await s.flush()
        src = SerieSource(portal_id=p.id, portal_item_id="12345",
                          original_name="Gold Rush", enabled=True,
                          raw_series="[1,2,3,4,5,6,7,8,9,10,11,12,13,14]")
        s.add(src)
        await s.commit()
        pid, sid = p.id, src.id

    job = Job(id="t", kind="fetch_seasons", portal_id=pid, series_ids=[sid])
    client = _ClassicClient(seasons=(13, 14), eps_per_season=5)
    await _fetch_seasons_episodes(job, client, pid, "p", series_ids=[sid])

    assert client.episode_calls == 0, "classic flavor: the episode list is IN the season object"
    async with SessionLocal() as s:
        seasons = (await s.execute(select(SerieSeason).where(
            SerieSeason.serie_source_id == sid).order_by(SerieSeason.season_number))).scalars().all()
        assert [x.season_number for x in seasons] == [13, 14]
        for season in seasons:
            eps = (await s.execute(select(SerieEpisode).where(
                SerieEpisode.serie_season_id == season.id)
                .order_by(SerieEpisode.episode_number))).scalars().all()
            assert [e.episode_number for e in eps] == [1, 2, 3, 4, 5]
            for e in eps:
                assert e.series_param is True
                assert e.cmd == f"/media/file_12345_{season.season_number}.mpg"
                assert e.name == f"Episode {e.episode_number}"


async def test_a_season_dump_answering_the_episode_call_is_expanded_not_stored():
    """Panels where series_seasons() fails but the 'episodes' call returns the
    season list again (the season_id parameter is ignored): the matching
    container must be expanded - previously each dump collapsed to one E02."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Portal, SerieEpisode, SerieSeason, SerieSource
    from app.portal.client import PortalError

    class DumpClient:
        async def series_seasons(self, pid):
            raise PortalError("no seasons endpoint")

        async def series_episodes(self, pid, season_id):
            # always the SAME season list, whatever season is asked for
            return [{"id": f"{pid}:{sn}", "name": f"Season {sn}",
                     "cmd": f"/media/file_{pid}_{sn}.mpg",
                     "series": [1, 2, 3]} for sn in (1, 2)]

    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://test/mock/c/", enabled=True)
        s.add(p)
        await s.flush()
        src = SerieSource(portal_id=p.id, portal_item_id="777",
                          original_name="Two Seasons", enabled=True,
                          raw_series="[1,2]")
        s.add(src)
        await s.commit()
        pid, sid = p.id, src.id

    job = Job(id="t2", kind="fetch_seasons", portal_id=pid, series_ids=[sid])
    await _fetch_seasons_episodes(job, DumpClient(), pid, "p", series_ids=[sid])

    async with SessionLocal() as s:
        seasons = (await s.execute(select(SerieSeason).where(
            SerieSeason.serie_source_id == sid).order_by(SerieSeason.season_number))).scalars().all()
        assert [x.season_number for x in seasons] == [1, 2]
        for season in seasons:
            eps = (await s.execute(select(SerieEpisode).where(
                SerieEpisode.serie_season_id == season.id)
                .order_by(SerieEpisode.episode_number))).scalars().all()
            assert [e.episode_number for e in eps] == [1, 2, 3], \
                f"season {season.season_number} must have 3 episodes, not a phantom E02"
            assert all(e.series_param for e in eps)
            assert all(e.cmd == f"/media/file_777_{season.season_number}.mpg" for e in eps)
