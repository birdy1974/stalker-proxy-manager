"""
Per-MAC channel-id translations — the explicit option that keeps every MAC of
one custom channel usable when a channel's id differs across packages.

What each test pins (the failure modes the design walked backward from):

* save() writes only the MACs whose view differs, upserts in place, counts all
  four skip buckets, finds stale ids by unique name, and drops a row that has
  become equal to the stored (fetch-MAC) view instead of shadowing it;
* plan_for asks THIS mac with its translation and never offers the stored cmd
  as alt_cmd (a wrong-channel retry after a refusal);
* _live_chain attaches inside the open session — the plain attribute is what
  survives on the detached chain rows;
* the probe attaches before plan_for, so it measures this MAC's id space (the
  mock portal's `seen_create_link.cmd` is the end-to-end witness);
* the endpoints validate BEFORE the portal 404 and both FKs really cascade;
* compare-genres seeds the saved-count on BOTH channel_ids paths: the offline
  message dict, and the legacy `None` the guard must not touch (unguarded it
  would be a TypeError → 500).
"""
from __future__ import annotations

import json

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (ChannelIdTranslation, LivePlaylist, LivePlaylistSource,
                        LiveSource, MacAddress, Portal, Setting)
from app.portal.links import plan_for
from app.portal.mock_portal import _STATE
from app.services import channel_translations as ct
from app.services import mac_probe
from app.services.stream_manager import MANAGER
from mockclient import GOOD, Wired
from test_mac_availability import _fake_first_bytes, _mock_route

BASE = "http://test"
MAC1 = "00:1A:79:00:00:01"
MAC2 = "00:1A:79:00:00:02"
STORED_CMD = "ffmpeg http://x/10.ts"
CMD_99 = "ffmpeg http://x/99.ts"


@pytest.fixture(autouse=True)
def _clean_manager():
    """MANAGER is a process-global singleton: leases and locks from one test
    must not decide the next one's answers."""
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()
    yield
    MANAGER.mac_locks.clear()
    MANAGER.redirect_leases.clear()
    MANAGER.lease_meta.clear()
    MANAGER.streams.clear()


@pytest.fixture(autouse=True)
def _fresh_mock_state():
    saved = dict(_STATE)
    _STATE.update({"create_links": 0, "create_link_error": None, "usage": {},
                   "max_per_mac": 1, "offline": False, "slow": False,
                   "create_link_seen": {}})
    yield
    _STATE.clear()
    _STATE.update(saved)


def _row(ids, cmds, *, key="bbc one", name="BBC One", status="differ"):
    """One Channel-IDs compare row, exactly the shape the tab POSTs."""
    return {"key": key, "name": name, "status": status, "ids": ids, "cmds": cmds}


async def _two_mac_portal(*, cid="10", name="BBC One", cmd=STORED_CMD,
                          online=True, twin=None, with_playlist=False):
    """portal (resolved → compare never touches the network) + two MACs +
    one LiveSource [+ a dual-fetch twin source] [+ a live playlist].

    Returns (portal_id, (mac1_id, mac2_id), src_id, playlist_id|None)."""
    async with SessionLocal() as s:
        p = Portal(name="tr", base_url="http://p.invalid/c/",
                   resolved_url="http://p.invalid/c/portal.php", enabled=True)
        s.add(p)
        await s.flush()
        status = "online" if online else "offline"
        m1 = MacAddress(portal_id=p.id, mac=MAC1, order=0, status=status, online=online)
        m2 = MacAddress(portal_id=p.id, mac=MAC2, order=1, status=status, online=online)
        s.add_all([m1, m2])
        await s.flush()
        src = LiveSource(portal_id=p.id, portal_channel_id=cid, original_name=name,
                         cmd=cmd, enabled=True)
        s.add(src)
        if twin:
            s.add(LiveSource(portal_id=p.id, portal_channel_id=twin[0],
                             original_name=name, cmd=twin[1], enabled=True))
        plid = None
        if with_playlist:
            await s.flush()
            pl = LivePlaylist(custom_name=name, enabled=True)
            s.add(pl)
            await s.flush()
            s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                     priority=1))
            plid = pl.id
        await s.commit()
        return p.id, (m1.id, m2.id), src.id, plid


async def _strategy(value: str) -> None:
    """Pin fallback_strategy the way runtime_settings._put does (JSON value)."""
    async with SessionLocal() as s:
        row = await s.get(Setting, "fallback_strategy")
        if row is None:
            s.add(Setting(key="fallback_strategy", value=json.dumps(value)))
        else:
            row.value = json.dumps(value)
        await s.commit()


# --------------------------------------------------------------------- save()
async def test_save_writes_only_the_macs_whose_view_differs_and_upserts():
    pid, (_m1, m2), sid, _ = await _two_mac_portal()
    out = await ct.save(pid, [_row({MAC1: "10", MAC2: "99"},
                                   {MAC1: STORED_CMD, MAC2: CMD_99})])
    assert out["ok"] and out["saved"] == 1 and out["unchanged"] == 1, out
    assert out["skipped"] == {"ambiguous": 0, "unmatched": 0,
                              "unknown_mac": 0, "empty_cmd": 0}
    assert out["count"] == 1
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        assert [(r.live_source_id, r.mac_id, r.portal_channel_id, r.cmd)
                for r in rows] == [(sid, m2, "99", CMD_99)], (
            "the fetch MAC (view == stored) needs no row; MAC2 does")
    # re-POST with a newer cmd for the same pair: upsert, still one row
    out2 = await ct.save(pid, [_row({MAC1: "10", MAC2: "99"},
                                    {MAC1: STORED_CMD, MAC2: "ffmpeg http://x/99b.ts"})])
    assert out2["saved"] == 1 and out2["count"] == 1, out2
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        assert len(rows) == 1 and rows[0].cmd == "ffmpeg http://x/99b.ts"


async def test_save_skip_counters_cover_mac_and_name_edges():
    pid, _m, _sid, _ = await _two_mac_portal(twin=("50", "ffmpeg http://x/50.ts"))
    rows = [
        # MAC not on this portal (the id/name still finds the target)
        _row({"00:1A:79:00:00:99": "10"}, {"00:1A:79:00:00:99": "cmdX"}),
        # known MAC, blank cmd
        _row({MAC1: "10"}, {MAC1: "   "}),
        # no id hit + unknown name
        _row({MAC1: "404"}, {MAC1: "cmd"}, key="ghost channel", name="Ghost"),
        # no id hit + TWO sources share the name → ambiguous
        _row({MAC1: "77"}, {MAC1: "cmd"}),
    ]
    out = await ct.save(pid, rows)
    assert out["saved"] == 0 and out["unchanged"] == 0 and out["count"] == 0, out
    assert out["skipped"] == {"ambiguous": 1, "unmatched": 1,
                              "unknown_mac": 1, "empty_cmd": 1}, out


async def test_save_falls_back_to_a_unique_name_when_the_ids_went_stale():
    pid, (_m1, m2), sid, _ = await _two_mac_portal()
    # nothing in `ids` matches the fetched cid any more (the panel renumbered),
    # but the normalized name is unique on this portal
    out = await ct.save(pid, [_row({MAC2: "77"}, {MAC2: "ffmpeg http://x/77.ts"})])
    assert out["saved"] == 1 and out["skipped"]["unmatched"] == 0, out
    assert out["count"] == 1
    async with SessionLocal() as s:
        r = (await s.execute(select(ChannelIdTranslation))).scalars().one()
        assert (r.live_source_id, r.mac_id, r.portal_channel_id) == (sid, m2, "77")


async def test_save_drops_a_row_that_now_equals_the_stored_cmd():
    pid, (m1, _m2), sid, _ = await _two_mac_portal()
    differ = _row({MAC1: "10", MAC2: "99"}, {MAC1: STORED_CMD, MAC2: CMD_99})
    out = await ct.save(pid, [differ])
    assert out["saved"] == 1 and out["count"] == 1, out
    # the operator re-fetches through MAC2: the stored row IS now MAC2's view
    async with SessionLocal() as s:
        src = await s.get(LiveSource, sid)
        src.portal_channel_id, src.cmd = "99", CMD_99
        await s.commit()
    out2 = await ct.save(pid, [differ])
    # MAC2 == stored → its old row must GO (else it would shadow the fetch);
    # MAC1 differs from the new stored view → gets its own row
    assert out2["unchanged"] == 1 and out2["saved"] == 1 and out2["count"] == 1, out2
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        assert [(r.mac_id, r.portal_channel_id) for r in rows] == [(m1, "10")]


async def test_save_maps_dual_fetch_duplicate_sources_by_id():
    """A channel fetched through both MACs exists as two source rows with ONE
    name; the ids in the compare row must map BOTH rows, so each source learns
    the other MAC's view (the name alone would be refused as ambiguous)."""
    pid, (m1, m2), s1, _ = await _two_mac_portal(twin=("50", "ffmpeg http://x/50.ts"))
    async with SessionLocal() as s:
        s2 = (await s.execute(select(LiveSource).where(
            LiveSource.portal_channel_id == "50"))).scalars().one().id
    out = await ct.save(pid, [_row({MAC1: "10", MAC2: "50"},
                                   {MAC1: STORED_CMD, MAC2: "ffmpeg http://x/50.ts"})])
    # per source: own MAC unchanged (its view IS the stored row), other saved
    assert out["saved"] == 2 and out["unchanged"] == 2 and out["count"] == 2, out
    assert out["skipped"]["ambiguous"] == 0, "id-hits must beat the dual-fetch name"
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        got = {(r.live_source_id, r.mac_id): r.portal_channel_id for r in rows}
    assert got == {(s1, m2): "50", (s2, m1): "10"}


# ------------------------------------------------------------------ plan_for
class _Src:
    def __init__(self, cmd, *, overrides=None, learned=""):
        self.cmd = cmd
        if overrides is not None:
            self.mac_cmd_overrides = overrides
        if learned:
            self.media_cmd = learned


class _Mac:
    def __init__(self, *, mac_id=None, mac=MAC1, force=False):
        if mac_id is not None:
            self.id = mac_id
        self.mac = mac
        self.force_ch_link_check = force


def test_plan_for_asks_this_mac_with_its_translation_and_never_an_alt():
    src = _Src(STORED_CMD, overrides={7: "ffmpeg http://x/translated.ts"},
               learned="learned form")
    plan = plan_for(src, _Mac(mac_id=7))
    assert plan.cmd == "ffmpeg http://x/translated.ts"
    assert plan.alt_cmd is None, (
        "stored/learned belong to another MAC's id space — offering them as a "
        "retry would hit the WRONG channel after a refusal")
    # a translation for a DIFFERENT mac must not leak into this one
    other = plan_for(src, _Mac(mac_id=8))
    assert other.cmd == "learned form"
    assert other.alt_cmd == STORED_CMD


def test_plan_for_without_a_translation_keeps_the_classic_branch():
    plain = _Src(STORED_CMD)                        # attribute absent entirely
    assert plan_for(plain, _Mac(mac_id=7)).cmd == STORED_CMD
    assert plan_for(plain, _Mac()).cmd == STORED_CMD           # mac row has no id
    assert plan_for(plain, None).cmd == STORED_CMD             # mac row is None
    blank = _Src(STORED_CMD, overrides={7: "   "})   # whitespace-only → falls through
    assert plan_for(blank, _Mac(mac_id=7)).cmd == STORED_CMD


# --------------------------------------------------------------- attach sites
async def test_live_chain_attaches_translations_that_survive_detach():
    pid, (m1, m2), _sid, plid = await _two_mac_portal(with_playlist=True)
    await _strategy("macs_first")
    out = await ct.save(pid, [_row({MAC1: "10", MAC2: "99"},
                                   {MAC1: STORED_CMD, MAC2: CMD_99})])
    assert out["saved"] == 1, out
    chain, name, _item = await MANAGER._live_chain(plid)
    assert len(chain) == 1 and name == "BBC One"
    src, _portal, macs = chain[0]
    assert [m.mac for m in macs] == [MAC1, MAC2], "macs_first picks both MACs"
    # the session closed when _live_chain returned — the plain attribute is
    # what survives on the now-detached row
    assert src.mac_cmd_overrides == {m2: CMD_99}
    assert plan_for(src, macs[0]).cmd == STORED_CMD
    assert plan_for(src, macs[1]).cmd == CMD_99
    assert plan_for(src, macs[1]).alt_cmd is None


async def test_probe_attaches_the_translation_and_asks_the_panel_with_it(monkeypatch):
    w = Wired(monkeypatch)
    pid, (mid,) = await _mock_route()      # source: cid 1002, "NPO 1", fetch cmd
    out = await ct.save(pid, [_row({GOOD: "7777"}, {GOOD: "ffmpeg http://mock/ts/7777.ts"},
                                   key="npo 1", name="NPO 1")])
    assert out["saved"] == 1, out
    seen: dict = {}
    real_plan_for = mac_probe.plan_for

    def spy(src, mac, **kw):
        # pins the ORDER: attach must have run before plan_for is asked
        seen["overrides"] = dict(getattr(src, "mac_cmd_overrides", None) or {})
        return real_plan_for(src, mac, **kw)

    monkeypatch.setattr(mac_probe, "plan_for", spy)
    monkeypatch.setattr(mac_probe, "_first_bytes", _fake_first_bytes(4096))
    rep = await mac_probe.probe_mac(pid, mid)
    assert rep["available"] is True, rep
    assert seen["overrides"] == {mid: "ffmpeg http://mock/ts/7777.ts"}
    # end-to-end witness: the panel was asked with THIS MAC's cmd
    state = await w.state()
    assert state["counters"]["create_links"] == 1
    assert state["seen_create_link"]["cmd"] == "ffmpeg http://mock/ts/7777.ts"


# ------------------------------------------------------------------ endpoints
async def test_endpoints_validate_first_then_rows_cascade_with_mac_and_portal():
    pid, (m1, m2), _sid, _ = await _two_mac_portal()
    both = _row({MAC1: "11", MAC2: "99"},
                {MAC1: "ffmpeg http://x/11.ts", MAC2: CMD_99})
    async with httpx.AsyncClient(transport=ASGITransport(app=app),
                                 base_url=BASE) as c:
        # every 400 fires BEFORE the portal is looked up (unknown pid!)
        for bad in ({"kind": "vod", "rows": []},
                    {"kind": "live", "rows": "not-a-list"},
                    {"kind": "live", "rows": [{}] * 5001},
                    {"kind": "live"}):                    # rows absent → not a list
            r = await c.post("/api/portals/999999/channel-id-translations", json=bad)
            assert r.status_code == 400, (bad, r.status_code, r.text)
        # a valid body on an unknown portal is the only 404
        r = await c.post("/api/portals/999999/channel-id-translations",
                         json={"kind": "live", "rows": []})
        assert r.status_code == 404, r.text
        r = await c.post(f"/api/portals/{pid}/channel-id-translations",
                         json={"kind": "live", "rows": [both]})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] and out["saved"] == 2 and out["count"] == 2, out
        assert out["skipped"] == {"ambiguous": 0, "unmatched": 0,
                                  "unknown_mac": 0, "empty_cmd": 0}
        r = await c.delete(f"/api/portals/{pid}/channel-id-translations")
        assert r.status_code == 200 and r.json() == {"ok": True, "cleared": 2}, r.text
        r = await c.delete("/api/portals/999999/channel-id-translations")
        assert r.status_code == 404, r.text

    # both FKs really cascade (FK pragma is ON per connection)
    await ct.save(pid, [both])
    async with SessionLocal() as s:
        rows = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        assert len(rows) == 2
    async with SessionLocal() as s:
        await s.delete(await s.get(MacAddress, m2))
        await s.commit()
    async with SessionLocal() as s:
        left = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        assert [r.mac_id for r in left] == [m1], "mac FK must take its own rows"
    async with SessionLocal() as s:
        await s.delete(await s.get(Portal, pid))
        await s.commit()
    async with SessionLocal() as s:
        left = (await s.execute(select(ChannelIdTranslation))).scalars().all()
        assert left == [], "portal delete must cascade through live_sources"


# ------------------------------------------------------------- compare guard
async def test_compare_seeds_the_count_and_keeps_the_legacy_none_path():
    pid, _m, _sid, _ = await _two_mac_portal(online=False)   # resolved → no network
    out = await ct.save(pid, [_row({MAC1: "10", MAC2: "99"},
                                   {MAC1: STORED_CMD, MAC2: CMD_99})])
    assert out["count"] == 1, out
    async with httpx.AsyncClient(transport=ASGITransport(app=app),
                                 base_url=BASE) as c:
        # two OFFLINE macs: usable is empty → the phase lands on its message
        # dict, and the None-guard still seeds the count on the way out
        r = await c.post(f"/api/portals/{pid}/compare-genres",
                         json={"mac_ids": [], "channel_kinds": ["live"]})
        assert r.status_code == 200, r.text
        ch = r.json()["channel_ids"]
        assert isinstance(ch, dict), ch
        assert "fewer than two usable packages" in (ch.get("message") or ""), ch
        assert ch["translations"] == {"count": 1}, ch
        # legacy path: channel_ids stays None — the guard must NOT touch it
        # (unguarded it would raise TypeError on None → HTTP 500 right here)
        r = await c.post(f"/api/portals/{pid}/compare-genres",
                         json={"mac_ids": [], "channel_kinds": []})
        assert r.status_code == 200, r.text
        assert r.json()["channel_ids"] is None
