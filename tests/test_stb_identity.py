"""
R1 (STB identity) and R3 (portal truth about the account).

Both come from docs/ESTALKER-COMPARISON.md, and both are about the same thing
seen from two sides: **a Stalker panel answers the client it recognises**.

R1 - identity. A portal decides how much to serve by how much the request looks
like the box it enrolled: the `X-User-Agent`/`Referer`/cookie set, the device
fingerprint in `get_profile`, and the second-step handshake where the box invents
a bearer and proves it knows its hash. EStalker is accepted everywhere because it
*is* the box; our bare `User-Agent` + `mac` cookie works on lenient panels and on
portals that gate on those details answer with 200 + nothing - the worst possible
failure mode, because it looks like an empty category rather than a refusal.

R3 - account truth. `get_profile` and `account_info` report `blocked`, `status`,
`force_ch_link_check` and the expiry. Marking a MAC `online` because the
handshake worked means a banned or expired MAC stays in every fallback chain
forever: a create_link, a refusal, and a portal connection slot per attempt.

The identity tests assert what the *portal received* (`/mock/_state` records the
queries), not what we intended to send - "we built the fingerprint" is worth
nothing if it never reaches the wire.
"""

from __future__ import annotations

import hashlib
import json
import string
from datetime import datetime, timezone
from urllib.parse import unquote

import pytest

from app.portal import client as client_mod
from app.portal.account import (account_verdict, expiry_warning, mac_is_usable,
                                mac_status, parse_expiry)
from app.portal.identity import (MINIMAL, STB_UA, cookies_for, derive_identity,
                                 headers_for, make_fake_bearer,
                                 minimal_profile_params, missing_token,
                                 normalize_mac, profile_params, referer_for,
                                 wants_adid_cookie)
from app.portal.mock_portal import MOCK_MACS, _STATE
from app.portal.pool import PortalSession
from app.services.db_logging import flush_logs
from mockclient import BANNED, EXPIRED, GOOD, PORTAL, Wired


_DEFAULTS = {k: v for k, v in _STATE.items()
             if k in ("offline", "slow", "max_per_mac", "create_link_error", "token_rejects",
                      "mac_placeholder", "require_prehash", "require_mac_param",
                      "fingerprint_required", "profile_mode", "not_valid")}


@pytest.fixture(autouse=True)
def _mock_reset():
    """The mock's toggles are module state; never inherit another test's panel."""
    saved = dict(_STATE)
    for k, v in _DEFAULTS.items():
        _STATE[k] = v
    _STATE["handshakes"] = 0
    _STATE["profile_calls"] = 0
    _STATE["profile_seen"] = {}
    _STATE["handshake_seen"] = []
    yield
    _STATE.clear()
    _STATE.update(saved)


def set_state(**kw):
    _STATE.update(kw)


# =========================================================================== #
# the derived identity
# =========================================================================== #
def test_identity_is_derived_from_the_mac_not_random():
    """Same MAC -> same box, in every process and every spelling of the input.

    A portal remembers the device it enrolled. If the serial were random, a
    container restart would present a *new device* from the same MAC, which is
    how a working account gets flagged - so this is a determinism test, not a
    style test.
    """
    a = derive_identity("00:1A:79:AA:AA:01")
    b = derive_identity("00:1a:79:aa:aa:01")
    c = derive_identity("001A79AAAA01")
    assert (a.sn, a.device_id, a.prehash, a.adid) == (b.sn, b.device_id, b.prehash, b.adid)
    assert (a.sn, a.device_id) == (c.sn, c.device_id), "a MAC typed without colons is the same box"
    assert len(a.sn) == 13 and a.sn == a.sn.upper()
    assert a.prehash == hashlib.sha1((a.sn + a.mac).encode()).hexdigest()
    assert a.hw_version_2 == hashlib.sha1(a.mac.encode()).hexdigest()


def test_a_pinned_serial_keeps_the_whole_set_consistent():
    """Overriding `sn` must not leave a stale `prehash` computed from the old one."""
    idn = derive_identity(GOOD, sn="REAL-BOX-SN")
    assert idn.sn == "REAL-BOX-SN"
    assert idn.prehash == hashlib.sha1(f"REAL-BOX-SN{GOOD}".encode()).hexdigest()
    assert idn.adid == hashlib.md5(f"REAL-BOX-SN{GOOD}".encode()).hexdigest()


def test_profile_params_are_the_fingerprint_a_box_sends():
    idn = derive_identity(GOOD)
    q = profile_params(idn, token_random="RND", timestamp=1700000000)
    for field in ("hd", "ver", "num_banks", "sn", "stb_type", "client_type", "image_version",
                  "video_out", "device_id", "device_id2", "signature", "auth_second_step",
                  "hw_version", "not_valid_token", "metrics", "hw_version_2", "timestamp",
                  "api_signature", "prehash"):
        assert field in q, f"a panel matching on {field} would see it missing"
    assert q["action"] == "get_profile" and q["type"] == "stb"
    assert q["device_id2"] == q["device_id"], "the box reports the same id twice"
    assert q["not_valid_token"] == "0"
    # `metrics` is percent-encoded JSON that has to name the same box
    metrics = json.loads(unquote(q["metrics"]))
    assert metrics == {"mac": GOOD, "sn": idn.sn, "model": "MAG250", "type": "STB",
                       "uid": "", "random": "RND"}


def test_minimal_mode_sends_the_two_field_shape():
    """`identity_mode="minimal"` is the escape hatch, so it must be real."""
    idn = derive_identity(GOOD, minimal=True)
    assert profile_params(idn, timestamp=1700000000) == minimal_profile_params(idn, timestamp=1700000000)
    assert set(minimal_profile_params(idn)) == {
        "type", "action", "JsHttpRequest", "sn", "device_id", "timestamp"}


def test_not_valid_from_the_handshake_is_echoed_back():
    """`js.not_valid` is a request the panel makes of us; ignoring it fails step 2."""
    idn = derive_identity(GOOD)
    assert profile_params(idn, not_valid=True)["not_valid_token"] == "1"
    assert profile_params(idn, not_valid=False)["not_valid_token"] == "0"


def test_headers_look_like_the_box_without_sabotaging_our_pooling():
    idn = derive_identity(GOOD)
    h = headers_for("http://h/stalker_portal/c/portal.php", idn, user_agent="MAG-UA",
                    token="TOK")
    assert h["X-User-Agent"] == "Model: MAG250; Link: WiFi"
    assert h["Referer"] == "http://h/stalker_portal/c/index.html"
    assert h["Authorization"] == "Bearer TOK"
    assert h["User-Agent"] == "MAG-UA"
    # deliberate deviations from a box emulator, both load-bearing for a proxy:
    assert "Connection" not in h, "Connection: Close would throw away every keep-alive"
    assert "Host" not in h, "we may be talking through a proxy, which sets its own Host"


def test_referer_and_adid_and_cookie_rules():
    assert referer_for("http://host:8080/c/portal.php") == "http://host:8080/c/index.html"
    assert referer_for("portal.php") == "", "no host -> no referer, never a relative one"
    assert wants_adid_cookie("http://h/stalker_portal/c/portal.php") is True
    assert wants_adid_cookie("http://h/c/portal.php") is False
    jar = cookies_for("http://h/c/portal.php", "00:1a:79:aa:aa:01", adid="AD", timezone="Europe/Oslo")
    # the MAC stays in its colon form: a panel that compares the cookie verbatim
    # must keep matching what it matched before the identity work
    assert jar["mac"] == "00:1A:79:AA:AA:01"
    assert jar["timezone"] == "Europe/Oslo" and "adid" not in jar
    full = cookies_for("http://h/stalker_portal/c/portal.php", GOOD, adid="AD", token="T")
    assert full["adid"] == "AD" and full["token"] == "T"
    assert normalize_mac("00-1a-79-aa-aa-01") == GOOD


def test_second_step_bearer_matches_its_own_prehash():
    token, prehash = make_fake_bearer()
    assert len(token) == 32
    assert set(token) <= set(string.ascii_uppercase + string.digits)
    assert prehash == hashlib.sha1(token.encode()).hexdigest()
    assert all(ch.isalnum() for ch in token)


def test_missing_is_the_only_msg_that_means_prove_your_bearer():
    assert missing_token({"msg": "missing"}) is True
    assert missing_token({"msg": "Missing Authorization"}) is True
    assert missing_token({"msg": "OK"}) is False
    assert missing_token({"token": "t"}) is False
    assert missing_token(None) is False


# =========================================================================== #
# the dance, against the mock portal
# =========================================================================== #
async def test_second_step_handshake_succeeds(monkeypatch):
    """`{"js":{"msg":"missing"}}` used to be a dead end; it is an instruction."""
    w = Wired(monkeypatch)
    set_state(require_prehash=True)
    c = w.client(GOOD)
    token = await c.handshake()
    assert token, "the panel only answers the prehash dance - so the dance must happen"
    state = await w.state()
    seen = state["seen_handshakes"]
    assert seen[0]["prehash"] in ("", "0") and seen[0]["bearer"] is False   # stage 1
    assert seen[1]["mac_param"] is True                                      # stage 2
    assert seen[-1]["prehash"] not in ("", "0"), "no prehash was ever sent"
    # and the hash we sent really is the hash of the bearer we sent
    assert c._token_random == "mock-random-seed", "js.random is read and kept for metrics"


async def test_handshake_carries_the_mac_param_when_the_panel_needs_it(monkeypatch):
    """Some panels key the session on `mac=` and answer stage 1 with nothing."""
    w = Wired(monkeypatch)
    set_state(require_mac_param=True)
    c = w.client(GOOD)
    await c.handshake()
    seen = (await w.state())["seen_handshakes"]
    assert seen[0]["mac_param"] is False and seen[1]["mac_param"] is True


async def test_fingerprint_reaches_the_portal(monkeypatch):
    """The point of R1 is observable on the wire, not in our own data structures."""
    w = Wired(monkeypatch)
    set_state(fingerprint_required=True, not_valid=True)
    c = w.client(GOOD)
    await c.handshake()                       # must not raise: the panel is happy
    seen = (await w.state())["seen_profile"]
    idn = derive_identity(GOOD)
    assert seen["sn"] == idn.sn and seen["device_id"] == idn.device_id
    assert seen["prehash"] == idn.prehash and seen["signature"] == idn.signature
    assert seen["not_valid_token"] == "1", "js.not_valid=1 must be echoed"
    assert json.loads(unquote(seen["metrics"]))["random"] == "mock-random-seed"


async def test_a_portal_without_get_profile_is_still_usable(monkeypatch):
    """Regression: the fingerprint call must never re-trigger the handshake.

    `get_profile` is asked for *inside* the handshake. When the portal answers a
    403 (it does not implement the action - the common case), the generic
    "401/403 -> re-handshake once" rule re-entered the handshake, which asked
    for the profile again, forever. A portal without `get_profile` used to hang
    the client instead of simply having no account data.
    """
    w = Wired(monkeypatch)
    set_state(profile_mode="none")
    c = w.client(GOOD)
    token = await c.handshake()
    assert token
    state = await w.state()
    assert state["counters"]["profile_calls"] == 1, "one attempt, then move on"
    assert state["counters"]["handshakes"] == 1, "a refused profile must not re-handshake"
    assert c._profile is None


async def test_profile_without_id_falls_back_to_the_minimal_shape(monkeypatch):
    w = Wired(monkeypatch)
    set_state(profile_mode="no_id")
    c = w.client(GOOD)
    await c.handshake()
    state = await w.state()
    assert state["counters"]["profile_calls"] == 2, "full shape, then the box's own fallback"
    assert state["seen_profile"]["device_id"] == "", "the minimal shape sends no device id"
    assert c._profile.get("mac") == GOOD


async def test_wrong_fingerprint_is_survivable(monkeypatch):
    """`minimal` against a panel that demands a fingerprint: no data, no crash."""
    w = Wired(monkeypatch)
    set_state(fingerprint_required=True)
    c = w.client(GOOD, identity_mode="minimal")
    await c.handshake()
    assert c._token, "the handshake never depends on the profile"
    assert c._profile is None


async def test_bearer_is_kept_in_the_cookie_too(monkeypatch):
    """Real firmware keeps the token in a cookie; some panels read that one."""
    w = Wired(monkeypatch)
    c = w.client(GOOD)
    await c.handshake()
    http = await c._http()
    assert http.cookies.get("token") == c._token
    assert http.headers["Authorization"] == f"Bearer {c._token}"
    c.invalidate()
    assert http.cookies.get("token") is None, "a stale token cookie shadows the fresh bearer"


# =========================================================================== #
# R3: account state -> a decision
# =========================================================================== #
@pytest.mark.parametrize("raw,expect", [
    ("2032-12-31 00:00:00", datetime(2032, 12, 31, tzinfo=timezone.utc)),
    ("2024-01-01", datetime(2024, 1, 1, tzinfo=timezone.utc)),
    ("2032-12-31T00:00:00+03:00", datetime(2032, 12, 30, 21, tzinfo=timezone.utc)),
    ("31.12.2032", datetime(2032, 12, 31, tzinfo=timezone.utc)),
    ("05/12/2031 22:30", datetime(2031, 12, 5, 22, 30, tzinfo=timezone.utc)),
    ("1893456000", datetime(2030, 1, 1, tzinfo=timezone.utc)),
    (1893456000, datetime(2030, 1, 1, tzinfo=timezone.utc)),
    ("2032-12-31 00:00:00.123456", datetime(2032, 12, 31, 0, 0, 0, 123456, tzinfo=timezone.utc)),
])
def test_expiry_is_parsed_in_the_formats_panels_use(raw, expect):
    assert parse_expiry(raw) == expect


@pytest.mark.parametrize("junk", ["Unknown", "", None, "sometime", "31.02.2032", "n/a", False,
                                  "0", "-", "2032-13-01"])
def test_an_unreadable_expiry_is_no_verdict(junk):
    """None means "we do not know" - and *not knowing* must never disable a MAC."""
    assert parse_expiry(junk) is None


def test_banned_beats_expired_beats_nothing():
    v = account_verdict(profile={"blocked": 1, "status": 1}, info={"phone": "2032-12-31"},
                        token="t")
    assert (v.status, v.online) == ("banned", False)
    v = account_verdict(profile={"blocked": "0"}, info={"phone": "2024-01-01 00:00:00"}, token="t")
    assert (v.status, v.online) == ("expired", False)
    assert "2024-01-01" in v.reason
    v = account_verdict(profile={"blocked": "0"}, info={"phone": "2032-12-31"}, token="")
    assert (v.status, v.online) == ("no_token", False)
    v = account_verdict(profile={"blocked": "0"}, info={"phone": "2032-12-31"}, token="t")
    assert (v.status, v.online, v.usable) == ("active", True, True)


def test_string_zero_is_not_blocked():
    """Panels send `"blocked": "0"`, and `bool("0")` is True."""
    for off in ("0", 0, False, None, "", "false", "no"):
        assert account_verdict(profile={"blocked": off}, token="t").status == "active"
    for on in ("1", 1, True, "yes"):
        assert account_verdict(profile={"blocked": on}, token="t").status == "banned"


def test_account_answer_is_not_invented_when_the_panel_says_nothing():
    v = account_verdict(profile={}, info={}, token="t")
    assert v.status == "active" and v.expire_date is None, "'not reported' is not 'expired'"
    v = account_verdict(profile={}, info={}, token=None)
    assert (v.status, v.online) == ("no_token", False)


def test_link_check_and_expiry_warning_are_carried():
    v = account_verdict(profile={"force_ch_link_check": "1"}, info={"phone": "2032-12-31"},
                        token="t")
    assert v.force_ch_link_check is True
    # an expiry 3 days out is still active - but it has to be *said*, because a
    # subscription that ends quietly is a support ticket in a week
    soon = account_verdict(profile={}, info={"phone": "2032-01-04 00:00:00"}, token="t",
                           now=datetime(2032, 1, 1, tzinfo=timezone.utc), near_expiry_days=7)
    assert soon.status == "active" and soon.online is True
    assert "expires in 3 day" in soon.reason
    assert expiry_warning(soon, warn_days=7) == ""          # past date: no warning needed
    dead = account_verdict(profile={}, info={"phone": "2024-01-01 00:00:00"}, token="t")
    assert "expired" in expiry_warning(dead)


def test_only_the_panel_own_verdicts_remove_a_mac():
    assert mac_is_usable("banned") is False and mac_is_usable("expired") is False
    # ours are transient: one timeout must not take a working source out of
    # every chain until someone presses Check Portal again
    for s in ("offline", "error", "unknown", "online", "", None, "Active"):
        assert mac_is_usable(s) is True, s
    assert mac_status(account_verdict(profile={}, info={}, token="")) == "unauthorized"
    assert mac_status(account_verdict(profile={"blocked": 1}, token="t")) == "banned"
    assert mac_status(account_verdict(profile={}, info={"phone": "2024-01-01"}, token="t")) == "expired"


async def test_refresh_account_reports_what_the_panel_says(monkeypatch):
    w = Wired(monkeypatch)
    banned = w.client(BANNED)
    await banned.handshake()
    v = await banned.refresh_account()
    assert (v.status, v.online) == ("banned", False)
    assert "blocked" in v.reason

    expired = w.client(EXPIRED)
    await expired.handshake()
    v = await expired.refresh_account()
    assert v.status == "expired" and v.expire_date == MOCK_MACS[EXPIRED]["phone"]

    good = w.client(GOOD)
    await good.handshake()
    v = await good.refresh_account()
    assert v.status == "active" and v.online is True
    assert v.play_token.startswith("mock-play-"), "the panel's own play_token is carried"


# =========================================================================== #
# wiring: pool profile, API, and the chain
# =========================================================================== #
def test_session_from_rows_carry_every_connection_setting():
    """The reason PortalSession exists: a setting cannot be half-applied.

    A portal's proxy, its TLS policy, its identity mode and a pinned serial all
    used to be threaded by hand through five call sites, and every one of them
    that was forgotten is a bug this repo has already had.
    """
    class P:
        base_url = "http://p/c/portal.php"
        resolved_url = "http://p/resolved/portal.php"
        proxy_url = "http://proxy:3128"
        tls_insecure = True
        identity_mode = "minimal"
        stb_timezone = "Europe/Oslo"

    class M:
        mac = "00:1A:79:AA:AA:01"
        password = "pw"
        sn = "PINNED"
        device_id = "DEV"

    s = PortalSession.from_rows(P, M)
    assert s.portal_url == P.resolved_url and s.proxy == P.proxy_url
    assert (s.tls_insecure, s.identity_mode, s.timezone) == (True, "minimal", "Europe/Oslo")
    assert (s.sn, s.device_id, s.mac, s.password) == ("PINNED", "DEV", M.mac, M.password)
    c = s.client()
    assert (c.tls_insecure, c.identity_mode, c.identity.sn) == (True, "minimal", "PINNED")
    # a row from before these columns existed must still produce a session
    class Old:
        base_url = "http://p/c/portal.php"
    s2 = PortalSession.from_rows(Old, M)
    assert s2.identity_mode == "mag250" and s2.tls_insecure is False


async def test_test_endpoint_marks_the_mac_the_way_the_panel_does(monkeypatch):
    """`Check Portal` used to mean "the handshake worked". Now it means the
    panel considers this MAC usable - which is what the chain builder reads."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import MacAddress, Portal
    from app.portal.pool import POOL
    from app.routers import api_portals

    w = Wired(monkeypatch)

    async def fake_get(session):
        return w.client(session.mac, password=session.password,
                        identity_mode=session.identity_mode)

    monkeypatch.setattr(POOL, "get", fake_get)
    async with SessionLocal() as s:
        p = Portal(name="mock", base_url="http://test/mock/c/", resolved_url=PORTAL,
                   enabled=True)
        s.add(p)
        await s.flush()
        for i, mac in enumerate((GOOD, EXPIRED, BANNED)):
            s.add(MacAddress(portal_id=p.id, mac=mac, order=i))
        await s.commit()
        pid = p.id

    async with SessionLocal() as s:
        out = await api_portals.test_portal(pid, db=s)
    by_mac = {r["mac"]: r for r in out["results"]}
    assert by_mac[GOOD]["status"] == "online" and by_mac[GOOD]["online"] is True
    assert by_mac[EXPIRED]["status"] == "expired" and by_mac[EXPIRED]["online"] is False
    assert by_mac[BANNED]["status"] == "banned" and "blocked" in by_mac[BANNED]["detail"]
    assert by_mac[GOOD]["sn"], "the GUI shows what identity this portal is being told"

    async with SessionLocal() as s:
        rows = (await s.execute(select(MacAddress).where(MacAddress.portal_id == pid))
                ).scalars().all()
        stored = {m.mac: m for m in rows}
    assert stored[EXPIRED].online is False
    assert "blocked" in (stored[BANNED].last_error or "")
    assert stored[GOOD].last_error in (None, "")
    await flush_logs()


async def test_identity_settings_are_persisted_and_validated(monkeypatch):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import MacAddress, Portal
    from app.routers import api_portals
    from fastapi import HTTPException

    async def row_of(s, pid):
        macs = list((await s.execute(select(MacAddress)
                                     .where(MacAddress.portal_id == pid)
                                     .order_by(MacAddress.order))).scalars().all())
        return api_portals._portal_row(await s.get(Portal, pid), macs)

    async with SessionLocal() as s:
        pid = (await api_portals.create_portal(
            {"name": "ident", "base_url": "http://p/c/",
             # the GUI sends a textarea string; objects have to work too, or a
             # captured serial can only be pinned with a database edit
             "macs": [{"mac": GOOD, "sn": "PIN-1"}],
             "identity_mode": "minimal", "stb_timezone": "Europe/Oslo"}, db=s))["id"]
        row = await row_of(s, pid)
        assert row["identity_mode"] == "minimal" and row["stb_timezone"] == "Europe/Oslo"
        assert row["macs"][0]["sn"] == "PIN-1"

        # the textarea round trip must not silently drop the pin
        await api_portals.update_portal(pid, {"macs": f"{GOOD}, 00:1A:79:AA:AA:09"}, db=s)
        await s.commit()
        row = await row_of(s, pid)
        assert {m["mac"] for m in row["macs"]} == {GOOD, "00:1A:79:AA:AA:09"}
        assert row["macs"][0]["sn"] == "PIN-1"

        with pytest.raises(HTTPException) as bad:
            await api_portals.update_portal(pid, {"identity_mode": "mag3000"}, db=s)
        assert bad.value.status_code == 400 and "mag250" in bad.value.detail
        await s.rollback()
    await flush_logs()


def test_chain_skips_macs_the_portal_said_no_to():
    from app.services.stream_manager import StreamManager

    class M:
        def __init__(self, mac, status):
            self.mac, self.status = mac, status

    macs = [M(GOOD, "online"), M(EXPIRED, "expired"), M(BANNED, "banned"), M("00:1A:79:00:00:09", "offline")]
    picked = StreamManager._pick_macs(macs, 1, "macs_first", set())
    assert [m.mac for m in picked] == [GOOD, "00:1A:79:00:00:09"], (
        "expired/banned are the panel's own verdicts and cannot be retried; "
        "our own transport verdicts stay in play")
    # every MAC unusable -> no chain entry at all, and never a crash
    assert StreamManager._pick_macs([M(EXPIRED, "expired")], 1, "macs_first", set()) is None
    # portal_first still takes exactly one, and it is a usable one
    picked = StreamManager._pick_macs(macs, 7, "portal_first", set())
    assert [m.mac for m in picked] == [GOOD]


def test_never_checked_macs_stay_in_the_chain():
    class M:
        def __init__(self, mac, status=None):
            self.mac, self.status = mac, status

    from app.services.stream_manager import StreamManager
    macs = [M(GOOD, "unknown"), M(EXPIRED, None)]
    assert len(StreamManager._pick_macs(macs, 1, "macs_first", set())) == 2


async def test_fetch_prefers_a_mac_the_panel_has_not_disabled(monkeypatch):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import MacAddress, Portal
    from app.portal.pool import POOL
    from app.services.fetch_jobs import Job, _prepare_client

    picked: list = []

    class FakeClient:
        def __init__(self, mac):
            self.mac = mac
            self.closed = False

        async def handshake(self):
            return "tok"

        async def refresh_account(self):
            return account_verdict(profile={}, info={"phone": "2032-12-31"}, token="t")

    async def fake_get(session):
        picked.append(session.mac)
        return FakeClient(session.mac)

    monkeypatch.setattr(POOL, "get", fake_get)
    async with SessionLocal() as s:
        p = Portal(name="f", base_url="http://test/mock/c/", resolved_url=PORTAL, enabled=True)
        s.add(p)
        await s.flush()
        # order puts the banned MAC first, which is exactly what used to happen
        s.add(MacAddress(portal_id=p.id, mac=BANNED, order=0, status="banned", online=False))
        s.add(MacAddress(portal_id=p.id, mac=GOOD, order=1, status="online", online=True))
        await s.commit()
        pid = p.id

    job = Job(id="j1", kind="fetch", portal_id=pid)
    client, name = await _prepare_client(job)
    assert client.mac == GOOD and picked == [GOOD]
    async with SessionLocal() as s:
        rows = (await s.execute(select(MacAddress).where(MacAddress.portal_id == pid)
                                .order_by(MacAddress.order))).scalars().all()
        assert rows[0].status == "banned", "the fetch must not promote the MAC it skipped"
    await flush_logs()


# =========================================================================== #
# the knobs have to be reachable from outside the test process, or they are
# not configuration - they are implementation detail
# =========================================================================== #
async def test_the_identity_knobs_are_driveable_through_the_control_endpoint(monkeypatch):
    w = Wired(monkeypatch)
    out = await w.control(require_prehash=1, profile_mode="none", fingerprint_required=1)
    assert out["state"]["require_prehash"] == 1 and out["state"]["profile_mode"] == "none"
    # a control endpoint that answers KeyError while applying a *valid* knob is
    # worse than one that does not have the knob: the caller cannot tell whether
    # the portal is now strict or still lenient
    seen = await w.state()
    assert seen["state"]["fingerprint_required"] == 1

    client = w.client(GOOD, identity_mode=MINIMAL)
    assert await client.handshake(), "a strict panel and a missing profile are fine"

    st = await w.state()
    # the counters are the point: they prove the *panel-side* settings took effect
    # through the endpoint, which is what makes them knobs and not internals
    assert st["counters"]["handshakes"] >= 2, "stage 1 (refused) + stage 3 (prehash)"
    assert st["seen_handshakes"], "the portal recorded what it was asked for"
    assert len(st["seen_handshakes"][-1]["prehash"]) == 40, "sha1, sent as the panel asked"
    # `none` means the *panel* has no get_profile at all, so it counts the
    # request it refused - and our client must treat the 404 as "no answer",
    # not as a reason to retry the minimal shape into the same wall forever
    assert st["counters"]["profile_calls"] == 1, "refused once, not retried on 404"


def test_the_announced_user_agent_is_one_value_for_the_whole_pipeline():
    """Portal calls, stream probes and ffmpeg's -user_agent must agree.

    Three literals that start identical drift the first time someone changes one
    to satisfy a panel - and then a probe reports success for a stream path that
    is still 403ing, which is the worst kind of green light.
    """
    from app.portal import resolver
    from app.services import probe, stream_manager

    assert client_mod.MAG_UA is STB_UA
    assert resolver.MAG_UA is STB_UA
    assert probe.MAG_UA is STB_UA and stream_manager.MAG_UA is STB_UA


# =========================================================================== #
# the install that is already running
# =========================================================================== #
async def test_an_existing_install_gets_the_columns_it_is_promised(tmp_path):
    """`_NEW_COLUMNS` has to work on a database, not only in a diff.

    A column that the model declares and the migration forgets is not a missing
    feature - it is a container that restart-loops, because every `SELECT
    portals` then dies with `no such column: identity_mode`. So: build the
    current schema, take the new columns away again, run the migration the
    startup path runs, and expect them back - twice, because boot may well do it
    twice.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import models
    from app.database import _NEW_COLUMNS, _add_missing_columns

    eng = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    try:
        async with eng.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
            for table, columns in _NEW_COLUMNS.items():
                for col in columns:
                    await conn.run_sync(
                        lambda c, t=table, n=col: c.execute(
                            text(f'ALTER TABLE {t} DROP COLUMN {n}')))

        for _ in range(2):
            async with eng.begin() as conn:
                await conn.run_sync(_add_missing_columns)

        def cols():
            insp = inspect(eng.sync_engine)
            return {t: {c["name"] for c in insp.get_columns(t)} for t in _NEW_COLUMNS}
        async with eng.connect() as conn:
            got = (await conn.run_sync(lambda c: {
                t: {col["name"] for col in inspect(c).get_columns(t)} for t in _NEW_COLUMNS}))
        for table, columns in _NEW_COLUMNS.items():
            assert set(columns) <= got[table], f"{table} lost {set(columns) - got[table]}"

        # and the defaults have to land, or every existing row reads as broken
        async with eng.connect() as conn:
            row = (await conn.execute(text(
                "SELECT identity_mode, tls_insecure FROM portals LIMIT 1"))).first()
        assert row is None, "empty table: nothing to check, and no crash"
    finally:
        await eng.dispose()


async def test_defaults_land_on_rows_that_predate_the_columns(tmp_path):
    """A portal created before R1 must not boot into `identity_mode IS NULL`."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app import models
    from app.database import _add_missing_columns

    eng = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'old2.db').as_posix()}")
    try:
        async with eng.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        # a row written while the column existed, then the column goes away - that
        # is what "upgrading an install" means for the ALTER below (ORM insert, so
        # every python-side default behaves like it does in production)
        async with AsyncSession(eng) as sess:
            sess.add(models.Portal(name="old", base_url="http://h/c/"))
            await sess.commit()
        async with eng.begin() as conn:
            await conn.run_sync(lambda c: c.execute(
                text("ALTER TABLE portals DROP COLUMN identity_mode")))
            await conn.run_sync(_add_missing_columns)
        async with eng.connect() as conn:
            mode = (await conn.execute(text("SELECT identity_mode FROM portals"))).scalar()
        assert mode == "mag250", (
            "ALTER TABLE ADD COLUMN ... DEFAULT fills existing rows; without the "
            "default an old portal would compare NULL against 'mag250' forever "
            "and never get the fingerprint it needs")
    finally:
        await eng.dispose()

