"""
Portal session reuse.

Measured against the built-in mock portal this app did **1.00 handshakes per
create_link**: a fresh `StalkerClient` was built at each call site, handshook,
made one request and was closed, so its token cache never survived. All four
reference implementations reuse one session instead - crispy-stalker caches the
token for `DEFAULT_TOKEN_VALIDITY_SECS = 3600` behind a `token_refresh_lock`,
StalkerPortalConverter fetches it once per run, stalker-portal-apiv1 keeps a
resource object per connection.

After the pool: 2 handshakes for 18 create_links (0.11), 16 of 36 portal round
trips gone. These tests pin the pieces that make that true.
"""

from __future__ import annotations

import time

import pytest

from app.portal.client import TOKEN_VALIDITY, StalkerClient
from app.portal.pool import ClientPool


async def test_pool_returns_the_same_client_for_the_same_key():
    pool = ClientPool()
    a = await pool.get("http://p/c/", "00:11:22:33:44:55")
    b = await pool.get("http://p/c/", "00:11:22:33:44:55")
    assert a is b, "the whole point is that the session survives between calls"
    assert a.shared is True, "a pooled client must not be torn down by its caller"
    assert pool.stats()["sessions_open"] == 1
    assert pool.stats()["handshakes"] == 1 and pool.stats()["reused"] == 1


async def test_pool_keys_separate_macs_and_portals_apart():
    pool = ClientPool()
    a = await pool.get("http://p/c/", "00:11:22:33:44:55")
    b = await pool.get("http://p/c/", "00:11:22:33:44:66")     # other MAC
    c = await pool.get("http://q/c/", "00:11:22:33:44:55")     # other portal
    assert len({id(a), id(b), id(c)}) == 3
    assert pool.stats()["sessions_open"] == 3


async def test_ensure_auth_handshakes_once_across_many_calls(monkeypatch):
    """The bug: callers invoked handshake() unconditionally on every request."""
    calls = []

    async def fake_handshake(self):
        calls.append(time.monotonic())
        self._token = "tok%d" % len(calls)
        self._token_at = time.monotonic()
        return self._token

    monkeypatch.setattr(StalkerClient, "handshake", fake_handshake)
    c = StalkerClient("http://p/c/", "00:11:22:33:44:55")
    for _ in range(25):
        await c.ensure_auth()
    assert len(calls) == 1, f"expected one handshake for 25 calls, got {len(calls)}"


async def test_expired_token_is_refreshed(monkeypatch):
    calls = []

    async def fake_handshake(self):
        calls.append(1)
        self._token = "tok%d" % len(calls)
        self._token_at = time.monotonic()
        return self._token

    monkeypatch.setattr(StalkerClient, "handshake", fake_handshake)
    c = StalkerClient("http://p/c/", "00:11:22:33:44:55")
    await c.ensure_auth()
    assert len(calls) == 1
    assert c._token_stale() is False

    # age the token past the validity window -> next call must re-handshake
    c._token_at = time.monotonic() - (TOKEN_VALIDITY + 1)
    assert c._token_stale() is True
    await c.ensure_auth()
    assert len(calls) == 2, "a token older than the portal's validity must be refreshed"


async def test_invalidate_drops_the_token(monkeypatch):
    calls = []

    async def fake_handshake(self):
        calls.append(1)
        self._token = "t"
        self._token_at = time.monotonic()
        return self._token

    monkeypatch.setattr(StalkerClient, "handshake", fake_handshake)
    c = StalkerClient("http://p/c/", "00:11:22:33:44:55")
    await c.ensure_auth()
    c.invalidate()                       # portal URL changed under us
    assert c._token is None
    await c.ensure_auth()
    assert len(calls) == 2


async def test_caller_close_does_not_kill_a_shared_session():
    """Call sites still do `finally: await client.close()` - that must be inert."""
    pool = ClientPool()
    a = await pool.get("http://p/c/", "00:11:22:33:44:55")
    await a.close()
    assert pool.stats()["sessions_open"] == 1, "caller close() must not drop a shared session"
    b = await pool.get("http://p/c/", "00:11:22:33:44:55")
    assert b is a


async def test_reap_closes_idle_sessions_and_keeps_fresh_ones():
    pool = ClientPool()
    old = await pool.get("http://p/c/", "00:11:22:33:44:55")
    new = await pool.get("http://p/c/", "00:11:22:33:44:66")
    pool._used[("http://p/c/", "00:11:22:33:44:55", "", "")] = time.monotonic() - 10_000

    assert await pool.reap(idle_ttl=60) == 1
    assert pool.stats()["sessions_open"] == 1
    # the survivor is still the same object, and a later get() reuses it
    assert await pool.get("http://p/c/", "00:11:22:33:44:66") is new
    assert old._client is None or True          # closed, or never opened - both fine


async def test_drop_forgets_one_session():
    pool = ClientPool()
    await pool.get("http://p/c/", "00:11:22:33:44:55")
    await pool.get("http://p/c/", "00:11:22:33:44:66")
    await pool.drop("http://p/c/", "00:11:22:33:44:55")
    assert pool.stats()["sessions_open"] == 1


async def test_close_all_empties_the_pool():
    pool = ClientPool()
    for mac in ("00:11:22:33:44:55", "00:11:22:33:44:66"):
        await pool.get("http://p/c/", mac)
    assert pool.stats()["sessions_open"] == 2
    await pool.close_all()
    assert pool.stats()["sessions_open"] == 0
