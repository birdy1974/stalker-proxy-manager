"""
R7 - the MAC -> Xtream bridge: detect, offer, adopt, detach.

The premise (docs/ESTALKER-COMPARISON.md §R7, from EStalker's
`Extensions/EStalker/playerapi.py` + `playlists.py:1020`): some Stalker panels are
a front for an ordinary Xtream account, and they say so *in the stream link they
hand out* - `create_link` answers `/movie/john/SECRET/12345.mp4` instead of a token
URL. That is worth detecting, because an Xtream URL needs no MAC session, no
`create_link` per play and no connection slot; and it is worth doing carefully,
because the same string is a live credential for somebody's subscription.

What these tests pin down, in order of how much it would hurt to get wrong:

1. **harvesting is exact or it is nothing** - the four path shapes, an `ffmpeg `
   prefix, percent-encoding, and the 32-hex-token refusal (that is a link
   signature, not an account, and adopting it is a playlist that dies at 3 a.m.);
2. **the account's own words** - `{"user_info": []}` means "wrong password", HTML
   means "refused", and `exp_date`/`status`/`active_cons` come back readable;
3. **matching never guesses** - channel number first, name second, an ambiguous
   name reported instead of resolved;
4. **nothing is automatic** - `probe` stores an observation and changes no playback;
   only `adopt` flips the flag, and it refuses an expired account;
5. **the password stays out of every response** - stored unmasked (a backup that
   restored `****` is a silently dead bridge), masked everywhere else;
6. **an adopted channel really bypasses the portal path** - `resolve()` returns the
   Xtream URL and the panel is not asked for a link.

Network parts run against the built-in mock portal (`xtream_mode` /
`/player_api.php` / `/live/<user>/<pass>/<id>.ts`), so the credential that
`create_link` handed out is the same one the media route accepts - a test that only
checked the string we built would pass even if that URL were nonsense.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import SessionLocal
from app.models import LivePlaylist, LivePlaylistSource, LiveSource, MacAddress, Portal, VodSource
from app.portal.mock_portal import _STATE, _live_rows, _vod_rows
from app.portal.xtream import (XtreamCreds, harvest, mask_password, parse_player_api,
                               parse_streams, plan_adoption, xtream_base)
from app.services import xtream_bridge
from app.services.db_logging import flush_logs
from mockclient import GOOD, PORTAL, Wired

USER, PW = "mockuser", "mockpass123"
#: the R7/R9 half of the mock's state; the counters are module globals and
#: cumulative, so an assertion that something did *not* happen needs them zeroed
_R7_KEYS = ("xtream_mode", "xtream_user", "xtream_pass", "xtream_exp_days",
            "xtream_status", "xtream_refuse", "epg_mode")


@pytest.fixture(autouse=True)
def _fresh_mock():
    saved = {k: _STATE.get(k) for k in _R7_KEYS}
    saved_counters = {k: _STATE.get(k) for k in
                      ("player_api_calls", "short_epg_calls", "flaky_hits",
                       "seen_player_api", "create_links", "create_link_seen")}
    for k in _R7_KEYS:
        _STATE[k] = {"xtream_mode": "off", "xtream_user": USER, "xtream_pass": PW,
                     "xtream_exp_days": 30, "xtream_status": "Active",
                     "xtream_refuse": False, "epg_mode": "normal"}[k]
    _STATE["player_api_calls"] = 0
    _STATE["short_epg_calls"] = 0
    _STATE["flaky_hits"] = 0
    _STATE["seen_player_api"] = {}
    _STATE["create_links"] = 0
    _STATE["create_link_seen"] = {}
    yield
    _STATE.update(saved)
    _STATE.update(saved_counters)


def creds(base="http://xt.test:8080", user="john", pw="s3cr3t", kind="movie"):
    return XtreamCreds(base=base, username=user, password=pw, kind=kind)


# =========================================================================== #
# 1. harvest: the credential is in the link, or it is not
# =========================================================================== #
@pytest.mark.parametrize("link,kind", [
    ("http://p.test/movie/john/s3cr3t/12345.mp4", "movie"),
    ("http://p.test/live/john/s3cr3t/999.ts", "live"),
    ("http://p.test/series/john/s3cr3t/77.mp4", "series"),
    ("http://p.test/vod/john/s3cr3t/77.mkv", "movie"),   # /vod/ is the movie path
    ("http://p.test/streaming/live/john/s3cr3t/3.ts", "live"),
    ("ffmpeg http://p.test/live/john/s3cr3t/999.ts -f mpegts pipe:1", "live"),
])
def test_harvest_finds_the_account_in_every_shape_a_panel_uses(link, kind):
    got = harvest(link)
    assert got is not None and (got.base, got.username, got.password) == \
        ("http://p.test", "john", "s3cr3t")
    # the segment is kept: a panel that only exposed /movie/ may still refuse /live/
    assert got.kind == kind


@pytest.mark.parametrize("link", [
    "http://p.test/mock/ts/1001.ts",                      # our own shape: no creds
    "http://p.test/live/john/0123456789abcdef0123456789abcdef/9.ts",  # a play_token
    "http://p.test/live//9.ts",                            # empty credential
    "ffmpeg -i pipe:0 -c copy out.ts",                     # a command, not a link
    "",
])
def test_harvest_refuses_when_there_is_nothing_safe_to_adopt(link):
    """Silence here is the correct answer for three different reasons.

    A panel that hands out token URLs has no Xtream side (the ordinary case); a
    32-hex string in the password slot is a *link signature*, short-lived, and
    adopting it is how a whole playlist goes dead at once; and an empty credential
    is an IP-locked account we must not pretend to own.
    """
    assert harvest(link) is None


def test_harvest_decodes_a_credential_and_the_builders_re_encode_it():
    """Round trip, because a password with punctuation is not exotic.

    Truncating at the first `%` instead would harvest a *wrong* secret and get us
    refused by the media host - which is how a panel starts thinking we are
    brute-forcing it. Garbage escapes are the opposite case: no match at all, since
    guessing half a password is worse than not adopting.
    """
    got = harvest("http://p.test/live/j%6Fn/pa%2Fss/1.ts")
    assert got is not None and got.username == "jon" and got.password == "pa/ss"
    assert got.stream_url("1", "live") == "http://p.test/live/jon/pa%2Fss/1.ts"
    assert harvest("http://p.test/live/john/100%25/9.ts").password == "100%"
    assert harvest("http://p.test/live/john/pa%ss/9.ts") is None


@pytest.mark.parametrize("link,want", [
    ("http://h/streaming/live/jon/pw/1.ts", ("http://h", "live", "jon", "pw")),
    ("http://localhost/live/jon/pw/1.ts", ("http://localhost", "live", "jon", "pw")),
    ("http://h/mock/streaming/movie/jon/pw/9.mkv", ("http://h/mock", "movie", "jon", "pw")),
    ("http://h:8080/portal/vod/jon/pw/9.mkv", ("http://h:8080/portal", "movie", "jon", "pw")),
])
def test_a_short_hostname_is_not_the_credential_path(link, want):
    """`streaming` is a wrapper, never a kind, and no path is ever the username.

    While the pattern allowed `streaming` as a kind *and* any `[a-z_]+` as a
    wrapper, `http://h/streaming/live/u/p/1.ts` matched with `h` as the wrapper and
    `streaming` as the kind: user=`live`, password=`u`. A wrong credential sent to a
    paying panel is worse than none - it is a failed-auth pattern on an account that
    works - and short host names (`h`, `server`, `localhost`) were exactly what hit
    it. This test exists because the running demo found it, not because a unit test
    could have.
    """
    got = harvest(link)
    assert got is not None and (got.base, got.kind, got.username, got.password) == want


def test_the_base_keeps_the_path_prefix_a_panel_streams_from():
    """A server that answers `/xtream/live/<u>/<p>/…` also serves `/xtream/player_api.php`.

    Stripping to the origin - what EStalker does - takes the harvested password to a
    vhost that never issued it, and the failure looks like "this panel has no
    Xtream". `streaming` is the one segment that is *not* a prefix.
    """
    c = harvest("http://h/mock/live/jon/pw/1.ts")
    assert c.base == "http://h/mock"
    assert c.api_url("get_live_streams").startswith("http://h/mock/player_api.php?")
    assert c.stream_url("1", "live") == "http://h/mock/live/jon/pw/1.ts"
    assert harvest("http://h/streaming/live/jon/pw/1.ts").base == "http://h"


def test_a_media_host_keeps_its_prefix_too():
    """`http_live_url` is a *media root*: drop the trailing kind, keep the rest."""
    from app.portal.xtream import media_base
    assert media_base("http://h:8000/live") == "http://h:8000"
    assert media_base("http://h:8000/live/") == "http://h:8000"
    assert media_base("http://h/mock/live") == "http://h/mock"
    assert media_base("http://h/streaming/live") == "http://h"
    assert media_base("") == "" and media_base("nonsense") == ""
    acc = parse_player_api({"server_info": {"http_live_url": "http://h/mock/live"},
                            "user_info": {"status": "Active", "auth": 1}})
    assert acc.base == "http://h/mock"


def test_harvest_handles_a_relative_link_a_panel_handed_out():
    """`create_link` sometimes answers `/movie/john/pw/1.mp4` with no origin.

    There is nothing to call in that answer, and saying so (None) is better than
    inventing a host from the portal URL and then querying some other server with a
    borrowed password.
    """
    assert harvest("/movie/john/s3cr3t/1.mp4") is None


def test_base_is_the_origin_only():
    assert xtream_base("http://p.test:8080/stalker_portal/c/portal.php?x=1") == \
        "http://p.test:8080"
    assert xtream_base("not a url") == ""


# =========================================================================== #
# 2. what the credential can be asked for, and what must not leak
# =========================================================================== #
def test_credential_urls_are_the_four_the_user_needs():
    c = creds()
    assert c.api_url("get_live_streams") == (
        "http://xt.test:8080/player_api.php?username=john&password=s3cr3t"
        "&action=get_live_streams")
    assert c.stream_url("42", "live") == "http://xt.test:8080/live/john/s3cr3t/42.ts"
    assert c.stream_url("42", "vod", "mkv") == "http://xt.test:8080/movie/john/s3cr3t/42.mkv"
    assert "type=m3u_plus" in c.playlist_url()
    assert "xmltv.php" in c.epg_url()


def test_public_forms_never_carry_the_password():
    c = creds(pw="supersecret")
    pub = c.public()
    assert pub["password"] == "****" and pub["has_password"] is True
    for key in ("api_url", "playlist_url", "epg_url"):
        assert "supersecret" not in pub[key], key
    assert "su***" in pub["api_url"]      # a hint of it, so a user can match an account
    assert "supersecret" not in c.masked() and "/john/***/" in c.masked()
    assert "supersecret" not in json.dumps(pub)


def test_mask_password_masks_both_credential_forms():
    assert "abc" not in mask_password("http://h/live/u/abcdef123/1.ts")
    masked = mask_password("http://h/get.php?username=u&password=abcdef123&type=m3u")
    assert "abcdef123" not in masked and "username=u" in masked
    # a short password has nothing worth keeping, so it is masked whole
    assert mask_password("http://h/live/u/ab/1.ts").endswith("/u/***/1.ts")


# =========================================================================== #
# 3. player_api.php: the account's own words
# =========================================================================== #
def test_player_api_reads_the_fields_that_matter_for_an_offer():
    acc = parse_player_api({
        "server_info": {"status": "Active", "http_live_url": "http://media.test:8000/live",
                        "hostname": "media.test", "http_port": 8000},
        "user_info": {"username": "john", "status": "Active", "exp_date": 1767225600,
                      "created_at": 1700000000, "active_cons": 2, "max_connections": 3,
                      "is_trial": "1", "auth": 1}})
    assert acc.status == "online" and acc.auth == 1 and acc.is_trial is True
    assert (acc.active_cons, acc.max_cons) == (2, 3)
    assert acc.exp_date.endswith("UTC") and acc.created_at
    # the media host wins over the portal host, and says where it came from
    assert acc.base == "http://media.test:8000"
    assert acc.public()["max_connections"] == 3


@pytest.mark.parametrize("payload,want_status", [
    ({"user_info": []}, "error"),           # how an Xtream server says "wrong password"
    ({"user_info": {"status": "Expired", "exp_date": 1700000000}}, "expired"),
    ({"user_info": {"status": "Banned"}}, "banned"),
    ({"user_info": {"status": "Disabled"}}, "banned"),
    ({"errors": "Unauthorized access"}, "error"),
])
def test_refusals_are_refusals_not_empty_accounts(payload, want_status):
    acc = parse_player_api(payload)
    assert acc.status == want_status
    assert acc.error or acc.status_raw


def test_html_from_player_api_is_reported_as_a_refusal():
    acc = parse_player_api("<html><body>403 Forbidden</body></html>")
    assert acc.status == "error" and "refused" in acc.error
    assert parse_player_api(None).status == "error"
    assert parse_player_api({"user_info": {}}).status == "error"


def test_streams_are_normalised_without_losing_the_id_type():
    rows = parse_streams([{"stream_id": 55, "name": "One", "num": "1", "container_extension": "ts"},
                          {"movie_id": "abc", "name": "Two", "extension": "mkv"},
                          {"name": "no id at all"},
                          "junk"])
    assert [(r["stream_id"], r["name"], r["extension"]) for r in rows] == [
        ("55", "One", "ts"), ("abc", "Two", "mkv")]
    assert parse_streams({"data": [{"series_id": 7, "name": "S"}]})[0]["stream_id"] == "7"
    assert parse_streams(None) == []


# =========================================================================== #
# 4. the matcher: number, then name, never a guess
# =========================================================================== #
def test_adoption_prefers_the_channel_number_over_a_different_name():
    plan = plan_adoption([{"id": 1, "number": "101", "name": "Sky One (portal name)"}],
                         [{"stream_id": "9000", "num": "101", "name": "Sky Rebranded",
                           "container_extension": "ts"}],
                         creds(), kind="live")
    assert plan.matched == 1 and plan.by_number == 1 and plan.by_name == 0
    assert plan.urls[1].endswith("/live/john/s3cr3t/9000.ts")


def test_adoption_falls_back_to_the_name_and_reports_ambiguity_instead_of_choosing():
    streams = [{"stream_id": "1", "num": "", "name": "Sky Sports", "container_extension": "ts"},
               {"stream_id": "2", "num": "", "name": "Sky Sports", "container_extension": "ts"},
               {"stream_id": "3", "num": "", "name": "Box Office", "container_extension": "ts"}]
    plan = plan_adoption([{"id": 10, "number": "", "name": "Sky Sports"},
                          {"id": 11, "number": "", "name": "box   office "}],
                         streams, creds(), kind="live")
    assert 11 in plan.urls and plan.by_name == 1        # names are normalised
    assert 10 not in plan.urls                          # two candidates: not our call
    # the report is a sentence, not an id: it is printed in a tooltip next to a
    # button that says "12 channels matched", and "Sky Sports" alone explains nothing
    assert plan.ambiguous == ["Sky Sports (2 streams share that name)"]
    assert plan.unmatched == []


def test_an_unmatched_channel_is_reported_not_invented():
    plan = plan_adoption([{"id": 1, "number": "77", "name": "Local Channel"}],
                         [{"stream_id": "9", "num": "78", "name": "Other"}],
                         creds(), kind="live")
    assert plan.matched == 0 and plan.unmatched == ["Local Channel"]
    pub = plan.public()
    assert pub["unmatched_total"] == 1 and "s3cr3t" not in json.dumps(pub)


def test_a_number_collision_falls_through_to_the_name_rule():
    """Both sides can be wrong; the safe answer is the one that needs less luck."""
    plan = plan_adoption([{"id": 1, "number": "5", "name": "Right Name"}],
                         [{"stream_id": "a", "num": "5", "name": "Wrong One"},
                          {"stream_id": "b", "num": "9", "name": "Right Name"}],
                         creds(), kind="live")
    # number 5 is unique on our side and on theirs, so it is still the number that wins
    assert plan.urls[1].endswith("/a.ts") and plan.by_number == 1
    plan2 = plan_adoption([{"id": 1, "number": "5", "name": "Right Name"},
                           {"id": 2, "number": "5", "name": "Other"}],
                          [{"stream_id": "b", "num": "9", "name": "Right Name"}],
                          creds(), kind="live")
    assert plan2.urls[1].endswith("/b.ts") and plan2.by_name == 1
    assert plan2.unmatched == ["Other"]


# =========================================================================== #
# 5. the stored observation
# =========================================================================== #
def test_observation_round_trips_and_a_masked_backup_is_not_a_credential():
    text = xtream_bridge.dumps_observation(creds(), parse_player_api(
        {"user_info": {"status": "Active", "max_connections": 2, "auth": 1}}), why="ok")
    obs = xtream_bridge.loads_observation(text)
    assert obs["found"] is True and json.loads(text)["creds"]["password"] == "s3cr3t"
    assert xtream_bridge.creds_from_observation(obs) == creds()
    pub = xtream_bridge.public_observation(obs)
    assert "s3cr3t" not in json.dumps(pub) and pub["account"]["status"] == "online"
    # a backup whose password was masked must not be mistaken for a usable account
    masked = json.loads(text)
    masked["creds"]["password"] = "****"
    assert xtream_bridge.creds_from_observation(masked) is None
    assert xtream_bridge.loads_observation("{ not json") == {}
    assert xtream_bridge.loads_observation("[1,2]") == {}
    assert xtream_bridge.public_observation({"found": False, "why": "nope"}) == \
        {"found": False, "why": "nope"}


# =========================================================================== #
# 6. probe: through the real client, against the mock panel
# =========================================================================== #
async def _seed(db, *, name="bridge", xtream_adopted=False, direct_links=True,
                live_rows=12, mac=GOOD, vod=True):
    """A portal + MAC + fetched rows, in the shape `fetch_jobs` would have left them."""
    p = Portal(name=name, base_url="http://test/mock/c/", resolved_url=PORTAL,
               enabled=True, xtream_adopted=xtream_adopted, direct_links=direct_links)
    db.add(p)
    await db.flush()
    if mac:
        db.add(MacAddress(portal_id=p.id, mac=mac, order=0, status="online", online=True))
    rows = _live_rows("http://mock/")[:live_rows]
    for row in rows:
        db.add(LiveSource(portal_id=p.id, portal_channel_id=str(row["id"]),
                          original_name=row["name"], number=row["number"],
                          cmd=f"ffmpeg http://mock/ts/{row['id']}.ts", enabled=True))
    if vod:
        vrow = _vod_rows()[0]
        db.add(VodSource(portal_id=p.id, portal_item_id=str(vrow["id"]),
                         original_name=vrow["name"],
                         cmd=f"http://mock/vod/{vrow['id']}.mp4", enabled=True))
    await db.commit()
    return p.id


@pytest.mark.parametrize("mode,expect_found", [("off", False), ("on", True)])
async def test_probe_reports_what_the_panel_handed_out(monkeypatch, mode, expect_found):
    """The offer exists only when the link carries an account - and the refusal is stored too.

    `why` on a portal with nothing to find matters: the GUI has to tell "this panel
    does not do Xtream" apart from "we never looked", or the button is a mystery.
    """
    w = Wired(monkeypatch)
    await w.control(xtream_mode=mode)
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=2)
        p = await s.get(Portal, pid)
        out = await xtream_bridge.probe(s, p)
    assert out["found"] is expect_found
    # `why` is the sentence the GUI prints: a reason in both directions
    assert out["why"] and ("no Xtream credentials" in out["why"]) is (not expect_found)
    # the property is "the secret is not in there", not "the word is not in there":
    # a response field literally named `password` and holding `****` is the point
    assert PW not in json.dumps(out)
    if expect_found:
        assert out["creds"]["password"] == "****" and out["creds"]["username"] == USER
        assert out["creds"]["base"] == "http://test/mock"
        assert PW not in out["creds"]["api_url"] and USER in out["creds"]["api_url"]
    async with SessionLocal() as s:
        p = await s.get(Portal, pid)
        stored = json.loads(p.xtream)
    if expect_found:
        # stored in full, because a masked password in the database is a bridge
        # that cannot bridge; every *response* masks it instead (the next assertion).
        # `/mock/` in the base is the app's own layout: our output API owns the
        # root-level Xtream paths, so the mock's panel side lives under its prefix -
        # which is also how a panel behind a path prefix behaves, exercised here
        assert stored["creds"]["base"] == "http://test/mock"   # the panel's own prefix
        assert stored["creds"]["password"] == PW
        assert stored["account"]["status"] == "online"
        assert stored["account"]["max_connections"] == 3
    else:
        assert stored["found"] is False
    await flush_logs()


async def test_probe_is_not_repeated_until_asked(monkeypatch):
    """One create_link per detection, and a second press without `force` costs nothing.

    The harvest is a real authenticated request on somebody's account. A page load
    that re-probed would be the beginning of a very short friendship with the panel.
    """
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=2)
        p = await s.get(Portal, pid)
        first = await xtream_bridge.probe(s, p)
        links_after_first = _STATE["create_links"]
        second = await xtream_bridge.probe(s, p)
        assert second["cached"] is True
        assert _STATE["create_links"] == links_after_first
        third = await xtream_bridge.probe(s, p, force=True)
        assert _STATE["create_links"] > links_after_first
    assert first["found"] is True and "press Detect again" in second["why"]
    assert second["why"].startswith(first["why"])     # the reason survives the cache
    assert "cached" not in third
    await flush_logs()


async def test_probe_refuses_to_adopt_a_bare_ip_locked_panel_and_stores_the_reason(monkeypatch):
    """The mock's default mode answers a token-free link with no credential at all."""
    Wired(monkeypatch)          # installs the mock transport; no control needed
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1)
        p = await s.get(Portal, pid)
        out = await xtream_bridge.probe(s, p)
        assert out["found"] is False and out["why"]
        assert p.xtream_adopted is False
        assert p.xtream_at is not None      # we did look, and we say when


async def test_probe_needs_a_mac_and_a_live_portal(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1, mac="")
        p = await s.get(Portal, pid)
        out = await xtream_bridge.probe(s, p)
        assert out["found"] is False and "no MAC" in out["why"]
        p.enabled = False
        await s.commit()
        out2 = await xtream_bridge.probe(s, p)
        assert out2["found"] is False and out2["probed"] is False


async def test_a_refusing_panel_is_not_a_bridge(monkeypatch):
    """`player_api.php` answering `{"user_info": []}` means those credentials are wrong.

    The harvest still found a credential in the link (so `found` is True), but the
    *offer* has to say the account is not usable, and `adopt` must then refuse -
    that combination is the whole reason this test exists.
    """
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on", xtream_refuse=True)
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=2)
        p = await s.get(Portal, pid)
        out = await xtream_bridge.probe(s, p)
        assert out["found"] is True
        assert out["account"]["status"] == "error"
        adopted = await xtream_bridge.adopt(s, p)
        assert adopted["ok"] is False and "would not confirm" in adopted["error"]
        assert adopted["ok"] is False and "force=1" in adopted["error"]
        forced = await xtream_bridge.adopt(s, p, force=True)
        # force=1 means "I know this panel, do it" - it must not then invent matches
        assert forced["ok"] is True and forced["adopted"] >= 0
    await flush_logs()


async def test_expired_account_needs_an_explicit_force(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on", xtream_status="Expired", xtream_exp_days=-1)
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=2)
        p = await s.get(Portal, pid)
        await xtream_bridge.probe(s, p)
        refused = await xtream_bridge.adopt(s, p)
        assert refused["ok"] is False and "expired" in refused["error"] and "force=1" in refused["error"]
        assert p.xtream_adopted is False


# =========================================================================== #
# 7. adopt / detach: the switch that changes playback
# =========================================================================== #
async def test_adopt_writes_one_url_per_matched_channel_and_keeps_the_portal_cmd(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=12)
        p = await s.get(Portal, pid)
        await xtream_bridge.probe(s, p)
        out = await xtream_bridge.adopt(s, p)
    assert out["ok"] is True and out["adopted"] >= 10
    assert out["kinds"]["live"]["unmatched"]      # every 11th channel is not on Xtream
    assert set(out["kinds"]["live"]) >= {"matched", "by_number", "by_name", "unmatched",
                                         "ambiguous", "ambiguous_total"}
    async with SessionLocal() as s:
        rows = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().all()
        with_url = [r for r in rows if r.xtream_url]
        assert with_url and all(r.xtream_url.startswith(f"http://test/mock/live/{USER}/{PW}/")
                                for r in with_url)
        # the portal's own cmd survives next to it: detaching is a flag, not a re-fetch
        assert all(r.cmd.startswith("ffmpeg ") for r in rows)
        p = await s.get(Portal, pid)
        assert p.xtream_adopted is True
        obs = json.loads(p.xtream)
        assert obs["adopted_at"] and obs["adopt"]["live"]["matched"] == len(with_url)
    assert PW not in json.dumps(out["kinds"])          # the plan is a report, not a secret
    assert PW not in json.dumps(out["portal"] if "portal" in out else {})
    await flush_logs()
    await flush_logs()


async def test_re_adopt_rewrites_every_row_so_a_removed_stream_cannot_linger(monkeypatch):
    """Adoption is idempotent in the strong sense: it re-derives, it does not merge.

    A channel the panel stopped offering must lose its URL. If it kept yesterday's,
    a re-adopt would look like a fix and the user would still get a black screen.
    """
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=3)
        p = await s.get(Portal, pid)
        await xtream_bridge.probe(s, p)
        await xtream_bridge.adopt(s, p)
    async with SessionLocal() as s:
        rows = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().all()
        stale = rows[0]
        stale.portal_channel_id = "1"          # pretend the panel moved this channel
        stale.xtream_url = "http://test/live/x/y/gone.ts"
        await s.commit()
        p = await s.get(Portal, pid)
        out = await xtream_bridge.adopt(s, p, force=True)
        rows = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().all()
    assert out["ok"] is True
    assert "gone.ts" not in "".join(r.xtream_url or "" for r in rows)


async def test_detach_pauses_and_clear_forgets(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=3)
        p = await s.get(Portal, pid)
        await xtream_bridge.probe(s, p)
        await xtream_bridge.adopt(s, p)
    async with SessionLocal() as s:
        p = await s.get(Portal, pid)
        out = await xtream_bridge.detach(s, p)
        rows = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().all()
    assert out == {"ok": True, "xtream_adopted": False, "cleared": False}
    # URLs kept: switching back should not cost the panel another catalogue walk
    assert all(r.xtream_url for r in rows)
    async with SessionLocal() as s:
        p = await s.get(Portal, pid)
        await xtream_bridge.detach(s, p, clear=True)
        rows = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().all()
    assert not any(r.xtream_url for r in rows)
    await flush_logs()


async def test_adopt_needs_a_probe_first(monkeypatch):
    Wired(monkeypatch)
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1)
        p = await s.get(Portal, pid)
        out = await xtream_bridge.adopt(s, p)
    assert out["ok"] is False and "Detect" in out["error"]


# =========================================================================== #
# 8. the payoff: an adopted channel plays without asking the panel
# =========================================================================== #
async def test_adopted_channel_skips_create_link_entirely(monkeypatch):
    from app.services import stream_manager as sm

    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=2)
        p = await s.get(Portal, pid)
        await xtream_bridge.probe(s, p)
        await xtream_bridge.adopt(s, p)
        pl = LivePlaylist(custom_name="Ch", enabled=True)
        s.add(pl)
        await s.flush()
        src = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().first()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id, priority=1))
        await s.commit()
        pl_id, url_want = pl.id, src.xtream_url
        before = _STATE["create_links"]

        # the flag, not a fixture: what `_plan` decides with the real rows
        p = await s.get(Portal, pid)
        mac_row = (await s.execute(select(MacAddress).where(
            MacAddress.portal_id == pid))).scalars().first()
        plan = sm.MANAGER._plan(src, mac_row, p)
        assert plan.adopted and plan.policy.direct and url_want in plan.direct_url
        assert plan.direct_url.startswith("http://test/mock/live/")
        assert "Xtream" in plan.policy.reason

    url, _name = await sm.MANAGER.resolve("live", pl_id)
    assert url == url_want
    assert _STATE["create_links"] == before, "an adopted channel must not ask for a link"


async def test_a_mac_less_portal_still_plays_once_adopted(monkeypatch):
    """The panel that bans per-MAC sessions but hands out Xtream links.

    `_macs_for` is the one place that decides whether a source has a chain, and an
    adopted source needs no MAC of its own - the URL is authenticated by its path.
    """
    from app.services import stream_manager as sm

    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1, mac="")
        p = await s.get(Portal, pid)
        await xtream_bridge.probe(s, p)
        # no MAC means no harvest normally, so the observation is stored by hand here
        # - this test is about the *chain*, not about how the creds got here
        p.xtream = xtream_bridge.dumps_observation(creds(base="http://test", user=USER,
                                                         pw=PW, kind="live"))
        p.xtream_adopted = True
        src = (await s.execute(select(LiveSource).where(
            LiveSource.portal_id == pid))).scalars().first()
        src.xtream_url = f"http://test/mock/live/{USER}/{PW}/{src.portal_channel_id}.ts"
        await s.commit()
        portal = await s.get(Portal, pid)
        macs = await xtream_bridge._usable_mac(s, pid)
        src_row = await s.get(LiveSource, src.id)
    assert macs is None
    chain = sm.MANAGER._macs_for(portal, src_row, [])
    assert chain == [None]
    plan = sm.MANAGER._plan(src_row, None, portal)
    assert plan.adopted and plan.policy.direct


# =========================================================================== #
# 9. the API surface: masked, and explicit
# =========================================================================== #
async def test_routers_expose_the_offer_and_hide_the_password(monkeypatch):
    from app.routers import api_portals

    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=3)
        probed = await api_portals.xtream_probe(pid, force=False, db=s)
        assert probed["found"] is True and PW not in json.dumps(probed)
        # a probe alone changes nothing: the flag is only ever set by `adopt`
        assert probed["portal"]["xtream_adopted"] is False
        assert probed["portal"]["xtream"]["creds"]["password"] == "****"
        adopted = await api_portals.xtream_adopt(pid, db=s)
        assert adopted["ok"] is True and adopted["portal"]["xtream_adopted"] is True
        assert PW not in json.dumps(adopted)
        detached = await api_portals.xtream_detach(pid, db=s)
        assert detached["xtream_adopted"] is False
        # a detached portal answers 404, and it has to be asked on the session that
        # is still open - reusing `s` after `async with` would silently check out a
        # second connection that nobody returns, which is how a test file starts
        # losing `database is locked` races against its own next test
        with pytest.raises(HTTPException) as exc:
            await api_portals.xtream_probe(pid + 999, db=s)
        assert exc.value.status_code == 404
    await flush_logs()


async def test_check_portal_probes_once_and_never_fails_the_check(monkeypatch):
    """`test_portal` is about the MACs; a crash in an optional extra may not spoil it.

    The probe is wrapped, the exception is swallowed into `xtream: None`, and the
    results the user came for are unchanged.
    """
    from app.routers import api_portals

    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=2)
        out = await api_portals.test_portal(pid, db=s)
    assert out["results"] and out["results"][0]["online"] is True
    assert out["xtream"]["found"] is True and PW not in json.dumps(out["xtream"])
    async with SessionLocal() as s:
        p = await s.get(Portal, pid)
        assert p.xtream_adopted is False       # found, offered, not applied
        assert p.xtream
    await flush_logs()


async def test_check_portal_does_not_re_ask_a_portal_that_has_already_answered(monkeypatch):
    from app.routers import api_portals

    w = Wired(monkeypatch)
    await w.control(xtream_mode="on")
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1)
        await api_portals.test_portal(pid, db=s)
        first = _STATE["create_links"]
        await api_portals.test_portal(pid, db=s)
        assert _STATE["create_links"] == first      # nothing is stored => nothing probed
    async with SessionLocal() as s:
        p = await s.get(Portal, pid)
        assert json.loads(p.xtream)["found"] is True


async def test_update_portal_cannot_flip_adoption_on_its_own(monkeypatch):
    """A field settable by hand would need the per-channel URLs to match; they won't."""
    from app.routers import api_portals

    Wired(monkeypatch)
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1)
        await api_portals.update_portal(pid, {"name": "bridge", "xtream_adopted": True}, db=s)
        p = await s.get(Portal, pid)
    assert p.xtream_adopted is False


async def test_summary_is_a_pure_read_of_the_row():
    async with SessionLocal() as s:
        pid = await _seed(s, live_rows=1, mac="")
        p = await s.get(Portal, pid)
        p.xtream = xtream_bridge.dumps_observation(creds(base="http://t", user="u", pw="p"),
                                                   parse_player_api({"user_info": {
                                                       "status": "Active", "auth": 1}}),
                                                   why="")
        p.xtream_adopted = True
        p.xtream_at = datetime.now(timezone.utc)
        out = xtream_bridge.summary(p)
    assert out["found"] and out["adopted"] and out["account"]["status"] == "online"
    assert out["username"] == "u" and out["probed"] is True
    assert "password" not in json.dumps(out)


async def test_api_request_uses_the_app_outbound_policy(monkeypatch):
    """The bridge must not build its own httpx client.

    `proxy`/`insecure` are the per-portal trust decisions from R4/R5; a bare
    `httpx.AsyncClient()` here would work on a laptop and hang in the container
    behind the same proxy, which is the kind of bug no log explains.
    """
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"user_info": {"status": "Active", "auth": 1}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen["url"] = url
            return _Resp()

    def factory(**kw):
        seen.update(kw)
        return _Client()

    monkeypatch.setattr(xtream_bridge, "outbound_client", factory)
    got = await xtream_bridge.api_request(creds(base="http://x", user="u", pw="p"),
                                          "get_live_streams", proxy="http://proxy:3128",
                                          tls_insecure=True, timeout=4.0)
    assert seen["proxy"] == "http://proxy:3128" and seen["insecure"] is True
    assert seen["timeout"] == 4.0
    assert "action=get_live_streams" in seen["url"] and got["user_info"]["status"] == "Active"
    assert "/player_api.php?username=u&password=p" in seen["url"]


async def test_api_request_raises_on_an_http_refusal(monkeypatch):
    """A 4xx is an exception the caller turns into `why`, never a parsed account."""
    class _Resp:
        status_code = 403
        text = "<html>no</html>"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(xtream_bridge, "outbound_client", lambda **kw: _Client())
    with pytest.raises(RuntimeError) as exc:
        await xtream_bridge.api_request(creds(base="http://x", user="u", pw="p"))
    assert "403" in str(exc.value)


