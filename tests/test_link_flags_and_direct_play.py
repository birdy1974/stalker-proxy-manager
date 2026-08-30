"""
R2: when may we hand the player the link the panel already gave us?

`create_link` is not a lookup, it is a *transaction*: it burns the channel's
`play_token`, and on panels with load balancing it picks a CDN and starts a
billing-side session. For a channel whose own flags say the link is permanent,
calling it on every play buys nothing - but skipping it everywhere else is how a
proxy serves a stale token and a black screen. So the rule lives in one pure
function, and these tests walk its branches, then prove the two stream paths
actually consult it (via the mock's counters - the only witness that survives a
refactor).
"""

from __future__ import annotations

import types

import pytest

from app.database import SessionLocal
from app.models import (LivePlaylist, LivePlaylistSource, LiveSource, MacAddress,
                        Portal)
from app.portal.client import extract_url
from app.portal.links import (FLAG_DISABLE_AD, FLAG_LOAD_BALANCING, FLAG_TMP_LINK,
                              apply_mac_placeholder, has_flag, link_policy,
                              link_request_params, parse_link_flags, plan_for,
                              split_flags, why_not_self_served)

_REPLAY = ("handshakes", "profile_calls", "version_calls", "modules_calls",
           "create_links", "create_link_seen")


@pytest.fixture(autouse=True)
def _fresh_mock():
    """Zero the mock's witnesses before each test, restore them after.

    The counters are module-global, so a test that asserts "no create_link
    happened" is really asserting "none happened since the process started"
    without this - which reads as a failure in whoever runs the suite next.
    """
    saved = {k: _STATE.get(k) for k in _REPLAY}
    _STATE.update({"handshakes": 0, "profile_calls": 0, "version_calls": 0,
                   "modules_calls": 0, "create_links": 0, "create_link_seen": {}})
    yield
    for k, v in saved.items():
        _STATE[k] = v
from app.portal.mock_portal import _STATE, _live_rows
from app.services.db_logging import flush_logs
from app.services.stream_manager import MANAGER
from mockclient import GOOD, PORTAL, Wired

CLEAN = "http://test/mock/ts/1002.ts"


# =========================================================================== #
# what the panel told us about a channel
# =========================================================================== #
@pytest.mark.parametrize("item,want", [
    ({"use_http_tmp_link": 1, "disable_ad": 0}, FLAG_TMP_LINK),
    ({"use_http_tmp_link": 0, "use_load_balancing": 0, "disable_ad": 1}, FLAG_DISABLE_AD),
    ({"use_http_tmp_link": 1, "use_load_balancing": 1}, f"{FLAG_TMP_LINK},{FLAG_LOAD_BALANCING}"),
    ({"use_http_tmp_link": 0, "use_load_balancing": 0, "disable_ad": 0}, ""),
    ({"use_http_tmp_link": "true", "disable_ad": "no"}, FLAG_TMP_LINK),
    # the panel's own "not applicable" is not a "no": it is the absence of data
    ({"use_http_tmp_link": -1, "use_load_balancing": -1, "disable_ad": -1}, None),
    ({"name": "Channel 1"}, None),
    ("not a dict", None),
    (None, None),
])
def test_flags_are_read_out_of_a_catalogue_row(item, want):
    assert parse_link_flags(item) == want


def test_unknown_is_not_the_same_as_nothing_set():
    """NULL means "we were never told"; "" means "we were told: nothing applies".

    One of those is a reason to ask the panel every time, the other is not, and
    a schema that cannot tell them apart loses one of them on the next refactor.
    """
    assert parse_link_flags({"use_http_tmp_link": 0}) == ""
    assert has_flag("", FLAG_TMP_LINK) is False
    assert has_flag(FLAG_TMP_LINK, FLAG_TMP_LINK) is True
    assert split_flags("use_http_tmp_link, disable_ad") == [FLAG_TMP_LINK, FLAG_DISABLE_AD]
    assert split_flags(None) == [] and has_flag(None, FLAG_TMP_LINK) is False


def test_the_stored_string_survives_the_database_and_a_hand_edit():
    assert split_flags(' "use_http_tmp_link" ') == [FLAG_TMP_LINK]
    assert split_flags(FLAG_LOAD_BALANCING.upper()) == [FLAG_LOAD_BALANCING]


# =========================================================================== #
# the URL itself
# =========================================================================== #
@pytest.mark.parametrize("url,why", [
    ("", "no URL"),
    ("ffmpeg http://x/1.ts", "absolute"),
    ("/ch/101.ts", "template"),
    ("http://h/stalker_portal/c/ch/1.ts", "template"),
    ("http://h/x.ts?play_token=abc", "session token"),
    ("http://h/x.ts?Hash=deadbeef", "session token"),
    ("http://h/x.ts?usertoken=1&other=2", "session token"),
])
def test_what_forbids_playing_a_stored_url(url, why):
    assert why in why_not_self_served(url)


def test_a_clean_url_is_playable_as_stored():
    assert why_not_self_served(CLEAN) == ""
    assert why_not_self_served("https://cdn.example.com/live/1.m3u8?c=1") == ""


def test_the_mac_placeholder_is_ours_to_fill_not_a_blocker():
    """`%mac%` looks like a template, but we substitute it ourselves (R4)."""
    assert "%mac%" in CLEAN.replace("/1002.ts", "/%mac%.ts")
    assert why_not_self_served("http://h/x.ts?mac=%mac%") == ""


# =========================================================================== #
# the policy
# =========================================================================== #
def test_a_permanent_link_on_a_known_channel_plays_directly():
    pol = link_policy(url=CLEAN, link_flags="")
    assert pol.direct and "stored link" in pol.reason


@pytest.mark.parametrize("flags,expect", [
    (FLAG_TMP_LINK, "not permanent"),
    (FLAG_LOAD_BALANCING, "not permanent"),
    (f"{FLAG_TMP_LINK},{FLAG_DISABLE_AD}", "not permanent"),
    (FLAG_DISABLE_AD, "stored link"),        # an ad flag is not a staleness flag
])
def test_disable_ad_does_not_gate_a_link(flags, expect):
    """`disable_ad` decides what the panel puts IN the link, not whether it is fresh.

    Reading it as a rebuild flag - which the comparison doc's rule text did, and
    which is the easy mistake from here - would send every ad-free channel
    through create_link on every play for no reason.
    """
    pol = link_policy(url=CLEAN, link_flags=flags)
    assert expect in pol.reason


def test_an_unasked_row_asks():
    pol = link_policy(url=CLEAN, link_flags=None)
    assert pol.create_link and "predates" in pol.reason
    assert pol.flags_known is False


def test_a_session_token_from_fetch_time_always_asks():
    """The rule that protects even a channel whose flags are clean."""
    pol = link_policy(url="http://test/mock/ts/1.ts?play_token=stale", link_flags="")
    assert pol.create_link
    assert "session token" in pol.reason


@pytest.mark.parametrize("kwargs,expect", [
    ({"ffmpeg": True}, "ffmpeg owns this stream"),
    ({"force_ch_link_check": True}, "force_ch_link_check"),
    ({"allow_direct": False}, "ask for every link"),
])
def test_the_three_overrides_come_before_the_fast_path(kwargs, expect):
    pol = link_policy(url=CLEAN, link_flags="", **kwargs)
    assert pol.create_link and expect in pol.reason


def test_a_template_cmd_asks_even_when_the_flags_are_clean():
    for cmd in ("/ch/101.ts", "http://h/stalker_portal/c/ch/1.ts"):
        pol = link_policy(url=cmd, link_flags="")
        assert pol.create_link and "template" in pol.reason


def test_a_relative_or_rtsp_url_asks():
    assert link_policy(url="udp://239.0.0.1:1234", link_flags="").create_link
    assert link_policy(url="", link_flags="").create_link


# =========================================================================== #
# reading a source row and a MAC row
# =========================================================================== #
class _Src:
    def __init__(self, cmd, link_flags=None):
        self.cmd = cmd
        self.link_flags = link_flags


class _Mac:
    def __init__(self, mac=GOOD, force_ch_link_check=False):
        self.mac = mac
        self.force_ch_link_check = force_ch_link_check


def test_the_plan_carries_both_the_stored_url_and_the_cmd_to_ask_with():
    plan = plan_for(_Src(f"ffmpeg {CLEAN}"), _Mac())
    assert plan.policy.create_link, "unknown flags: ask"
    assert plan.cmd == f"ffmpeg {CLEAN}", "create_link wants the WHOLE cmd"
    assert plan.url == CLEAN

    clean = plan_for(_Src(f"ffmpeg {CLEAN}", link_flags=""), _Mac())
    assert clean.policy.direct
    assert clean.direct_url == CLEAN

    masked = plan_for(_Src("ffmpeg http://test/mock/ts/%mac%.ts", link_flags=""), _Mac())
    assert masked.direct_url == f"http://test/mock/ts/{GOOD}.ts"
    assert apply_mac_placeholder("http://h/x?m=%25MAC%25", GOOD).endswith(GOOD)


def test_a_mac_the_panel_is_wary_of_overrides_a_clean_channel():
    mac = _Mac(force_ch_link_check=True)
    plan = plan_for(_Src(f"ffmpeg {CLEAN}", link_flags=""), mac)
    assert plan.policy.create_link and plan.request_kwargs()["force_ch_link_check"]


def test_direct_play_never_builds_a_request():
    plan = plan_for(_Src(f"ffmpeg {CLEAN}", link_flags=""), _Mac())
    assert plan.policy.direct
    # `request_kwargs` exists for the asking path only; a direct play must not
    # turn into "well, one small HEAD request first" by construction.
    assert plan.request_kwargs() == {"link_flags": "", "force_ch_link_check": False}


# =========================================================================== #
# what create_link is asked for
# =========================================================================== #
def test_the_channels_flags_reach_the_request():
    assert link_request_params(link_flags=FLAG_DISABLE_AD, force_ch_link_check=False) == {
        "series": "0", "forced_storage": "false", "disable_ad": "true",
        "download": "false", "force_ch_link_check": "false"}
    p = link_request_params(link_flags=None, force_ch_link_check=True, series=True)
    assert p["disable_ad"] == "false" and p["force_ch_link_check"] == "true"
    assert p["series"] == "1"


async def test_the_mock_portal_sees_the_parameters(monkeypatch):
    """Not "we built a dict" - what arrived on the wire."""
    w = Wired(monkeypatch)
    client = w.client(GOOD)
    await client.handshake()
    await client.create_link(f"ffmpeg {CLEAN}", "live",
                             link_flags=f"{FLAG_TMP_LINK},{FLAG_DISABLE_AD}")
    seen = (await w.state())["seen_create_link"]
    assert seen["disable_ad"] == "true"
    assert seen["force_ch_link_check"] == "false"
    assert seen["cmd"] == f"ffmpeg {CLEAN}"


async def test_force_ch_link_check_is_forwarded_not_just_stored(monkeypatch):
    """R3 stored this per-MAC flag for two rounds without using it."""
    w = Wired(monkeypatch)
    client = w.client(GOOD)
    await client.handshake()
    await client.create_link(f"ffmpeg {CLEAN}", "vod",
                             link_flags=None, force_ch_link_check=True)
    seen = (await w.state())["seen_create_link"]
    assert seen["force_ch_link_check"] == "true"


# =========================================================================== #
# the stream paths consult the policy
# =========================================================================== #
async def _channel(link_flags, *, direct_links=True, force=False, cmd=f"ffmpeg {CLEAN}"):
    async with SessionLocal() as s:
        p = Portal(name="p", base_url="http://test/mock/c/", enabled=True,
                   resolved_url=PORTAL, direct_links=direct_links)
        s.add(p)
        await s.flush()
        s.add(MacAddress(portal_id=p.id, mac=GOOD, order=0, force_ch_link_check=force))
        src = LiveSource(portal_id=p.id, portal_channel_id="1", original_name="Ch",
                         cmd=cmd, enabled=True, link_flags=link_flags)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Ch", enabled=True)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        await s.commit()
        return pl.id


async def test_a_permanent_channel_touches_the_portal_not_at_all(monkeypatch):
    """The point of R2, asserted where it matters.

    `handshakes == 0` is the strong half of this: the old code needed a token to
    ask for a link it then discarded, so a play that needs *no* portal request
    must be visible as no portal request at all.
    """
    w = Wired(monkeypatch)
    pl = await _channel("")
    url, name = await MANAGER.resolve("live", pl)
    assert url == CLEAN and name == "Ch"
    state = await w.state()
    assert state["counters"]["create_links"] == 0
    assert state["counters"]["handshakes"] == 0
    await flush_logs()


async def test_a_tmp_link_channel_asks_and_gets_the_panels_answer(monkeypatch):
    w = Wired(monkeypatch)
    pl = await _channel(FLAG_TMP_LINK)
    url, _name = await MANAGER.resolve("live", pl)
    assert url == CLEAN, "the mock rebuilds the same URL; a real panel hands out a new one"
    state = await w.state()
    assert state["counters"]["create_links"] == 1
    assert state["counters"]["handshakes"] >= 1
    await flush_logs()


async def test_an_unfetched_row_behaves_exactly_as_before(monkeypatch):
    """NULL flags (a database migrated but not re-fetched) => still create_link."""
    w = Wired(monkeypatch)
    pl = await _channel(None)
    await MANAGER.resolve("live", pl)
    assert (await w.state())["counters"]["create_links"] == 1
    await flush_logs()


async def test_a_portal_that_distrusts_its_flags_asks_every_time(monkeypatch):
    w = Wired(monkeypatch)
    pl = await _channel("", direct_links=False)
    url, _ = await MANAGER.resolve("live", pl)
    assert url == CLEAN
    assert (await w.state())["counters"]["create_links"] == 1
    await flush_logs()


async def test_a_mac_flag_overrides_a_clean_channel(monkeypatch):
    w = Wired(monkeypatch)
    pl = await _channel("", force=True)
    await MANAGER.resolve("live", pl)
    assert (await w.state())["counters"]["create_links"] == 1
    await flush_logs()


async def test_a_stale_token_in_the_url_overrides_clean_flags(monkeypatch):
    """The guard that makes the fast path safe enough to be the default."""
    w = Wired(monkeypatch)
    pl = await _channel("", cmd=f"ffmpeg {CLEAN}?play_token=from-the-fetch")
    await MANAGER.resolve("live", pl)
    assert (await w.state())["counters"]["create_links"] == 1
    await flush_logs()


def test_the_transcode_path_never_takes_the_shortcut():
    """An ffmpeg pipe needs the request for its own sake: it is the liveness
    probe the fallback chain is built on, and it hands ffmpeg a fresh token."""
    plan = plan_for(_Src(f"ffmpeg {CLEAN}", link_flags=""), _Mac(), ffmpeg=True)
    assert plan.policy.create_link and "ffmpeg" in plan.policy.reason


async def test_the_reason_reaches_the_stream_log(monkeypatch):
    """"We skipped the portal" is only diagnosable if it says why it skipped."""
    from app.services import stream_manager

    lines: list[tuple[str, str]] = []

    async def fake(level, module, message, **kw):
        lines.append((level, message))

    monkeypatch.setattr(stream_manager, "db_log", fake)
    pl = await _channel("")
    url, _name = await stream_manager.MANAGER.resolve("live", pl)
    assert url == CLEAN
    assert lines and lines[0][0] == "INFO"
    assert "playing the stored link" in lines[0][1]
    assert link_policy(url=CLEAN, link_flags="").reason in lines[0][1]


# =========================================================================== #
# the fetch stores the flags, and the mock keeps proving all of it
# =========================================================================== #
def test_a_fetch_stores_what_the_panel_said():
    from app.services.fetch_jobs import _live_fields, _vod_fields

    row = types.SimpleNamespace()
    _live_fields(row, {"name": "Ch", "cmd": f"ffmpeg {CLEAN}",
                       "use_http_tmp_link": "1", "disable_ad": "1"}, 7)
    assert row.link_flags == f"{FLAG_TMP_LINK},{FLAG_DISABLE_AD}"

    vrow = types.SimpleNamespace()
    _vod_fields(vrow, {"name": "Movie", "cmd": f"ffmpeg {CLEAN}"}, 3)
    assert vrow.link_flags is None, "never told, not 'nothing set'"


def test_the_mock_catalogue_still_covers_every_shape():
    """The fixture is the test's only source of flagged channels.

    If someone tidies `_LIVE_SHAPES` down to one row, every fast-path assertion
    above quietly stops exercising the policy, so the variety is asserted here
    instead of being trusted.
    """
    rows = _live_rows("http://test")
    flags = [parse_link_flags(r) for r in rows]
    assert "" in flags, "a permanent channel"
    assert any(has_flag(f, FLAG_TMP_LINK) for f in flags), "a tmp-link channel"
    assert None in flags, "a channel the panel said nothing about"
    assert any(FLAG_LOAD_BALANCING in (f or "") for f in flags), "a load-balanced one"
    # `why_not_self_served` takes the URL a player would be handed, so the test
    # extracts it the same way `plan_for` does - a cmd is not a URL, and a policy
    # that quietly accepted one would call a tokenised stream "clean".
    tokenised = [r for r in rows
                 if "session token" in why_not_self_served(extract_url(r["cmd"]))]
    assert tokenised, "a permanent-looking URL that still carries a play_token"


async def test_the_mock_state_reports_the_new_counters():
    state = _STATE
    for key in ("version_calls", "modules_calls", "create_links", "create_link_seen"):
        assert key in state, f"/mock/_state is blind to {key}"


# =========================================================================== #
# the GUI's escape hatch is a real column with a real default
# =========================================================================== #
async def test_the_sources_api_shows_what_the_panel_said():
    """"This channel skips create_link because its flags say so" must be checkable
    without reading the database file - the row is where a support thread looks."""
    from sqlalchemy import select

    from app.routers import api_sources

    async with SessionLocal() as s:
        p = Portal(name="flagged", base_url="http://test/mock/c/", enabled=True,
                   resolved_url=PORTAL)
        s.add(p)
        await s.flush()
        s.add(LiveSource(portal_id=p.id, portal_channel_id="1", original_name="Ch",
                         cmd=f"ffmpeg {CLEAN}", enabled=True,
                         link_flags=f"{FLAG_TMP_LINK},{FLAG_DISABLE_AD}"))
        await s.commit()
        src = (await s.execute(select(LiveSource))).scalars().one()
        item = api_sources._live_item(src, {}, {p.id: "flagged"})
    assert item["link_flags"] == f"{FLAG_TMP_LINK},{FLAG_DISABLE_AD}"
    assert item["cmd"] == f"ffmpeg {CLEAN}"


async def test_direct_links_defaults_on_and_is_persisted():
    async with SessionLocal() as s:
        p = Portal(name="default-on", base_url="http://x/c/")
        s.add(p)
        await s.commit()
        pid = p.id
    async with SessionLocal() as s:
        assert (await s.get(Portal, pid)).direct_links is True
        p = await s.get(Portal, pid)
        p.direct_links = False
        await s.commit()
    async with SessionLocal() as s:
        assert (await s.get(Portal, pid)).direct_links is False


async def test_the_update_endpoint_accepts_the_switch(monkeypatch):
    """A GUI checkbox that the API ignores is a GUI checkbox that does nothing."""
    from app.routers import api_portals

    _wired = Wired(monkeypatch)   # the patching IS the point
    async with SessionLocal() as s:
        p = Portal(name="switch", base_url="http://test/mock/c/", enabled=True)
        s.add(p)
        await s.commit()
        pid = p.id
    async with SessionLocal() as s:
        await api_portals.update_portal(pid, {"direct_links": False}, db=s)
        p = await s.get(Portal, pid)
        assert p.direct_links is False
        row = api_portals._portal_row(p, [])
        assert row["direct_links"] is False
    async with SessionLocal() as s:
        await api_portals.update_portal(pid, {"direct_links": True}, db=s)
        assert (await s.get(Portal, pid)).direct_links is True
    await flush_logs()
