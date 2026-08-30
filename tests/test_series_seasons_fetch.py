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
