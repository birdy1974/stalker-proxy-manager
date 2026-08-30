"""
Shared test harness: real `StalkerClient`, real mock portal, no network.

Three portal test files need the same thing - a client whose transport is the
built-in mock portal, so the tests drive the code under test instead of a stub -
so it lives here once. Copy-pasting it is how a harness quietly diverges: the
copy that forgot one patch is the test that passes against the internet.

Every module that builds its own client is patched - the pooled portal session,
discovery, and the Xtream bridge (R7), which talks to `player_api.php` outside the
pool. The list is one place on purpose; see `CLIENT_BUILDERS` for what happens to a
module that is forgotten.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from app.portal.client import StalkerClient
from app.portal.mock_portal import router as MOCK_ROUTER

PORTAL = "http://test/mock/c/portal.php"
GOOD = "00:1A:79:AA:AA:01"
EXPIRED = "00:1A:79:BB:BB:01"
BANNED = "00:1A:79:CC:CC:01"


class Wired:
    """A `StalkerClient` (and a resolver) whose transport is the mock portal."""

    #: every module that builds its own outbound client. A new one added to the app
    #: without appearing here is a test that quietly talks to the internet: the
    #: sandbox proxies every hostname, so an unpatched call answers 404 and a test
    #: that asserts "the portal refused" goes green for the wrong reason.
    CLIENT_BUILDERS = ("app.portal.client", "app.portal.resolver",
                       "app.portal.mock_portal", "app.services.xtream_bridge")

    def __init__(self, monkeypatch) -> None:
        import importlib

        app = FastAPI()
        app.include_router(MOCK_ROUTER)
        self._transport = httpx.ASGITransport(app=app)
        for name in self.CLIENT_BUILDERS:
            mod = importlib.import_module(name)
            if hasattr(mod, "outbound_client"):
                monkeypatch.setattr(mod, "outbound_client", self._factory)

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
