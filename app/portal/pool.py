"""
One StalkerClient per portal *session*, reused instead of rebuilt per request.

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

So: keep the client alive, let its own TTL logic refresh the token just under the
portal's validity window, and reap clients that have been idle. Reusing the
instance also reuses the httpx connection pool, so keep-alive survives between
requests.

Why `PortalSession` is a type instead of a list of keyword arguments
----------------------------------------------------------------------
A session used to be identified by five loose parameters repeated at five call
sites, and every per-portal connection setting had to be threaded through all of
them by hand - which is exactly how a portal's proxy ended up applied to
`create_link` but not to the catalogue fetch, and how `verify` had two different
answers in one process. `from_rows()` builds the profile from the ORM rows once,
and the pool keys on `session.key`, so a new connection setting (TLS policy,
identity mode, pinned serial) cannot be *half* applied: either it is in the
dataclass, and then it is in the key and in the client, or it does not exist.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from ..config import PORTAL_HTTP_TIMEOUT
from .client import StalkerClient
from .identity import MAG250, STB_LANG, STB_TIMEZONE, normalize_mac

log = logging.getLogger("spm.portal")

# Close a session that nothing has asked for in this long. Long enough that a
# user browsing the GUI keeps one connection, short enough that a MAC that went
# away does not hold a socket open forever.
IDLE_TTL = float(os.environ.get("SPM_SESSION_IDLE_TTL", "900"))


@dataclass(frozen=True)
class PortalSession:
    """Everything that identifies one portal connection.

    `timeout` is deliberately *not* part of `key`: it does not change what the
    panel sees, and splitting a session over a 10 s vs 15 s timeout would cost a
    handshake to buy nothing. The first caller's timeout wins, as it always did.
    """

    portal_url: str
    mac: str
    password: str | None = None
    proxy: str | None = None
    timeout: float = PORTAL_HTTP_TIMEOUT
    tls_insecure: bool = False
    identity_mode: str = MAG250
    timezone: str = STB_TIMEZONE
    lang: str = STB_LANG
    sn: str | None = None
    device_id: str | None = None

    @property
    def key(self) -> tuple:
        # The TLS policy and the claimed device identity are part of a session's
        # identity: two clients for the same (portal, MAC) may not differ only in
        # verification or in their serial number, and a flag flipped in the GUI
        # must not keep serving a session built with the old one.
        return (self.portal_url, normalize_mac(self.mac), self.password or "",
                self.proxy or "", bool(self.tls_insecure), self.identity_mode,
                self.timezone or "", self.lang or "", self.sn or "", self.device_id or "")

    def client(self) -> StalkerClient:
        return StalkerClient(self.portal_url, self.mac, self.password, self.proxy,
                             self.timeout, tls_insecure=self.tls_insecure,
                             identity_mode=self.identity_mode, timezone=self.timezone,
                             lang=self.lang, sn=self.sn, device_id=self.device_id)

    @classmethod
    def from_rows(cls, portal, mac_row, *, portal_url: str | None = None,
                  timeout: float | None = None) -> "PortalSession":
        """Build the one correct connection profile for (portal row, MAC row).

        Duck-typed on purpose (`getattr` with defaults): rows from a partial
        query, and rows created before a column existed, must still produce a
        usable session instead of an AttributeError in the stream path.
        """
        url = portal_url or getattr(portal, "resolved_url", None) or getattr(portal, "base_url", "")
        return cls(
            portal_url=url,
            mac=getattr(mac_row, "mac", "") or "",
            password=getattr(mac_row, "password", None),
            proxy=getattr(portal, "proxy_url", None),
            timeout=PORTAL_HTTP_TIMEOUT if timeout is None else float(timeout),
            tls_insecure=bool(getattr(portal, "tls_insecure", False)),
            identity_mode=getattr(portal, "identity_mode", None) or MAG250,
            timezone=getattr(portal, "stb_timezone", None) or STB_TIMEZONE,
            lang=getattr(portal, "stb_lang", None) or STB_LANG,
            sn=getattr(mac_row, "sn", None),
            device_id=getattr(mac_row, "device_id", None),
        )


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[tuple, StalkerClient] = {}
        self._used: dict[tuple, float] = {}
        self._guard = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    async def get(self, session: PortalSession) -> StalkerClient:
        key = session.key
        async with self._guard:
            client = self._clients.get(key)
            if client is None:
                client = session.client()
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

    async def drop(self, session: PortalSession) -> None:
        """Forget one session - used when a portal or MAC is edited/deleted."""
        key = session.key
        async with self._guard:
            client = self._clients.pop(key, None)
            self._used.pop(key, None)
        if client is not None:
            await client._aclose()

    async def drop_mac(self, mac: str) -> int:
        """Close every pooled session that authenticated as ``mac``.

        MAC is part of the session key (index 1, normalised). Used when a MAC
        row is removed so a deleted device cannot keep a live token against the
        panel, and so a re-added MAC starts with a clean handshake.
        """
        target = normalize_mac(mac or "")
        if not target:
            return 0
        async with self._guard:
            victims = [(k, self._clients.pop(k))
                       for k in list(self._clients)
                       if k[1] == target]
            for k, _ in victims:
                self._used.pop(k, None)
        return await self._close_victims(victims)

    async def drop_portal_url(self, portal_url: str | None) -> int:
        """Close every pooled session pointed at ``portal_url`` (or base_url).

        Called on portal delete so tokens and TCP sockets of a removed panel do
        not linger until the idle reaper. Matching is exact on the key's first
        element (the URL the client was built with).
        """
        url = (portal_url or "").strip()
        if not url:
            return 0
        async with self._guard:
            victims = [(k, self._clients.pop(k))
                       for k in list(self._clients)
                       if k[0] == url]
            for k, _ in victims:
                self._used.pop(k, None)
        return await self._close_victims(victims)

    async def _close_victims(self, victims: list) -> int:
        closed = 0
        for key, client in victims:
            try:
                await client._aclose()
                closed += 1
            except Exception:  # noqa: BLE001 - never let cleanup fail the caller
                log.warning("could not close portal session %s", key[:2] if key else "?")
        return closed

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
