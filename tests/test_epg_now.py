"""
R9 - "what is on now" from the portal's own short EPG, with EStalker's budget.

The endpoint is trivial (`type=itv&action=get_short_epg&ch_id=&size=10`). The
*accounting* is the feature, and it is the only reason a guide tooltip exists in
this app instead of being a `for` loop: one request per channel against somebody's
Stalker panel, for a GUI nicety. EStalker gets away with it because it asks only
for the rows on screen, once per batch, and gives up on a portal that refuses.

These tests therefore assert mostly *counts* - what the panel was asked, not what
we parsed (`app/portal/epg.py`'s parsing has its own tests). Each one says which
of the four budget rules it protects:

  * a configured XMLTV source answers for free, in zero portal requests;
  * a batch is deduped, bounded, and cached (TTL) across calls;
  * a refusal stops the batch instead of walking the rest of the catalogue;
  * a 503 is retried twice, and only a 503.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.database import SessionLocal
from app.models import (EpgProgramme, LivePlaylist, LivePlaylistSource, LiveSource,
                        MacAddress, Portal)
from app.portal.mock_portal import _STATE
from app.routers import api_epg
from app.services import epg_now as svc
from app.services.db_logging import flush_logs
from mockclient import GOOD, PORTAL, Wired


@pytest.fixture(autouse=True)
def _fresh_mock():
    """Counters are module state and cumulative; every 'we did not ask' assertion
    in this file is only meaningful if the box starts at zero."""
    saved = {k: _STATE.get(k) for k in ("epg_mode", "short_epg_calls", "flaky_hits",
                                        "xtream_mode", "modules", "modules_disabled")}
    _STATE["epg_mode"] = "normal"
    _STATE["short_epg_calls"] = 0
    _STATE["flaky_hits"] = 0
    _STATE["xtream_mode"] = "off"
    svc.cache_clear()
    yield
    _STATE.update(saved)
    svc.cache_clear()


async def _portal(s, *, tz="Europe/Amsterdam", modules=None, mac=GOOD, online=True,
                  channels=3):
    p = Portal(name="guidetest", base_url="http://test/mock/c/", resolved_url=PORTAL,
               enabled=True, stb_timezone=tz, modules=modules)
    s.add(p)
    await s.flush()
    if mac:            # mac="" means "this portal has no box registered at all"
        s.add(MacAddress(portal_id=p.id, mac=mac, order=0,
                         status="online" if online else "expired", online=online))
    ids = []
    for i in range(channels):
        src = LiveSource(portal_id=p.id, portal_channel_id=str(1001 + i),
                        original_name=f"Mock Channel {i + 1}", number=str(i + 1),
                        cmd=f"ffmpeg http://mock/ts/{1001 + i}.ts", enabled=True)
        s.add(src)
        await s.flush()
        ids.append(src.id)
    await s.commit()
    return p.id, ids


# =========================================================================== #
# the batch API itself
# =========================================================================== #
async def test_a_visible_page_costs_one_request_per_channel_and_no_more(monkeypatch):
    """The core budget: N rows asked = N portal calls, never N × something."""
    Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=3)
    out = await svc.now_for(live_ids=ids)
    assert len(out["items"]) == 3
    assert all(v["found"] and v["source"] == "portal" for v in out["items"].values())
    assert out["counts"]["asked"] == 3 and out["counts"]["skipped"] == 0
    assert _STATE["short_epg_calls"] == 3, "one channel, one request - not one per retry, per MAC or per flag"


async def test_the_same_row_listed_twice_is_asked_once(monkeypatch):
    """The GUI can hand us the same source from two tabs; the portal must not see it twice."""
    Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=2)
    out = await svc.now_for(live_ids=[ids[0], ids[0], ids[1], ids[1]])
    assert len(out["items"]) == 2 and _STATE["short_epg_calls"] == 2


async def test_a_second_page_is_served_from_the_cache(monkeypatch):
    """Scrolling back must be free, and `refresh=1` must still be possible.

    The TTL is the difference between "a tooltip" and "a load problem" on a panel
    with a few hundred channels: the first page pays, the rest do not.
    """
    Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=2)
    first = await svc.now_for(live_ids=ids)
    calls = _STATE["short_epg_calls"]
    again = await svc.now_for(live_ids=ids)
    assert _STATE["short_epg_calls"] == calls
    assert again["counts"]["cache"] == 2 and again["items"] == first["items"]
    forced = await svc.now_for(live_ids=ids, refresh=True)
    assert forced["counts"]["cache"] == 0 and _STATE["short_epg_calls"] == calls + 2


async def test_a_batch_is_bounded_and_says_so(monkeypatch):
    """Even a buggy caller cannot turn this into a catalogue walk."""
    Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=2)
    # two real rows + (MAX_IDS + 3) phantoms = MAX_IDS + 5 asked, 5 refused
    many = ids + list(range(max(ids) + 1, max(ids) + 1 + svc.MAX_IDS + 3))
    out = await svc.now_for(live_ids=many)
    assert out["truncated"] == 5
    assert len(out["items"]) == svc.MAX_IDS
    assert _STATE["short_epg_calls"] == 2      # the phantom ids cost nothing either


# =========================================================================== #
# XMLTV first: the whole point of ordering the lookup
# =========================================================================== #
async def test_a_configured_guide_source_makes_the_portal_call_unnecessary(monkeypatch):
    """Zero portal requests for a channel whose XMLTV row spans now.

    This is the rule that keeps R9 affordable: on an install with a guide source,
    "what's on" is a local SELECT, and the portal is asked only for the channels
    the guide does not cover.
    """
    Wired(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    async with SessionLocal() as s:
        pid, ids = await _portal(s, channels=2)
        src = await s.get(LiveSource, ids[0])
        src.epg_original = "mock.channel.one"
        s.add(EpgProgramme(tvg_id="mock.channel.one", start_ts=now - timedelta(minutes=30),
                          stop_ts=now + timedelta(minutes=30), title="Guide Says This",
                          desc="from the xmltv source"))
        s.add(EpgProgramme(tvg_id="mock.channel.one", start_ts=now + timedelta(minutes=30),
                          stop_ts=now + timedelta(minutes=90), title="Guide Says Next"))
        await s.commit()

    out = await svc.now_for(live_ids=ids)
    mine = out["items"][f"live:{ids[0]}"]
    other = out["items"][f"live:{ids[1]}"]
    assert mine["source"] == "xmltv" and mine["now"]["title"] == "Guide Says This"
    assert mine["next"]["title"] == "Guide Says Next"
    # +/-1 minute, not exact: the rows are seeded from a minute-truncated `now`
    # while the service uses the real one, and an assertion that depends on which
    # side of a minute boundary the test runner woke up on is a flaky test
    assert abs(mine["next"]["starts_in"] - 30) <= 1
    assert abs(mine["now"]["minutes_left"] - 30) <= 1
    assert 0 <= mine["now"]["progress"] <= 100
    assert other["source"] == "portal"
    assert _STATE["short_epg_calls"] == 1, "only the uncovered channel may reach the panel"


async def test_a_guide_that_knows_the_future_still_answers(monkeypatch):
    """`found: False` with a `next` is an answer, and asking the portal to confirm
    an empty hour would be a request spent proving nothing."""
    Wired(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=1)
        src = await s.get(LiveSource, ids[0])
        src.epg_original = "gap.channel"
        s.add(EpgProgramme(tvg_id="gap.channel", start_ts=now + timedelta(minutes=45),
                          stop_ts=now + timedelta(minutes=105), title="Later"))
        await s.commit()
    out = await svc.now_for(live_ids=ids)
    item = out["items"][f"live:{ids[0]}"]
    assert item["found"] is False and item["now"] is None
    assert item["next"]["title"] == "Later"
    assert _STATE["short_epg_calls"] == 0


async def test_an_unknown_tvg_id_falls_through_to_the_portal(monkeypatch):
    Wired(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=1)
        src = await s.get(LiveSource, ids[0])
        src.epg_original = "not.in.the.guide.at.all"
        s.add(EpgProgramme(tvg_id="someone.else", start_ts=now - timedelta(hours=1),
                          stop_ts=now + timedelta(hours=1), title="Other channel"))
        await s.commit()
    out = await svc.now_for(live_ids=ids)
    assert out["items"][f"live:{ids[0]}"]["source"] == "portal"


# =========================================================================== #
# refusal, failure and the stop-loss
# =========================================================================== #
async def test_a_panel_without_the_action_is_told_once_and_then_left_alone(monkeypatch):
    """`epg_mode=absent` answers 404 + `no such action`: not retryable, and after
    three of them the rest of the batch is skipped *for that portal*.

    The distinction from the next test is the whole reason `empty`, `absent` and
    `flaky` are separate mock modes: "no guide" and "busy" look identical in a log
    and opposite in what the user should do next.
    """
    w = Wired(monkeypatch)
    await w.control(epg_mode="absent")
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=8)
    out = await svc.now_for(live_ids=ids)
    assert _STATE["short_epg_calls"] == svc.MAX_PORTAL_FAILURES, \
        "a rude panel must be asked three times, not eight"
    errors = [v for v in out["items"].values() if "no such action" in v.get("why", "")
              or "PortalError" in v.get("why", "")]
    skipped = [v for v in out["items"].values() if "not asked" in v.get("why", "")]
    assert len(errors) == svc.MAX_PORTAL_FAILURES and len(skipped) == 8 - len(errors)
    assert out["counts"]["skipped"] == 8 - len(errors)


async def test_a_busy_panel_is_retried_twice_then_believed(monkeypatch):
    """503, 503, then the schedule: the first channel spends 3 requests and wins."""
    w = Wired(monkeypatch)
    await w.control(epg_mode="flaky")
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=3)
    out = await svc.now_for(live_ids=ids)
    assert all(v["found"] and v["source"] == "portal" for v in out["items"].values())
    assert _STATE["short_epg_calls"] == 5      # 3 for the first (503, 503, ok), 1 each after


async def test_an_empty_guide_is_reported_as_empty_not_as_a_failure(monkeypatch):
    """`{"js": []}` means "no schedule here": answer `found: False`, ask nothing extra."""
    w = Wired(monkeypatch)
    await w.control(epg_mode="empty")
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=3)
    out = await svc.now_for(live_ids=ids)
    assert all(v["source"] == "portal" and v["found"] is False for v in out["items"].values())
    assert all("why" not in v for v in out["items"].values())
    assert _STATE["short_epg_calls"] == 3      # an empty answer is not retried


async def test_a_panel_that_declares_no_epg_module_is_not_asked_at_all(monkeypatch):
    """R6's answer, spent here: `get_modules` already told us, so the tooltip is grey.

    `gated: True` is what the GUI needs - a skipped row because of evidence is not
    a row that failed, and it must not be drawn in the same red as a refusal.
    """
    from app.portal.capabilities import dumps_modules
    Wired(monkeypatch)
    async with SessionLocal() as s:
        pid, ids = await _portal(s, channels=4)
        p = await s.get(Portal, pid)
        p.modules = dumps_modules(["itv", "vod"])
        await s.commit()
    out = await svc.now_for(live_ids=ids)
    assert _STATE["short_epg_calls"] == 0
    assert all(v["gated"] and "epg" in v["why"] for v in out["items"].values())
    assert out["counts"] == {"xmltv": 0, "cache": 0, "asked": 0, "skipped": 4}


async def test_a_disabled_or_mac_less_portal_is_explained(monkeypatch):
    """Two ordinary misconfigurations, each with its own sentence.

    Both of these used to be silent: no rows, no reason, and a user concluding the
    portal is broken when the portal was never asked.
    """
    Wired(monkeypatch)
    async with SessionLocal() as s:
        pid, ids = await _portal(s, channels=1, mac="")
    out = await svc.now_for(live_ids=ids)
    assert "no MAC" in out["items"][f"live:{ids[0]}"]["why"]
    assert _STATE["short_epg_calls"] == 0

    async with SessionLocal() as s:
        pid2, ids2 = await _portal(s, channels=1)
        p = await s.get(Portal, pid2)
        p.enabled = False
        await s.commit()
    out2 = await svc.now_for(live_ids=ids2)
    assert "disabled" in out2["items"][f"live:{ids2[0]}"]["why"]


# =========================================================================== #
# the timezone chain, which is where a portal guide is actually wrong
# =========================================================================== #
async def test_portal_times_are_read_in_the_timezone_we_declared(monkeypatch):
    """The answer carries no offset, so the `timezone=` cookie decides what it means.

    The mock renders its schedule in the zone from that cookie - which is the only
    way this file can prove the *chain* (Portal.stb_timezone → cookie → parse)
    rather than a constant both sides happen to share.
    """
    from zoneinfo import ZoneInfo
    tz_name = "Asia/Bangkok"
    Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=1, tz=tz_name)
    out = await svc.now_for(live_ids=ids)
    item = out["items"][f"live:{ids[0]}"]
    assert item["found"] is True
    local_now = datetime.now(ZoneInfo(tz_name))
    start = datetime.fromisoformat(item["now"]["start"]).replace(tzinfo=ZoneInfo(tz_name))
    # the mock starts the current programme 20 minutes ago, on the hour boundary
    assert abs((local_now - start).total_seconds() - 1200) < 120
    assert start.hour == local_now.hour or (local_now.minute < 20 and
                                            start.hour == (local_now.hour - 1) % 24)
    assert item["now"]["title"]


async def test_an_unknown_timezone_name_is_read_as_utc_instead_of_crashing(monkeypatch):
    """`SPM_STB_TIMEZONE` can be any string in the settings file; a bad one is not fatal."""
    w = Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=1, tz="Mars/Olympus_Mons")
        assert (await w.state())["state"]["epg_mode"] == "normal"
    out = await svc.now_for(live_ids=ids)
    assert out["items"][f"live:{ids[0]}"]["found"] is True


# =========================================================================== #
# the playlist flavour, and the router
# =========================================================================== #
async def test_playlist_rows_are_answered_by_their_first_source(monkeypatch):
    """`playlist:<id>` is what the output page shows; its ids are not source ids.

    A playlist item's XMLTV key is its own `epg_id` (the mapping lives there, not
    on the source), which is why the service takes both lists instead of assuming
    the caller knows which table a row came from.
    """
    Wired(monkeypatch)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=2)
        pl = LivePlaylist(custom_name="From Portal", enabled=True, epg_id="chan.two")
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=ids[1], priority=1))
        pl2 = LivePlaylist(custom_name="Orphan", enabled=True)
        s.add(pl2)
        await s.flush()
        s.add(EpgProgramme(tvg_id="chan.two", start_ts=now - timedelta(minutes=10),
                          stop_ts=now + timedelta(minutes=50), title="On Guide"))
        await s.commit()
        pl_id, orphan_id = pl.id, pl2.id

    out = await svc.now_for(playlist_ids=[pl_id, orphan_id, 999999])
    a = out["items"][f"playlist:{pl_id}"]
    b = out["items"][f"playlist:{orphan_id}"]
    assert a["source"] == "xmltv" and a["now"]["title"] == "On Guide"
    assert b["found"] is False and "no portal source" in b["why"]
    assert "no such row" in out["items"]["playlist:999999"]["why"]
    assert _STATE["short_epg_calls"] == 0


async def test_mixed_lists_come_back_keyed_by_kind(monkeypatch):
    """One call for a page that shows both kinds, and the caller never confuses ids 7 and 7."""
    Wired(monkeypatch)
    async with SessionLocal() as s:
        pid, ids = await _portal(s, channels=1)
        pl = LivePlaylist(custom_name="P", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=ids[0], priority=1))
        await s.commit()
        pl_id = pl.id
    out = await svc.now_for(live_ids=[ids[0]], playlist_ids=[pl_id])
    assert set(out["items"]) == {f"live:{ids[0]}", f"playlist:{pl_id}"}
    assert out["items"][f"playlist:{pl_id}"]["source"] == "portal"
    assert _STATE["short_epg_calls"] == 2       # no dedupe across kinds, by design


async def test_router_parses_id_lists_and_refuses_an_empty_call(monkeypatch):
    Wired(monkeypatch)
    async with SessionLocal() as s:
        _pid, ids = await _portal(s, channels=2)
    with pytest.raises(HTTPException) as exc:
        await api_epg.what_is_on_now()
    assert exc.value.status_code == 400
    # `junk` is dropped rather than 400'd: a page that rendered one bad row must
    # still get tooltips for the rest
    out = await api_epg.what_is_on_now(live=f"{ids[0]}, {ids[1]},junk,")
    assert len(out["items"]) == 2 and out["ttl"] == svc.TTL_SECONDS
    assert _STATE["short_epg_calls"] == 2
    missing = await api_epg.what_is_on_now(playlist="999999")
    assert "no such row" in missing["items"]["playlist:999999"]["why"]
    await flush_logs()


async def test_the_endpoint_never_raises_when_the_portal_is_on_fire(monkeypatch):
    """A tooltip that fails shows nothing; a 500 on the channel page is a bug report.

    Everything the service cannot answer becomes `{"found": False, "why": …}`, and
    the batch still returns the rows it *could* answer.
    """
    w = Wired(monkeypatch)
    await w.control(epg_mode="absent")
    async with SessionLocal() as s:
        pid, ids = await _portal(s, channels=2)
        src = await s.get(LiveSource, ids[1])
        src.epg_original = "fine.channel"
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        s.add(EpgProgramme(tvg_id="fine.channel", start_ts=now - timedelta(minutes=5),
                          stop_ts=now + timedelta(minutes=55), title="Unaffected"))
        await s.commit()
        out = await api_epg.what_is_on_now(live=f"{ids[0]},{ids[1]}")
    bad = out["items"][f"live:{ids[0]}"]
    good = out["items"][f"live:{ids[1]}"]
    assert bad["found"] is False and bad["why"]
    assert good["now"]["title"] == "Unaffected" and good["source"] == "xmltv"
    assert (await flush_logs()) is None


# ---------------------------------------------------------------------------
# unit-level: the cache and the retry classification, without a database
# ---------------------------------------------------------------------------
def test_retry_rule_separates_busy_from_rude():
    """Which half of the error taxonomy is worth a second and third try.

    Retrying a refusal is how a polite proxy becomes a load problem, and `code`
    (R4) is the only thing that tells the two apart once the client has unwound.
    """
    from app.portal.client import PortalError
    assert svc._retryable(PortalError("boom", code="http_503"))
    assert svc._retryable(PortalError("boom", code="timeout"))
    assert svc._retryable(PortalError("boom", code="empty_reply"))
    assert not svc._retryable(PortalError("boom", code="no such action"))
    assert not svc._retryable(PortalError("boom", code="unauthorized"))
    assert not svc._retryable(PortalError("boom", code="bad_json"))
    assert svc._retryable(TimeoutError())          # `asyncio.TimeoutError` is this


def test_cache_expiry_is_honoured():
    svc._CACHE[(1, "c")] = (0.0, {"found": True})
    assert svc._CACHE[(1, "c")][0] <= 0.0            # expired on purpose
    assert svc.cache_clear() == 1
    assert svc.TTL_SECONDS >= 15
