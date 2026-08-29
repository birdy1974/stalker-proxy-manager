"""
Detail-popup stream probe + TMDB lookup.

Two production bugs lived here:
  * the probe timed out after a hard-coded 10s because it ran without the MAG
    identity (panels/CDNs stall the default Lavf user-agent);
  * TMDB always answered "no hit" because the query carried panel noise and
    any error (bad key, network) was swallowed and cached as "no hit".
"""

from __future__ import annotations

from app.database import SessionLocal
from app.models import Setting
from app.services import tmdb as tmdb_mod
from app.services.probe import PROBE_TIMEOUT, _probe_args


def test_probe_args_impersonate_the_mag_box_for_network_streams():
    args = _probe_args("http://cdn.example.com/x/1.ts", is_url=True)
    assert "-user_agent" in args
    assert "Lavf" not in " ".join(args)          # never the default Lavf UA
    assert args[args.index("-referer") + 1] == "http://cdn.example.com/"
    assert "-reconnect" in args and "-rw_timeout" in args
    assert args[0] == "ffmpeg"                    # replaced by FFMPEG_BIN at runtime


def test_probe_args_stay_plain_for_local_files():
    args = _probe_args("/media/some/file.ts", is_url=False)
    assert "-user_agent" not in args
    assert "-referer" not in args
    assert "-reconnect" not in args


def test_probe_timeout_is_not_the_old_10s():
    # the reported failure was "probe timed out (>10s)"; the cap must be
    # comfortably above that now
    assert PROBE_TIMEOUT >= 30


def test_tmdb_clean_query_strips_panel_noise():
    c = tmdb_mod._clean_query
    assert c("Movie.Name (1999) HD") == "Movie Name"
    assert c("Some.Show.S01E02.1080p") == "Some Show"
    assert c("The Film 2021") == "The Film"
    assert c("Title [2018]") == "Title"
    assert c("Series 4K HEVC x265") == "Series"
    assert c("Show 2 (720p)") == "Show 2"
    assert c("Clean Title") == "Clean Title"


async def test_tmdb_lookup_returns_none_without_a_key(monkeypatch):
    async def no_key():
        return ""
    monkeypatch.setattr(tmdb_mod, "_api_key", no_key)
    assert await tmdb_mod.tmdb_lookup("Some Movie", "2020", "vod") is None


async def test_tmdb_lookup_errors_loudly_for_a_noise_only_title(monkeypatch):
    async def some_key():
        return "fake-key"
    monkeypatch.setattr(tmdb_mod, "_api_key", some_key)
    out = await tmdb_mod.tmdb_lookup("1080p", None, "vod")
    assert isinstance(out, dict) and "error" in out


async def test_tmdb_api_key_reads_both_json_and_plain_storage():
    # settings are stored JSON-encoded; legacy rows may be plain text.
    async with SessionLocal() as s:
        s.add(Setting(key="tmdb_api_key", value='"json-key-123"'))
        await s.commit()
        assert await tmdb_mod._api_key() == "json-key-123"
        row = await s.get(Setting, "tmdb_api_key")
        row.value = "plain-key-456"
        await s.commit()
        assert await tmdb_mod._api_key() == "plain-key-456"
