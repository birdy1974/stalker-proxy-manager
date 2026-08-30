"""
R6: what the panel says about itself, and what we are allowed to do with it.

Two probes, one rule. `version.js` is a static file that needs no token, so it
can be read while the portal is still being *resolved* - and the module list
from `get_modules` decides which catalogues exist at all. The rule the whole
file is built around is that **an unanswered probe must not cost a user their
channels**: every "the panel did not reply" case has to land on `None`
(unknown), never on `[]` (nothing), because gating on the difference is how a
portal that was briefly unreachable would silently lose its VOD catalogue.

As in the identity tests, the assertions run against what the *mock portal
recorded*: a probe we intended to send but never sent is worth nothing.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import MacAddress, Portal
from app.portal import client as client_mod
from app.portal.capabilities import (dumps_modules, enabled_modules,
                                     gate_feature, loads_modules, parse_modules,
                                     parse_version_js, supports, version_js_url)
from app.portal.mock_portal import _STATE
from app.portal.resolver import resolve_portal
from app.services import fetch_jobs
from app.services.db_logging import flush_logs
from mockclient import GOOD, PORTAL, Wired

_TOGGLES = ("version_mode", "modules", "modules_disabled", "no_modules", "offline")


@pytest.fixture(autouse=True)
def _mock_reset():
    saved = {k: _STATE.get(k) for k in _TOGGLES}
    yield
    _STATE.update(saved)


def set_state(**kw):
    _STATE.update(kw)


async def _portal_row(**fields) -> int:
    async with SessionLocal() as s:
        p = Portal(**{"name": "caps", "base_url": "http://test/mock/c/",
                      "enabled": True, **fields})
        s.add(p)
        await s.flush()
        pid = p.id
        await s.commit()
    return pid


# =========================================================================== #
# version.js - the answer that needs no token
# =========================================================================== #
@pytest.mark.parametrize("body,want", [
    ("var ver = '5.4.2';", "5.4.2"),
    ('xpcom.version = { portal: "5.3.14", image: "0.2.18" };', "5.3.14"),
    ("portal_version: 5.4.0;", "5.4.0"),
    ("some noise\nPORTAL version: 5.2.0; API Version: JS API version: 343;\n", "5.2.0"),
])
def test_every_version_js_shape_a_panel_uses(body, want):
    assert parse_version_js(body).portal == want


def test_the_image_version_is_read_too_because_the_pair_explains_quirks():
    v = parse_version_js("var ver = '5.4.2';\nvar ImageDescription = '0.2.20-r3-250';")
    assert v.image == "0.2.20-r3-250"
    # one printable line, for the GUI badge and for a bug report
    assert v.label == "portal 5.4.2 image 0.2.20-r3-250"


def test_a_product_name_is_kept_because_ministra_and_iversa_behave_differently():
    v = parse_version_js("/* Ministra-STB */\nvar ver = '5.4.2';")
    assert v.product == "ministra"
    assert v.label.startswith("Ministra portal 5.4.2")


@pytest.mark.parametrize("body", [
    "<!DOCTYPE html><html><head>Access denied</head><body>ver = 'blocked'</body></html>",
    "<html><body>ver = 9.9.9</body></html>",
])
def test_an_html_page_is_never_a_portal_version(body):
    """A captive portal or WAF answers this URL, and its markup contains `ver =`.

    Printing a fragment of HTML as "the portal version" is worse than saying
    nothing: the user reads it as a fact about their panel and files a report
    about the wrong thing.
    """
    v = parse_version_js(body)
    assert not v.known
    assert "captive portal" in v.error or "redirected" in v.error


def test_unreadable_and_empty_bodies_are_errors_not_crashes():
    assert not parse_version_js("").known
    assert not parse_version_js(None).known
    assert parse_version_js("// nothing here").error == "no version in version.js"


def test_the_file_lives_next_to_index_html_of_the_winning_path():
    assert version_js_url("http://h/stalker_portal/c/portal.php") \
        == "http://h/stalker_portal/c/version.js"
    assert version_js_url("http://h:8080/portal.php") == "http://h:8080/version.js"
    assert version_js_url("") == ""


# =========================================================================== #
# get_modules - the answer that gates a 30-minute job
# =========================================================================== #
@pytest.mark.parametrize("payload,want", [
    ({"js": {"all_modules": ["tv", "vclub", "sclub"], "disabled_modules": ["sclub"]}},
     ["tv", "vclub"]),
    # the 5.4 shape: a list of dicts carrying their own status
    ({"js": {"all_modules": [{"name": "tv", "status": "1"},
                             {"name": "vclub", "status": "0"}],
             "disabled_modules": []}}, ["tv"]),
    # and the other 5.4 shape: the panel lists only what is off
    ({"js": {"all_modules": {"tv": {"title": "Live"}, "vclub": {"title": "VOD"}},
             "disabled_modules": ["vclub"]}}, ["tv"]),
    # a module marked off inside all_modules is disabled, not lost: the `!name`
    # fold keeps it out of the gate without hiding the fact that it exists
    ({"js": {"all_modules": {"tv": {"status": "0"}, "sclub": {"status": "1"}},
             "disabled_modules": []}}, ["sclub"]),
    ({"all_modules": ["tv"], "disabled_modules": None}, ["tv"]),
    # a bare js list is what some older panels send
    ({"js": [{"all_modules": ["tv", "epg"], "disabled_modules": []}]}, ["tv", "epg"]),
])
def test_module_answers_are_read_in_whatever_shape_the_panel_picked(payload, want):
    assert enabled_modules(payload) == want


@pytest.mark.parametrize("payload", [
    None, {}, {"js": {}}, {"js": {"error": "no such action"}},
    {"js": {"all_modules": []}},                     # "nothing at all" is not a fact
    {"js": {"all_modules": "   "}},                  # a name nobody typed
    {"js": {"disabled_modules": ["tv"]}},            # only what is off tells us nothing
])
def test_not_being_told_is_stored_as_unknown_and_gates_nothing(payload):
    """The distinction the whole feature rests on.

    A panel that 404s `get_modules` has not told us it has no series; it has told
    us nothing. Collapsing those two into an empty list is how catalogues
    disappear from a working portal.
    """
    assert enabled_modules(payload) is None
    ok, why = gate_feature(None, "series")
    assert (ok, why) == (True, "")


def test_a_single_name_is_still_a_name():
    assert enabled_modules({"js": {"all_modules": "tv"}}) == ["tv"]


def test_a_comma_string_and_a_json_string_are_both_read_as_lists():
    """Panels serialize this field three different ways; a name must not be lost
    to a quoting choice, because the other two shapes then look "complete"."""
    assert enabled_modules({"js": {"all_modules": "tv, vclub ,sclub"}}) == ["tv", "vclub", "sclub"]
    assert enabled_modules({"js": {"all_modules": '["tv", "vclub"]'}}) == ["tv", "vclub"]
    assert parse_modules(None) == ([], [])


def test_features_map_to_the_module_names_panels_actually_use():
    assert supports(["tv", "vclub", "sclub"], "vod") is True
    assert supports(["tv", "vclub"], "series") is False
    assert supports(["tv", "itv"], "live") is True
    # both archive spellings are real in the wild
    assert supports(["captured_tv_archive"], "archive") is True
    assert supports(None, "vod") is None


def test_the_reason_names_the_module_and_what_was_offered():
    ok, why = gate_feature(["tv", "epg"], "vod")
    assert not ok
    assert "vclub" in why and "tv, epg" in why


def test_the_stored_answer_survives_a_round_trip_and_junk_does_not():
    assert loads_modules(dumps_modules(["vclub", "tv"])) == ["tv", "vclub"]
    assert dumps_modules(None) is None and loads_modules(None) is None
    assert loads_modules("[]") is None and loads_modules("{oops}") is None


# =========================================================================== #
# the client, against the mock portal
# =========================================================================== #
async def test_resolve_reads_version_js_on_the_way_out(monkeypatch):
    """No token, no MAC, no handshake - which is why it happens during discovery.

    The version is the line that explains a quarter of portal bug reports, and
    discovery is the one moment we know for sure the panel is answering us.
    """
    w = Wired(monkeypatch)
    res = await resolve_portal("http://test/mock/c/")
    assert res.ok, res.attempts
    assert res.version.portal == "5.4.2"
    assert res.version.known and "ministra" in res.version.product
    assert any("version.js" in line for line in res.attempts)
    # the Referer the box will claim, printable on purpose: "it resolves but
    # 403s" is usually a question about this path
    assert res.referer == "http://test/mock/c/index.html"
    assert (await w.state())["counters"]["version_calls"] == 1


async def test_a_missing_or_html_version_file_leaves_the_resolve_alone(monkeypatch):
    _wired = Wired(monkeypatch)   # the patching IS the point
    for mode, expect in (("none", "http_404"), ("html", "HTML")):
        set_state(version_mode=mode)
        res = await resolve_portal("http://test/mock/c/")
        assert res.ok, f"a cosmetic probe must not fail a resolve ({mode})"
        assert not res.version.known
        assert expect in res.version.error
    set_state(version_mode="full")


async def test_the_client_reads_both_answers_in_one_call(monkeypatch):
    w = Wired(monkeypatch)
    client = w.client(GOOD)
    await client.handshake()
    caps = await client.refresh_capabilities()
    assert caps["version"]["portal"] == "5.4.2"
    assert "vclub" in caps["modules"] and caps["features"]["vod"] is True
    state = await w.state()
    assert state["counters"]["modules_calls"] == 1


async def test_capabilities_are_cached_until_asked_for_again(monkeypatch):
    w = Wired(monkeypatch)
    client = w.client(GOOD)
    await client.handshake()
    await client.refresh_capabilities()
    before = (await w.state())["counters"]["modules_calls"]
    await client.portal_modules()               # cached: no request
    assert (await w.state())["counters"]["modules_calls"] == before
    await client.portal_modules(force=True)     # the GUI's "ask again"
    assert (await w.state())["counters"]["modules_calls"] == before + 1


async def test_a_panel_without_the_action_is_recorded_as_ignorance(monkeypatch):
    """`no_modules` must not become "this portal has no series"."""
    w = Wired(monkeypatch)
    await w.control(no_modules=1)
    client = w.client(GOOD)
    await client.handshake()
    caps = await client.refresh_capabilities()
    assert caps["modules"] is None
    assert caps["features"]["vod"] is None
    assert caps["modules_error"], "and we say why"
    ok, why = gate_feature(caps["modules"], "vod")
    assert (ok, why) == (True, "")


async def test_disabled_modules_are_offered_but_gated(monkeypatch):
    w = Wired(monkeypatch)
    await w.control(modules_disabled="sclub,tv_archive")
    client = w.client(GOOD)
    await client.handshake()
    caps = await client.refresh_capabilities()
    assert caps["modules"] and "sclub" not in caps["modules"]
    assert caps["features"]["series"] is False
    assert caps["features"]["vod"] is True


# =========================================================================== #
# the router: store it, show it, and never fail a resolve over it
# =========================================================================== #
async def test_resolve_stores_what_the_panel_said(monkeypatch):
    from app.routers import api_portals

    _wired = Wired(monkeypatch)   # the patching IS the point
    pid = await _portal_row(resolved_url=PORTAL)
    async with SessionLocal() as s:
        s.add(MacAddress(portal_id=pid, mac=GOOD, order=0))
        await s.commit()
        out = await api_portals.resolve(pid, db=s)
    assert out["ok"] and out["version"]["portal"] == "5.4.2"
    assert "vclub" in out["modules"]
    assert out["features"]["vod"] is True and out["features"]["series"] is True
    assert out["referer"].endswith("/mock/c/index.html")

    async with SessionLocal() as s:
        p = await s.get(Portal, pid)
        row = api_portals._portal_row(p, [])
    assert "5.4.2" in row["portal_version"]
    assert "sclub" in row["modules"] and row["capabilities_at"]
    # a portal row that carries `features` is what lets the GUI grey a tab out
    # with a reason instead of fetching an empty catalogue
    assert row["features"]["vod"] is True
    assert p.resolved_path == "/mock/c/"      # R6 asked for the winning prefix
    await flush_logs()


async def test_a_panel_that_refuses_to_answer_still_resolves(monkeypatch):
    from app.routers import api_portals

    _wired = Wired(monkeypatch)   # the patching IS the point
    await _wired.control(no_modules=1, version_mode="none")
    pid = await _portal_row(resolved_url=PORTAL)
    async with SessionLocal() as s:
        s.add(MacAddress(portal_id=pid, mac=GOOD, order=0))
        await s.commit()
        out = await api_portals.resolve(pid, db=s)
        p = await s.get(Portal, pid)
    assert out["ok"], "the probes are information; they may not fail the resolve"
    assert out["modules"] is None
    assert p.modules is None and not p.portal_version
    row = api_portals._portal_row(p, [])
    assert row["modules"] is None and row["features"]["vod"] is None
    await flush_logs()


async def test_a_crashing_probe_cannot_break_a_working_portal(monkeypatch):
    """The probes go through a real client, so they can raise anything at all."""

    _wired = Wired(monkeypatch)   # the patching IS the point
    pid = await _portal_row(resolved_url=PORTAL)

    def boom(self):
        raise RuntimeError("bug in the probe")

    monkeypatch.setattr(client_mod.StalkerClient, "refresh_capabilities", boom)
    from app.routers import api_portals

    async with SessionLocal() as s:
        out = await api_portals.resolve(pid, db=s)
        p = await s.get(Portal, pid)
    assert out["ok"] is True and out["modules"] is None
    assert p.modules is None
    await flush_logs()


async def test_the_answer_is_in_the_backup_and_survives_a_restore(monkeypatch):
    """`modules` gates fetch jobs, so a backup that drops it loses the gate."""
    from fastapi.responses import JSONResponse

    from app.routers import api_misc, api_portals

    _wired = Wired(monkeypatch)   # the patching IS the point
    pid = await _portal_row(resolved_url=PORTAL)
    async with SessionLocal() as s:
        s.add(MacAddress(portal_id=pid, mac=GOOD, order=0))
        await s.commit()
        await api_portals.resolve(pid, db=s)
        stored = (await s.get(Portal, pid)).modules

    async with SessionLocal() as s:
        resp = await api_misc.export_config(section="portals", db=s)
    assert isinstance(resp, JSONResponse)
    data = json.loads(resp.body.decode())
    portal = data["portals"][0]
    for field in ("portal_version", "modules", "direct_links"):
        assert field in portal, f"backup lost {field}"
    assert portal["modules"] == stored

    # an import of a *newer* backup must not die on a column this build lacks
    data["portals"][0]["name"] = "restored"
    data["portals"][0]["from_the_future"] = 1
    async with SessionLocal() as s:
        applied = await api_misc.import_config({"mode": "merge", "data": data}, db=s)
        p = (await s.execute(select(Portal).where(Portal.name == "restored"))).scalar_one()
    assert applied["imported"] == 1
    assert p.modules == portal["modules"] and p.direct_links == portal["direct_links"]
    await flush_logs()


# =========================================================================== #
# the gate that saves the user a job they cannot finish
# =========================================================================== #
async def test_a_fetch_skips_the_catalogues_the_panel_has_not_got():
    from app.services.fetch_jobs import _feature_gate

    pid = await _portal_row(base_url="http://x/c/", modules=dumps_modules(["tv", "epg"]))
    ok, why = await _feature_gate(pid, "vod")
    assert not ok and "vclub" in why
    assert (await _feature_gate(pid, "live"))[0] is True

    pid2 = await _portal_row(name="never-asked", base_url="http://x/c/", modules=None)
    assert (await _feature_gate(pid2, "vod"))[0] is True, "no answer: no assumptions"
    assert (await _feature_gate(999999, "vod"))[0] is True


class _RecordingClient:
    def __init__(self):
        self.calls: list[str] = []

    async def live_genres(self):
        self.calls.append("live")
        return []

    async def vod_genres(self):
        self.calls.append("vod")
        return []

    async def series_genres(self):
        self.calls.append("series")
        return []

    async def get_vod_items(self, *a, **kw):
        self.calls.append("vod-items")
        return []

    async def get_series_items(self, *a, **kw):
        self.calls.append("series-items")
        return []

    async def get_live_items(self, *a, **kw):
        self.calls.append("live-items")
        return []


@pytest.fixture
def logs(monkeypatch):
    """Capture what a service told the user, because "we skipped this on purpose"
    is a claim the fetch log is the only place a user can check."""
    seen: list[tuple[str, str, str]] = []

    async def fake(level, module, message, **kw):
        seen.append((level, module, message))

    monkeypatch.setattr(fetch_jobs, "db_log", fake)
    return seen


async def test_the_genre_fetch_does_not_ask_a_panel_for_a_module_it_lacks(logs):
    """`get_categories` for VOD/Series is skipped, and the log says who told us."""
    pid = await _portal_row(name="tv-only", base_url="http://x/c/",
                            modules=dumps_modules(["tv"]))
    client = _RecordingClient()
    job = fetch_jobs.Job(id="j", kind="genres", portal_id=pid)
    await fetch_jobs._sync_all_genres(job, client, pid, "tv-only")
    assert client.calls == ["live"], "the panel said no vclub/sclub: do not ask"
    assert any("vod categories skipped" in m for _l, _mo, m in logs)
    assert any("series categories skipped" in m for _l, _mo, m in logs)
    assert all("ERROR" not in l for l, _m, _x in logs), "a skip is a fact, not a fault"


async def test_an_unknown_panel_is_still_fetched_in_full():
    """The other half of the gate: no answer must not mean no catalogue."""
    pid = await _portal_row(name="silent", base_url="http://x/c/", modules=None)
    client = _RecordingClient()
    job = fetch_jobs.Job(id="j", kind="genres", portal_id=pid)
    await fetch_jobs._sync_all_genres(job, client, pid, "silent")
    assert client.calls == ["live", "vod", "series"]


async def test_the_items_stage_is_gated_for_both_halves_too(logs):
    """`fetch_all` on a TV-only panel must not spend 30 minutes on `get_vod_list`."""
    pid = await _portal_row(name="tv-only2", base_url="http://x/c/",
                            modules=dumps_modules(["tv"]))
    client = _RecordingClient()
    job = fetch_jobs.Job(id="j", kind="fetch_all", portal_id=pid)
    await fetch_jobs._run_items_fetch(job, client, "tv-only2")
    assert client.calls == [], "the panel said no vclub and no sclub: do not ask"
    assert any("vod items skipped" in m for _l, _mo, m in logs)
    assert any("series items skipped" in m for _l, _mo, m in logs)
