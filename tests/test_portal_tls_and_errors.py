"""
R5 + R4: one trust policy for portal calls, and portal refusals as *codes*.

Two related problems, both from the EStalker comparison (docs/ESTALKER-COMPARISON.md):

1. `StalkerClient` built its own `httpx.AsyncClient`, so portal calls used
   certifi while the EPG/logo fetchers used the OS trust store
   (`services/http_client.py`). A panel with a self-signed chain therefore
   failed TLS on exactly the one path a user cannot work around - and the only
   fix available in the wild was `verify=False`, which EStalker does on every
   single request. Portals now share `outbound_client()`, verification stays on,
   and a broken panel gets a per-portal opt-out (`Portal.tls_insecure`) that is
   part of the pool key, so flipping it cannot leave a session behind.

2. Panels refuse with a *reason*, usually as HTTP 200 + {"js":{"error":"limit"}}.
   We parsed only the status code, so "this MAC is over its quota", "this
   channel is gone" and "your bearer expired" all arrived as
   `create_link returned no usable url` - and a 200 token refusal was not
   retried at all, so a portal that rotates bearers looked broken until the
   local TTL expired. `PortalError.code` now carries the portal's own code and
   the token-shaped refusals go through the same one re-handshake as a 401.
"""

from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.portal.client import (PortalError, StalkerClient, apply_mac_placeholder,
                               is_hls, js_error, js_has_payload, normalize_error,
                               status_for_error)
from app.portal.mock_portal import router as MOCK_ROUTER
from app.portal.pool import ClientPool, PortalSession
from app.services import http_client as hc

MAC = "00:1A:79:AA:AA:01"
PORTAL = "http://test/mock/c/portal.php"


# --------------------------------------------------------------------------- #
# harness: a real StalkerClient talking to the built-in mock portal
# --------------------------------------------------------------------------- #
class Wired:
    """StalkerClient whose transport is the mock portal's ASGI app.

    Patching `outbound_client` (instead of httpx.AsyncClient) also captures the
    kwargs, which is what the TLS-policy assertions need. `requests` records
    every outbound query so a test can count handshakes.
    """

    def __init__(self, monkeypatch) -> None:
        self.kwargs: list[dict] = []
        self.requests: list[str] = []
        app = FastAPI()
        app.include_router(MOCK_ROUTER)
        outer = self

        def factory(**kwargs):
            outer.kwargs.append(dict(kwargs))
            kwargs.pop("insecure", None)        # consumed by the real helper
            kwargs.pop("verify", None)          # replaced by the ASGI transport

            async def _spy(request):
                outer.requests.append(str(request.url.query or ""))

            kwargs["event_hooks"] = {"request": [_spy]}
            return httpx.AsyncClient(transport=ASGITransport(app=app), **kwargs)

        monkeypatch.setattr("app.portal.client.outbound_client", factory)

    @property
    def handshakes(self) -> int:
        return sum(1 for q in self.requests if "action=handshake" in q)

MOCK_APP = FastAPI()
MOCK_APP.include_router(MOCK_ROUTER)


async def control(**payload) -> None:
    """Flip a mock-portal behaviour without a real network.

    Goes through the same ASGI app the client is wired to, but with its own
    httpx client - `Wired` only patches how a *portal session* is built.
    """
    async with httpx.AsyncClient(transport=ASGITransport(app=MOCK_APP)) as c:
        await c.post("http://test/mock/_control", json=payload)


# --------------------------------------------------------------------------- #
# R5: one TLS policy, per-portal opt-out
# --------------------------------------------------------------------------- #
def test_outbound_client_verifies_with_the_os_store_by_default():
    ctx = hc.outbound_client()._transport._pool._ssl_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    # and it is the OS store, not certifi: that is the whole point of the helper
    assert ctx is hc._CTX


def test_outbound_client_insecure_is_an_explicit_optout():
    client = hc.outbound_client(insecure=True)
    assert client._transport._pool._ssl_context.verify_mode == ssl.CERT_NONE
    assert client._transport._pool._ssl_context.check_hostname is False


async def test_portal_client_now_shares_that_policy(monkeypatch):
    """The bug: StalkerClient called httpx.AsyncClient directly, so the portal
    path silently kept certifi while every other outbound call used the OS
    store - and there was no way to say 'this one panel has a broken chain'."""
    wired = Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    http = await c._http()
    assert http is not None
    assert wired.kwargs, "the portal client must be built through outbound_client()"
    kwargs = wired.kwargs[0]
    assert "insecure" in kwargs, "the TLS policy must be delegated to outbound_client()"
    assert "verify" not in kwargs, "and never decided privately by StalkerClient"
    await c._aclose()


async def test_tls_insecure_reaches_the_http_client(monkeypatch):
    wired = Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC, tls_insecure=True)
    await c._http()
    assert wired.kwargs[-1]["insecure"] is True
    await c._aclose()


async def test_pool_does_not_share_sessions_across_tls_policies():
    pool = ClientPool()
    verified = await pool.get(PortalSession("http://p/c/", MAC))
    insecure = await pool.get(PortalSession("http://p/c/", MAC, tls_insecure=True))
    assert verified is not insecure, "verification is part of a session's identity"
    assert insecure.tls_insecure is True and verified.tls_insecure is False
    assert await pool.get(PortalSession("http://p/c/", MAC)) is verified
    assert pool.stats()["sessions_open"] == 2


async def test_session_reaper_actually_runs(monkeypatch):
    """The loop that closes idle sessions died with a NameError on every pass.

    `asyncio` was imported inside the startup function only, so
    `_reap_sessions()` hit `except Exception`, logged "session reaper failed"
    and kept the socket - i.e. the leak it exists to prevent, silently, every
    300 s. A reaper that never reaps is worse than no reaper, because the
    failure is invisible in the log line that says it is working.
    """
    import time

    from app import main
    from app.portal.pool import POOL

    await POOL.get(PortalSession("http://p/c/", MAC))      # one open session, now idle
    reaped: list[int] = []
    real_reap = POOL.reap

    async def spy(*a, **kw):
        closed = await real_reap(*a, **kw)
        reaped.append(closed)
        return closed

    monkeypatch.setattr(POOL, "reap", spy)
    # make the one session look long idle
    for key in list(POOL._used):
        POOL._used[key] = time.monotonic() - 100_000

    task = asyncio.create_task(main._reap_sessions(interval=0.01))
    try:
        for _ in range(100):                       # <=1 s, normally one loop turn
            if reaped and reaped[0] == 1:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert reaped and reaped[0] == 1, f"reaper never reaped: {reaped}"


def test_portal_model_and_migration_carry_the_flag():
    from app.database import _NEW_COLUMNS
    from app.models import Portal

    assert Portal.tls_insecure.property.columns[0].default.arg is False
    assert _NEW_COLUMNS["portals"]["tls_insecure"][0] == "BOOLEAN", \
        "existing installs must get the column, or every SELECT breaks"


async def test_api_roundtrip_exposes_and_persists_the_flag():
    """The GUI toggle has to survive save + reload, and be off by default."""
    from app.routers.api_portals import _portal_row
    from app.models import Portal

    p = Portal(name="x", base_url="http://x", tls_insecure=True)
    assert _portal_row(p, [])["tls_insecure"] is True
    p.tls_insecure = False
    assert _portal_row(p, [])["tls_insecure"] is False


# --------------------------------------------------------------------------- #
# R4: error codes
# --------------------------------------------------------------------------- #
def test_normalize_error_only_reports_something_meaningful():
    assert normalize_error("Account is in use") == "account_is_in_use"
    assert normalize_error("limit") == "limit"
    assert normalize_error(0) == "" and normalize_error("0") == ""
    assert normalize_error("") == "" and normalize_error(None) == ""
    assert normalize_error("OK") == "" and normalize_error(False) == ""
    assert normalize_error("Timeout of authorization token") == "timeout_of_authorization_token"


def test_js_error_and_payload_rules():
    assert js_error({"js": {"error": "limit"}}) == "limit"
    # a portal that always sends "error": 0 next to real data is NOT refusing
    assert js_error({"js": {"error": 0, "data": [{"id": "1"}]}}) == ""
    assert js_error({"js": {"data": [], "msg": "no channels"}}) == "no_channels"
    assert js_error({"js": [1, 2]}) == ""
    assert js_error({"js": {}}) == ""
    # genre payloads come back as {"1": {...}} - usable even with a stray note
    assert js_has_payload({"js": {"1": {"title": "News"}}}) is True
    assert js_has_payload({"js": {"error": "limit", "msg": "max 1"}}) is False


async def test_portal_refusal_on_http_200_keeps_its_code(monkeypatch):
    Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    await control(**{"create_link_error": "limit"})
    try:
        with pytest.raises(PortalError) as got:
            await c.create_link("ffmpeg http://mock/ts/1000.ts", "live")
    finally:
        await control(**{"create_link_error": ""})
        await c._aclose()
    assert got.value.code == "limit"
    assert got.value.mac_suspect is True, "a limit means 'next MAC', not 'next source'"
    assert "connection limit" in got.value.hint
    # what a user actually reads in the log / GUI
    assert "connection limit for this MAC" in got.value.detail()


async def test_nothing_to_play_is_not_blamed_on_the_mac(monkeypatch):
    Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    await control(**{"create_link_error": "nothing_to_play"})
    try:
        with pytest.raises(PortalError) as got:
            await c.create_link("ffmpeg http://mock/ts/1000.ts", "live")
    finally:
        await control(**{"create_link_error": ""})
        await c._aclose()
    assert got.value.code == "nothing_to_play"
    assert got.value.mac_suspect is False


def test_status_mapping_for_the_mac_table():
    assert status_for_error(PortalError("x", code="access_denied")) == "unauthorized"
    assert status_for_error(PortalError("x", code="http_403")) == "unauthorized"
    assert status_for_error(PortalError("x", code="transport")) == "offline"
    assert status_for_error(PortalError("x", code="http_503")) == "offline"
    assert status_for_error(PortalError("x", code="limit")) == "error"
    assert status_for_error(PortalError("x")) == "error"


class _FailingTransport(httpx.AsyncBaseTransport):
    """Transport that always raises - the sandbox proxies every real host, so
    this is how a connection failure has to be produced in a test."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def handle_async_request(self, request):
        raise self._exc


@pytest.mark.parametrize("exc,code", [
    (httpx.ConnectError("connection refused"), "transport"),
    (httpx.ReadTimeout("read timed out"), "timeout"),
])
async def test_transport_failures_get_a_code(monkeypatch, exc, code):
    def factory(**kwargs):
        kwargs.pop("insecure", None)
        kwargs.pop("verify", None)
        return httpx.AsyncClient(transport=_FailingTransport(exc), **kwargs)

    monkeypatch.setattr("app.portal.client.outbound_client", factory)
    c = StalkerClient("http://portal.invalid/c/portal.php", MAC)
    with pytest.raises(PortalError) as got:
        await c.handshake()
    assert got.value.code == code
    assert status_for_error(got.value) == "offline"       # the portal is not down, we are
    await c._aclose()


async def test_http_status_failures_get_a_code(monkeypatch):
    Wired(monkeypatch)
    c2 = StalkerClient(PORTAL, "00:1A:79:DE:AD:00")               # unknown MAC
    with pytest.raises(PortalError) as got2:
        await c2.all_channels()
    assert got2.value.code == "http_403", "403 for an unenrolled MAC is a credential problem"
    assert status_for_error(got2.value) == "unauthorized"
    await c2._aclose()


async def test_expired_bearer_answered_with_200_is_retried_once(monkeypatch):
    """Ministra expires a session with HTTP 200 + {"js":{"error":"token"}}.

    Without the retry the whole portal looks broken until the local token TTL
    (3000 s) runs out; with an unconditional retry every refusal would double
    the portal load. So: exactly one re-handshake, then it must succeed.
    """
    wired = Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    await c.handshake()
    base_handshakes = wired.handshakes
    await control(**{"token_rejects": 1})
    try:
        link = await c.create_link("ffmpeg http://mock/ts/1000.ts", "live")
    finally:
        await c._aclose()
    assert link.endswith("1000.ts")
    assert wired.handshakes == base_handshakes + 1, \
        "expected exactly one transparent re-handshake"


async def test_persistent_token_refusal_does_not_loop(monkeypatch):
    """One retry only - a portal that rejects every bearer must not be
    hammered into an endless re-handshake loop by the fallback chain."""
    wired = Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    await control(**{"token_rejects": 99})
    try:
        with pytest.raises(PortalError) as got:
            await c.create_link("ffmpeg http://mock/ts/1000.ts", "live")
    finally:
        await control(**{"token_rejects": 0})
        await c._aclose()
    assert got.value.code == "token"
    assert wired.handshakes <= 2, f"retried too many times: {wired.handshakes}"


# --------------------------------------------------------------------------- #
# R4: %mac% and HLS
# --------------------------------------------------------------------------- #
def test_mac_placeholder_is_filled_in_every_encoding():
    want = "00:1A:79:12:34:56"
    assert apply_mac_placeholder("http://h/ch/%mac%/1.ts", want) == f"http://h/ch/{want}/1.ts"
    assert apply_mac_placeholder("http://h/ch/%MAC%/1.ts", want) == f"http://h/ch/{want}/1.ts"
    # after a query round trip through merge_link the placeholder comes back
    # percent-encoded on both sides
    assert apply_mac_placeholder("http://h/1.ts?m=%25mac%25", want) == f"http://h/1.ts?m={want}"
    # anything else in the URL is left alone (the mock's stream path stays put)
    assert apply_mac_placeholder("http://h/a.ts?x=1", want) == "http://h/a.ts?x=1"
    assert apply_mac_placeholder("", want) == ""
    assert apply_mac_placeholder("http://h/%mac%.ts", "") == "http://h/%mac%.ts"


async def test_create_link_resolves_a_portal_mac_template(monkeypatch):
    Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    await control(**{"mac_placeholder": True})
    try:
        link = await c.create_link("ffmpeg http://mock/ts/1000.ts", "live")
    finally:
        await control(**{"mac_placeholder": False})
        await c._aclose()
    assert f"/ts/{MAC}.ts" in link, f"the placeholder survived: {link}"
    assert "%mac%" not in link.lower().replace("%25", "")


def test_is_hls_only_matches_playlists():
    assert is_hls("http://h/live/1.m3u8") is True
    assert is_hls("http://h/live/1.m3u8?token=abc") is True
    assert is_hls("http://h/live/M3U8.ts") is False
    assert is_hls("http://h/live/1.ts") is False
    assert is_hls("ffmpeg http://h/a.m3u8") is True        # tolerant: also a cmd
    assert is_hls("") is False


def test_hls_input_options_are_added_for_playlists_only():
    from app.services.ffmpeg_templates import URL_PLACEHOLDER
    from app.services.stream_manager import StreamManager

    tpl = f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"
    hls = StreamManager._ffmpeg_argv(tpl, "http://cdn/live/1.m3u8?tok=x")
    assert hls is not None
    assert "-protocol_whitelist" in hls and "ALL" in hls, \
        "an HLS input without these flags dies before the first byte"
    # they belong in front of -i: after it they would be output options
    assert hls.index("-protocol_whitelist") < hls.index("-i")

    plain = StreamManager._ffmpeg_argv(tpl, "http://cdn/live/1.ts")
    assert "-protocol_whitelist" not in plain, "no flags for a TS input"

    # a user who set their own whitelist is never overridden
    custom = StreamManager._ffmpeg_argv(
        f"ffmpeg -protocol_whitelist http,tcp -i {URL_PLACEHOLDER} -c copy pipe:1",
        "http://cdn/live/1.m3u8")
    assert custom.count("-protocol_whitelist") == 1
    assert "-allowed_extensions" in custom


async def test_stream_log_says_what_the_portal_meant(monkeypatch):
    """Regression guard for the reported 'black channel, no explanation': the
    fallback line must carry the code, not just 'no usable url for cmd=…'."""
    Wired(monkeypatch)
    c = StalkerClient(PORTAL, MAC)
    await control(**{"create_link_error": "link_fault"})
    try:
        with pytest.raises(PortalError) as got:
            await c.create_link("ffmpeg http://mock/ts/1.ts", "live")
    finally:
        await control(**{"create_link_error": ""})
        await c._aclose()
    assert got.value.detail().endswith("portal/CDN fault while building the link")
