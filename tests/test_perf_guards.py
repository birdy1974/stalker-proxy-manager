"""
Long-run memory bounds, event-loop hygiene and the health snapshot.

The audit these guards come from found the classic slow-leak shape: caches
whose reads checked a TTL but which nothing ever EVICTED (probe results keyed
by token-carrying URLs, TMDB results keyed by title, whole-genre previews
keyed by genre), CPU-heavy batch work running on the event loop (XMLTV
passes, O(items x candidates) difflib matching, per-file scans), and a
~0.5 s diagnostics report recomputed per poll/keystroke. Each test below
pins one fix so the shape cannot silently come back.
"""

from __future__ import annotations

import random
import re
import time
from difflib import SequenceMatcher
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import probe, tmdb
from app.services import playlist_health as health


# --------------------------------------------------------------- probe caches
def test_probe_caches_prune_expired_and_keep_fresh():
    now = time.time()
    probe._CACHE.clear(); probe._SUBS_CACHE.clear(); probe._CODECS_CACHE.clear()
    probe._CACHE["old"] = (now - probe._CACHE_TTL - 1, {})
    probe._CACHE["new"] = (now, {})
    probe._SUBS_CACHE["old"] = (now - probe._SUBS_CACHE_TTL - 1, [])
    probe._SUBS_CACHE["new"] = (now, [])
    probe._CODECS_CACHE["old"] = (now - probe._SUBS_CACHE_TTL - 1, None)
    gone = probe.prune_caches()
    assert gone == 3
    assert list(probe._CACHE) == ["new"]
    assert list(probe._SUBS_CACHE) == ["new"]
    assert list(probe._CODECS_CACHE) == []
    probe._CACHE.clear(); probe._SUBS_CACHE.clear(); probe._CODECS_CACHE.clear()


def test_probe_cache_put_is_capped_and_evicts_expired_first(monkeypatch):
    monkeypatch.setattr(probe, "_CACHE_MAX", 3)
    cache: dict = {}
    now = time.time()
    cache["stale"] = (now - 10_000, "x")          # expired under ttl=60
    probe._cache_put(cache, "a", 1, 60)
    probe._cache_put(cache, "b", 2, 60)           # 3 entries, at cap
    probe._cache_put(cache, "c", 3, 60)           # over cap: stale goes first
    assert list(cache) == ["a", "b", "c"]
    probe._cache_put(cache, "d", 4, 60)           # now the oldest live one goes
    assert list(cache) == ["b", "c", "d"]
    # re-putting refreshes position (least-recently-STORED eviction)
    probe._cache_put(cache, "b", 22, 60)
    probe._cache_put(cache, "e", 5, 60)
    assert list(cache) == ["d", "b", "e"]


def test_tmdb_cache_prunes_expired():
    now = time.time()
    tmdb._CACHE.clear()
    tmdb._CACHE["old"] = (now - tmdb._CACHE_TTL - 1, {})
    tmdb._CACHE["new"] = (now, {})
    assert tmdb.prune_cache() == 1
    assert list(tmdb._CACHE) == ["new"]
    tmdb._CACHE.clear()


# ------------------------------------------------------------ pending genres
def test_pending_genre_previews_expire_and_are_capped(monkeypatch):
    from app.routers import api_portals as ap

    ap.clear_pending_genre_items()
    monkeypatch.setattr(ap, "_PENDING_GENRE_MAX", 2)
    ap._pending_put((1, "vod", 1), [{"id": "1"}])
    ap._pending_put((1, "vod", 2), [{"id": "2"}])
    ap._pending_put((1, "vod", 3), [{"id": "3"}])     # over cap: oldest dropped
    assert ap._pending_get((1, "vod", 1)) is None
    assert ap._pending_get((1, "vod", 3)) == [{"id": "3"}]
    # an aged-out preview is dropped on read (caller refetches from the portal)
    key = (1, "vod", 3)
    at, items = ap._PENDING_GENRE_ITEMS[key]
    ap._PENDING_GENRE_ITEMS[key] = (at - ap._PENDING_GENRE_TTL - 1, items)
    assert ap._pending_get(key) is None
    assert key not in ap._PENDING_GENRE_ITEMS
    # and the janitor-facing prune counts what it drops
    ap._pending_put((2, "live", 9), [{"id": "9"}])
    at, items = ap._PENDING_GENRE_ITEMS[(2, "live", 9)]
    ap._PENDING_GENRE_ITEMS[(2, "live", 9)] = (at - ap._PENDING_GENRE_TTL - 1, items)
    assert ap.prune_pending_genre_items() == 1
    ap.clear_pending_genre_items()


# ------------------------------------------------------------ refusal counter
def test_refusal_counter_stops_minting_keys_past_the_cap():
    from app.portal import client

    client.reset_refusals()
    for i in range(512):
        client._refusals[("h", f"code{i}")] = 1
    client.note_refusal("http://h/c/", "brand_new_code")
    assert ("h", "brand_new_code") not in client._refusals   # capped
    client.note_refusal("http://h/c/", "code7")
    assert client._refusals[("h", "code7")] == 2             # known still count
    client.reset_refusals()


# ------------------------------------------------------------- janitor wiring
async def test_janitor_sweeps_the_probe_and_preview_tables():
    from app.routers import api_portals as ap
    from app.services import janitor

    now = time.time()
    probe._CACHE["stale"] = (now - probe._CACHE_TTL - 1, {})
    tmdb._CACHE["stale"] = (now - tmdb._CACHE_TTL - 1, {})
    ap._PENDING_GENRE_ITEMS[(9, "vod", 9)] = (time.monotonic() - 99_999, [])
    try:
        report = await janitor.sweep_once()
        assert report.get("probe_caches", 0) >= 1
        assert report.get("tmdb_cache", 0) >= 1
        assert report.get("pending_genres", 0) >= 1
    finally:
        probe._CACHE.clear(); tmdb._CACHE.clear()
        ap.clear_pending_genre_items()


# --------------------------------------------------------------- EPG ranking
def _reference_rank(name, candidates, limit=20):
    """The pre-optimization ranking, verbatim: the optimized version must
    produce byte-identical output (kept candidates AND their scores)."""
    from app.services.epg import norm_name

    def sim(a, b):
        return SequenceMatcher(None, a, b).ratio() if a and b else 0.0

    want = norm_name((name or "").replace("+", " plus "))
    numbers = re.findall(r"\d+", want)
    ranked = []
    for candidate in candidates:
        score = 0.0
        for alias in candidate["names"]:
            normalized = norm_name(alias.replace("+", " plus "))
            value = sim(want, normalized)
            if numbers != re.findall(r"\d+", normalized):
                value *= 0.5
            if ("plus" in want.split()) != ("plus" in normalized.split()):
                value *= 0.5
            score = max(score, value)
        if score >= 0.62:
            ranked.append(
                {k: v for k, v in candidate.items() if k != "names"}
                | {"score": round(score, 4)}
            )
    ranked.sort(key=lambda row: (-row["score"], row["tvg_id"]))
    return ranked[:limit]


def test_rank_candidates_matches_the_reference_algorithm():
    from app.services.epg import rank_candidates

    random.seed(20261025)
    base = ["NPO", "RTL", "SBS", "Viaplay", "ESPN", "BBC", "Discovery",
            "Film", "News", "Kids", "MTV", "National Geographic", ""]

    def mk() -> str:
        name = random.choice(base)
        if random.random() < 0.7:
            name += f" {random.randint(1, 99)}"
        if random.random() < 0.3:
            name += random.choice([" HD", "+", " TV+", " (NL)", " 4K", " FHD"])
        return name

    candidates = [
        {"tvg_id": f"id{i}", "name": mk(),
         "names": [mk() for _ in range(random.randint(0, 3))],
         "sources": ["s"], "source_ids": [1]}
        for i in range(250)
    ]
    for _ in range(200):
        name = mk()
        assert rank_candidates(name, candidates) == _reference_rank(name, candidates)


# --------------------------------------------------------------- EPG parsing
def test_epg_parse_passes_produce_the_ingest_rows():
    from datetime import datetime, timedelta, timezone

    from app.services.epg import _parse_channels, _parse_programmes

    raw = (
        '<tv generator-info-name="t">'
        '<channel id="npo1.nl"><display-name>NPO 1</display-name>'
        '<icon src="http://x/1.png"/></channel>'
        '<channel id="npo2.nl"><display-name>NPO 2</display-name></channel>'
        '<programme channel="npo1.nl" start="20261025080000 +0200" '
        'stop="20261025090000 +0200"><title>News</title>'
        '<category>News</category></programme>'
        '<programme channel="npo2.nl" start="20261025080000 +0200" '
        'stop="20261025090000 +0200"><title>Other</title></programme>'
        '<programme channel="npo1.nl" start="bad" stop="bad">'
        '<title>Broken</title></programme>'
        "</tv>"
    ).encode()
    root_attrs, chans = _parse_channels(raw, 7)
    assert root_attrs == {"generator-info-name": "t"}
    assert [c["tvg_id"] for c in chans] == ["npo1.nl", "npo2.nl"]
    assert chans[0] == {"epg_source_id": 7, "tvg_id": "npo1.nl",
                         "name": "NPO 1", "icon": "http://x/1.png"}
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    rows = _parse_programmes(
        raw, src_id=7, wanted={"npo1.nl"},
        lo=now - timedelta(days=1), hi=now + timedelta(days=1),
        root_attrs=root_attrs,
        timing=SimpleNamespace(timezone_mode="auto", timezone_name=None),
        is_portal=False)
    # wanted filter drops npo2; the broken timestamps drop the third
    assert len(rows) == 1
    assert rows[0]["title"] == "News" and rows[0]["category"] == "News"
    assert rows[0]["start_ts"].hour == 6      # 08:00 +0200 -> 06:00 UTC


def test_epg_parse_rejects_non_xmltv():
    import pytest

    from app.services.epg import _parse_channels

    with pytest.raises(ValueError, match="not an XMLTV"):
        _parse_channels(b"<html><body>nope</body></html>", 1)


# ---------------------------------------------------------------- local scan
def test_walk_local_source_collects_plain_data(tmp_path):
    from app.routers.api_sources import _walk_local_source

    (tmp_path / "a.mkv").write_bytes(b"x" * 10)
    (tmp_path / "b.txt").write_bytes(b"x")            # not a video ext
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.MP4").write_bytes(b"y" * 5)             # case-insensitive ext

    found, skipped, seen = _walk_local_source(tmp_path, recursive=True)
    assert seen == 2 and skipped == []
    assert {rel for rel, _fn, _sz, _mt in found} == {"a.mkv", "sub/c.MP4"}
    by_rel = {rel: (fn, sz) for rel, fn, sz, _mt in found}
    assert by_rel["a.mkv"] == ("a.mkv", 10)
    assert all(mt.endswith("+00:00") for *_x, mt in found)

    found, _skipped, seen = _walk_local_source(tmp_path, recursive=False)
    assert seen == 1 and [r for r, *_ in found] == ["a.mkv"]


# --------------------------------------------------------- health snapshot
async def _live_channel(db):
    from app.models import (LivePlaylist, LivePlaylistSource, LiveSource,
                            MacAddress, Portal)

    p = Portal(name="Provider", base_url="http://provider",
               resolved_url="http://provider/portal.php")
    db.add(p)
    await db.flush()
    db.add(MacAddress(portal_id=p.id, mac="00:11:22:33:44:55", status="online"))
    source = LiveSource(portal_id=p.id, portal_channel_id="1",
                        original_name="Input", cmd="http://x/1", enabled=True)
    item = LivePlaylist(custom_name="Channel")
    db.add_all([source, item])
    await db.flush()
    db.add(LivePlaylistSource(live_playlist_id=item.id,
                              live_source_id=source.id, priority=1))
    await db.commit()
    return item, source


async def test_health_report_is_a_snapshot_until_something_changes(monkeypatch):
    from app.database import SessionLocal

    builds = []
    real_build = health._build_report

    async def counting_build(db, strategy):
        builds.append(1)
        return await real_build(db, strategy)

    monkeypatch.setattr(health, "_build_report", counting_build)
    async with SessionLocal() as db:
        _item, source = await _live_channel(db)
        first = await health.report(db)
        second = await health.report(db)
        assert builds == [1]                          # second call: snapshot
        assert second["checked_at"] == first["checked_at"]
        assert second["items"] == first["items"]

        # a recorded verdict invalidates immediately (probe -> refresh flow)
        health.record_probe(health.signature(source), {"video": {"codec": "h264"}})
        third = await health.report(db)
        assert len(builds) == 2
        assert third["items"][0]["status"] == "healthy"

        # a write to a table the report reads invalidates via health generation
        source.cmd = "http://x/changed"
        await db.commit()
        fourth = await health.report(db)
        assert len(builds) == 3
        assert fourth["items"][0]["status"] == "unverified"   # cmd changed


async def test_health_snapshot_follows_the_fallback_strategy(monkeypatch):
    from app.database import SessionLocal
    from app.services import runtime_settings

    monkeypatch.setattr(runtime_settings, "fallback_strategy",
                        AsyncMock(return_value="macs_first"))
    async with SessionLocal() as db:
        await _live_channel(db)
        assert (await health.report(db))["fallback_strategy"] == "macs_first"
        monkeypatch.setattr(runtime_settings, "fallback_strategy",
                            AsyncMock(return_value="portal_first"))
        # no DB write happened - the snapshot must still not be served
        assert (await health.report(db))["fallback_strategy"] == "portal_first"


async def test_health_cache_can_be_disabled(monkeypatch):
    from app.database import SessionLocal

    monkeypatch.setattr(health, "REPORT_CACHE_S", 0.0)
    builds = []
    real_build = health._build_report

    async def counting_build(db, strategy):
        builds.append(1)
        return await real_build(db, strategy)

    monkeypatch.setattr(health, "_build_report", counting_build)
    async with SessionLocal() as db:
        await _live_channel(db)
        await health.report(db)
        await health.report(db)
        assert len(builds) == 2
