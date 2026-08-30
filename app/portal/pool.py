"""
One StalkerClient per (portal, MAC), reused instead of rebuilt per request.

Measured against the built-in mock portal, this app did **1.00 handshakes per
create_link** - a fresh `StalkerClient` was constructed at each of the five call
sites, did a full handshake, made one call, and was closed. `StalkerClient`
already cached its token, but the instance never outlived the request, so the
cache was dead weight: every stream open, every detail probe and every fetch
page paid an extra portal round trip and a fresh TCP connection.

The reference implementations all reuse one session:

* crispy-stalker (`src/session.rs`) caches token + cookie with
  `token_obtained_at` / `token_validity` (`DEFAULT_TOKEN_VALIDITY_SECS = 3600`)
  and serialises refresh behind a `token_refresh_lock`.
* StalkerPortalConverter fetches the token once per run and reuses it.
* stalker-portal-apiv1 keeps a resource object per connection.

So: keep the client alive, let its own TTL logic refresh the token just under
the portal's validity window, and reap clients that have been idle. Reusing the
instance also reuses the httpx connection pool, so keep-alive survives between
requests. A session is keyed by its full connection profile - including the
per-portal TLS policy - so flipping that flag in the GUI cannot leave a stale
client with the other policy in the pool.

Callers still do `finally: await client.close()` - for a pooled client that is a
no-op (see `StalkerClient.close`), so no call site has to know it is sharing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .client import PORTAL_HTTP_TIMEOUT, StalkerClient

log = logging.getLogger("spm.portal")

# Close a session that nothing has asked for in this long. Long enough that a
# user browsing the GUI keeps one connection, short enough that a MAC that went
# away does not hold a socket open forever.
IDLE_TTL = float(os.environ.get("SPM_SESSION_IDLE_TTL", "900"))


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[tuple, StalkerClient] = {}
        self._used: dict[tuple, float] = {}
        self._guard = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(portal_url: str, mac: str, password: str | None,
             proxy: str | None, tls_insecure: bool = False) -> tuple:
        # The TLS policy is part of the identity of a session: two clients for
        # the same (portal, MAC) may NOT differ only in verification, and a
        # flipped flag must not keep serving a session built with the old one.
        return (portal_url, mac, password or "", proxy or "", bool(tls_insecure))

    async def get(self, portal_url: str, mac: str, password: str | None = None,
                  proxy: str | None = None,
                  timeout: float = PORTAL_HTTP_TIMEOUT,
                  tls_insecure: bool = False) -> StalkerClient:
        key = self._key(portal_url, mac, password, proxy, tls_insecure)
        async with self._guard:
            client = self._clients.get(key)
            if client is None:
                client = StalkerClient(portal_url, mac, password, proxy, timeout,
                                       tls_insecure=tls_insecure)
                client.shared = True
                self._clients[key] = client
                self.misses += 1
            else:
                self.hits += 1
            self._used[key] = time.monotonic()
            return client

    async def reap(self, idle_ttl: float = IDLE_TTL) -> int:
        """Close sessions nothing has used recently. Returns how many."""
        now = time.monotonic()
        async with self._guard:
            stale = [k for k, t in self._used.items() if now - t > idle_ttl]
            victims = [(k, self._clients.pop(k)) for k in stale if k in self._clients]
            for k in stale:
                self._used.pop(k, None)
        closed = 0
        for key, client in victims:
            try:
                await client._aclose()
                closed += 1
            except Exception:  # noqa: BLE001 - never let cleanup fail the caller
                log.warning("could not close idle portal session %s", key[:2])
        if closed:
            log.info("closed %d idle portal session(s)", closed)
        return closed

    async def drop(self, portal_url: str, mac: str, password: str | None = None,
                   proxy: str | None = None, tls_insecure: bool = False) -> None:
        """Forget one session - used when a portal or MAC is edited/deleted."""
        key = self._key(portal_url, mac, password, proxy, tls_insecure)
        async with self._guard:
            client = self._clients.pop(key, None)
            self._used.pop(key, None)
        if client is not None:
            await client._aclose()

    async def close_all(self) -> None:
        async with self._guard:
            clients = list(self._clients.items())
            self._clients.clear()
            self._used.clear()
        for _key, client in clients:
            try:
                await client._aclose()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "sessions_open": len(self._clients),
            "handshakes": self.misses,
            "reused": self.hits,
            "reuse_pct": round(100.0 * self.hits / total, 1) if total else 0.0,
        }


POOL = ClientPool()
