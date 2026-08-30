"""
Shared test harness: real `StalkerClient`, real mock portal, no network.

Three portal test files need the same thing - a client whose transport is the
built-in mock portal, so the tests drive the code under test instead of a stub -
so it lives here once. Copy-pasting it is how a harness quietly diverges: the
copy that forgot one patch is the test that passes against the internet.

Both client factories are patched, deliberately:

  * `app.portal.client.outbound_client`   - the pooled portal session;
  * `app.portal.resolver.outbound_client` - discovery builds its own client.

Patching only the first is a trap: the sandbox here proxies *every* host, so a
resolver that was not patched gets a real 404 from the proxy and a test asserting
"the portal answered" still goes green. The second patch is what keeps resolve
tests honest.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from app.portal import client as client_mod
from app.portal import resolver as resolver_mod
from app.portal.client import StalkerClient
from app.portal.mock_portal import router as MOCK_ROUTER

PORTAL = "http://test/mock/c/portal.php"
GOOD = "00:1A:79:AA:AA:01"
EXPIRED = "00:1A:79:BB:BB:01"
BANNED = "00:1A:79:CC:CC:01"


class Wired:
    """A `StalkerClient` (and a resolver) whose transport is the mock portal."""

    def __init__(self, monkeypatch) -> None:
        app = FastAPI()
        app.include_router(MOCK_ROUTER)
        self._transport = httpx.ASGITransport(app=app)
        monkeypatch.setattr(client_mod, "outbound_client", self._factory)
        monkeypatch.setattr(resolver_mod, "outbound_client", self._factory)

    def _factory(self, **kwargs):
        # `outbound_client` is a *sync* factory, and the client it builds must
        # not be told to verify or proxy: `transport=` and a verify policy are
        # mutually exclusive in httpx.
        kwargs.pop("insecure", None)
        kwargs.pop("verify", None)
        kwargs.pop("trust_env", None)
        kwargs.pop("proxy", None)
        return httpx.AsyncClient(transport=self._transport, base_url="http://test", **kwargs)

    def client(self, mac: str = GOOD, **kw) -> StalkerClient:
        kw.setdefault("tls_insecure", False)
        return StalkerClient(PORTAL, mac, **kw)

    async def state(self) -> dict:
        async with httpx.AsyncClient(transport=self._transport, base_url="http://test") as ac:
            return (await ac.get("/mock/_state")).json()

    async def control(self, **payload) -> dict:
        """Drive the portal through the endpoint the GUI and a curl use."""
        async with httpx.AsyncClient(transport=self._transport, base_url="http://test") as ac:
            return (await ac.post("/mock/_control", json=payload)).json()
