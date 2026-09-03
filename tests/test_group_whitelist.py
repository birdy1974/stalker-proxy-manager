"""
Group whitelists: matching tolerance and the "filters everything out" log.

Reported as "the VOD titles do not appear in the VLC playlist - live, series
and local are fine, and the Enigma2 box shows VOD correctly". The M3U lines
themselves are well-formed and VLC parses them exactly like the other types,
so the failure family left standing is per-user visibility: a VOD whitelist
whose entries match no group in the library removes EVERY vod entry from
that user's M3U/Xtream/bouquet output, silently. Such entries come from a
portal category rename or a whitespace-padded paste, and the whitelist
editor only ever rendered groups that exist - the dead entries were
invisible there.

Pinned here: matching tolerates case AND stray whitespace (access semantics
unchanged otherwise), a user whose whitelist blackholes a whole type gets a
WARNING in the log pane naming the dead entries and the groups that DO
exist, the warning does not spam build after build, and whatever the GUI or
an API client sends is normalised before it reaches the database.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Portal, User, VodPlaylist, VodSource
from app.services import playlist_gen
from app.services.playlist_gen import _allowed, build_m3u

BASE = "http://proxy.local"


async def _seed_vod(groups: dict) -> User:
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://portal.invalid")
        s.add(p)
        await s.flush()
        vs = VodSource(portal_id=p.id, portal_item_id="v1",
                       original_name="The Big Movie (2026)")
        s.add(vs)
        await s.flush()
        s.add(VodPlaylist(vod_source_id=vs.id, custom_name="The Big Movie (2026)",
                          group_name="Action"))
        s.add(VodPlaylist(vod_source_id=vs.id, custom_name="Quiet One (2025)",
                          group_name="Comedy"))
        u = User(name="vlc-tv", password="pw", m3u_enabled=True,
                 xtream_enabled=True, groups_json=json.dumps(groups))
        s.add(u)
        await s.commit()
        return u


async def _set_groups(uid: int, groups: dict) -> User:
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        u.groups_json = json.dumps(groups)
        await s.commit()
        return await s.get(User, uid)


def test_allowed_is_case_and_whitespace_insensitive():
    assert _allowed("Action", ["Action"]) is True
    assert _allowed("Action", [" Action "]) is True
    assert _allowed(" Action", ["Action"]) is True
    assert _allowed("Action", ["ACTION"]) is True
    assert _allowed("Comedy", ["Action"]) is False
    assert _allowed(None, ["VOD"]) is False
    assert _allowed(None, []) is True          # empty list = allow all
    assert _allowed(None, [""]) is True        # pre-existing semantics kept


async def test_whitespace_padded_whitelist_entry_still_matches():
    user = await _seed_vod({"vod": [" Action "]})
    text = await build_m3u(BASE, user)
    assert "The Big Movie (2026)" in text
    assert "Quiet One (2025)" not in text, "matching must stay selective"


async def test_stale_whitelist_entries_still_filter_but_now_log_why(monkeypatch):
    logged: list[tuple[str, str, str]] = []

    async def fake_log(level, module, message):
        logged.append((level, module, message))

    monkeypatch.setattr(playlist_gen, "db_log", fake_log)

    user = await _seed_vod({"vod": ["Old Genre", "Filme EN"]})
    text = await build_m3u(BASE, user)
    # access control itself must not change: stale entries keep filtering
    assert "/play/vod/" not in text

    warnings = [m for lvl, _mod, m in logged if lvl == "WARNING"]
    assert len(warnings) == 1, "one clear warning, not a stream of noise"
    msg = warnings[0]
    assert "'vlc-tv'" in msg
    assert "vod" in msg
    assert "Old Genre" in msg and "Filme EN" in msg, \
        "the dead whitelist entries must be named"
    assert "Action" in msg and "Comedy" in msg, \
        "the groups that DO exist must be offered as the fix"


async def test_blackhole_warning_is_deduped_and_rearms_after_a_fix(monkeypatch):
    logged: list[tuple[str, str, str]] = []

    async def fake_log(level, module, message):
        logged.append((level, module, message))

    monkeypatch.setattr(playlist_gen, "db_log", fake_log)

    user = await _seed_vod({"vod": ["Old Genre"]})
    await build_m3u(BASE, user)
    playlist_gen._M3U_CACHE.clear()     # force a real rebuild, keep the dedupe
    await build_m3u(BASE, user)
    assert len(logged) == 1, "identical state must not warn twice"

    # fixed whitelist: healthy again, no log, and the dedupe disarms
    user = await _set_groups(user.id, {"vod": ["Action"]})
    playlist_gen._M3U_CACHE.clear()
    text = await build_m3u(BASE, user)
    assert "/play/vod/" in text
    assert len(logged) == 1

    # broken again later -> warn again
    user = await _set_groups(user.id, {"vod": ["Old Genre"]})
    playlist_gen._M3U_CACHE.clear()
    await build_m3u(BASE, user)
    assert len(logged) == 2, "a NEW blackhole occurrence must warn again"


async def test_healthy_whitelist_logs_nothing(monkeypatch):
    logged: list[tuple[str, str, str]] = []

    async def fake_log(level, module, message):
        logged.append((level, module, message))

    monkeypatch.setattr(playlist_gen, "db_log", fake_log)

    user = await _seed_vod({"vod": ["Action"]})
    text = await build_m3u(BASE, user)
    assert "The Big Movie (2026)" in text
    assert logged == []


def test_clean_groups_normalises_before_storage():
    from app.routers.api_users import _clean_groups

    assert _clean_groups({"vod": [" Action ", "action", "", "  ", "Comedy"]}) == \
        {"live": [], "vod": ["Action", "Comedy"], "series": [], "local": []}
    assert _clean_groups({"vod": "Solo"})["vod"] == ["Solo"], \
        "a bare string is one entry, not one entry per character"
    assert _clean_groups(None) == {"live": [], "vod": [], "series": [], "local": []}
    assert _clean_groups({"junk": ["x"]}) == \
        {"live": [], "vod": [], "series": [], "local": []}, "unknown keys are dropped"


async def test_users_api_reports_stale_whitelist_entries():
    await _seed_vod({"vod": [" Action", "Old Genre"], "live": []})
    from app.routers.api_users import _row
    async with SessionLocal() as s:
        uid = (await s.execute(select(User.id).where(User.name == "vlc-tv"))).scalar_one()
        u = await s.get(User, uid)
        row = _row(u, BASE, {"live": [], "vod": ["Action"], "series": [], "local": []})

    assert row["groups_stale"]["vod"] == ["Old Genre"], \
        "the padded entry matches after normalising; the dead one is reported"
    assert row["groups_stale"]["live"] == []
