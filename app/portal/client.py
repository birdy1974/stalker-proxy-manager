"""
Lenient async client for Stalker/Ministra portals (based on STB-Proxy's stb.py,
hardened with the Phase-1 findings and the quirks you reported):

  * token via `handshake`, then `Authorization: Bearer <token>` + `mac` cookie
  * 401/expired token            -> transparent re-handshake + one retry
  * `is_series` may be int 0/1 or str "0"/"1"    -> parsed loosely
  * series items have EMPTY `cmd` (they are containers) -> still accepted
  * VOD and Series BOTH live under type=vod on many portals; on others series
    use type=series. We try both and accept whichever answers.
  * paginate with `p=` (pages of ~14), budgets are enforced by the caller
  * every request/response is summarized to the debug log for fault finding
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import PORTAL_HTTP_TIMEOUT

log = logging.getLogger("spm.portal")

MAG_UA = (
    "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
    "(KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 2721 Safari/533.3"
)


class PortalError(RuntimeError):
    """Raised for portal-level failures (offline, unauthorized, bad payload)."""


def truthy(v: Any) -> bool:
    """Loose bool: accepts 1/'1'/True/'true' (is_series quirk)."""
    return v in (1, True, "1", "true", "True", "yes")


@dataclass
class Page:
    items: list[dict]
    total: int  # total_items reported by the portal (0 if unknown)


class StalkerClient:
    def __init__(self, portal_url: str, mac: str, password: str | None = None,
                 proxy: str | None = None, timeout: float = PORTAL_HTTP_TIMEOUT) -> None:
        self.portal_url = portal_url
        self.mac = mac
        self.password = password
        self.proxy = proxy
        self.timeout = timeout
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()  # serialize token refresh

    # ------------------------------------------------------------------ http
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict = {
                "headers": {"User-Agent": MAG_UA},
                "cookies": {"mac": self.mac, "stb_lang": "en", "timezone": "Europe/Amsterdam"},
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, params: dict, *, retried: bool = False) -> Any:
        """Single GET with token auth + one re-handshake retry."""
        http = await self._http()
        if self._token is None:
            await self.handshake()
        log.debug("GET %s params=%s", self.portal_url, {k: v for k, v in params.items() if k != "JsHttpRequest"})
        try:
            r = await http.get(self.portal_url, params=params)
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"request failed: {type(exc).__name__}: {exc}") from exc
        if r.status_code in (401, 403) and not retried:
            log.info("portal answered %s -> re-handshaking once", r.status_code)
            async with self._lock:
                self._token = None
                await self.handshake()
            return await self._get(params, retried=True)
        if r.status_code != 200:
            raise PortalError(f"HTTP {r.status_code}")
        if not r.content:
            raise PortalError("empty reply (portal dropped connection or IP is blocked)")
        try:
            return r.json()
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"invalid JSON payload ({len(r.content)} bytes)") from exc

    # ------------------------------------------------------------- handshake
    async def handshake(self) -> str:
        http = await self._http()
        params = {"type": "stb", "action": "handshake", "prehash": "0", "JsHttpRequest": "1-xml"}
        log.info("handshake %s mac=%s", self.portal_url, self.mac)
        try:
            r = await http.get(self.portal_url, params=params)
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"handshake failed: {type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            raise PortalError(f"handshake HTTP {r.status_code}")
        try:
            token = r.json()["js"]["token"]
        except Exception as exc:  # noqa: BLE001
            raise PortalError("handshake: no token in reply (MAC unknown/blocked?)") from exc
        self._token = token
        # Authorization header is set per-request because httpx cookies persist
        http.headers["Authorization"] = "Bearer " + token
        log.debug("handshake ok, token=%s…", token[:8])
        return token

    async def profile(self) -> dict:
        data = await self._get({"type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml"})
        return data.get("js") or {}

    async def account_expires(self) -> str | None:
        """Portals report expiry in account_info.get_main_info 'phone' (STB-Proxy convention)."""
        try:
            data = await self._get({"type": "account_info", "action": "get_main_info",
                                    "JsHttpRequest": "1-xml"})
            js = data.get("js")
            if isinstance(js, dict):
                return js.get("phone") or js.get("end_date")
            if isinstance(js, list) and js:
                return js[0].get("phone")
        except PortalError:
            raise
        except Exception:  # noqa: BLE001
            pass
        return None

    # --------------------------------------------------------------- genres
    async def _genres_for_type(self, type_: str) -> list[dict]:
        data = await self._get({"type": type_, "action": "get_genres", "JsHttpRequest": "1-xml"})
        js = data.get("js")
        if isinstance(js, dict):
            return [{"id": k, **(v if isinstance(v, dict) else {"title": str(v)})} for k, v in js.items()]
        if isinstance(js, list):
            return js
        return []

    async def _categories_for_type(self, type_: str) -> list[dict]:
        data = await self._get({"type": type_, "action": "get_categories", "JsHttpRequest": "1-xml"})
        js = data.get("js")
        return js if isinstance(js, list) else []

    async def live_genres(self) -> list[dict]:
        return await self._genres_for_type("itv")

    async def vod_genres(self) -> list[dict]:
        """VOD categories; portals without categories -> empty list (caller fetches unfiltered)."""
        try:
            return await self._categories_for_type("vod")
        except PortalError:
            return []

    async def series_genres(self) -> list[dict]:
        """Series categories: type=series first, fall back to vod (merged portals)."""
        try:
            cats = await self._categories_for_type("series")
            if cats:
                return cats
        except PortalError:
            pass
        return await self.vod_genres()

    # ------------------------------------------------------------ item lists
    async def live_channels(self, genre_id: str | None, page: int) -> Page:
        params: dict = {"type": "itv", "action": "get_ordered_list",
                        "force_ch_link_check": "", "fav": "0", "sortby": "number",
                        "hd": "0", "p": str(page), "JsHttpRequest": "1-xml"}
        if genre_id:
            params["genre"] = genre_id
        data = await self._get(params)
        js = data.get("js") or {}
        items = js.get("data") if isinstance(js, dict) else None
        total = int(js.get("total_items", 0) or 0) if isinstance(js, dict) else 0
        return Page(items=items if isinstance(items, list) else [], total=total)

    async def vod_list(self, category_id: str | None, page: int) -> Page:
        """VOD items. `sortby=added` + `not_ended=0` per spec; is_series=0 tolerated loosely."""
        params: dict = {"type": "vod", "action": "get_ordered_list",
                        "sortby": "added", "not_ended": "0", "p": str(page),
                        "JsHttpRequest": "1-xml"}
        if category_id:
            params["category"] = category_id
        data = await self._get(params)
        js = data.get("js") or {}
        items = js.get("data") if isinstance(js, dict) else []
        # Loose split: VOD = items where is_series is falsy-ish (0/'0'/None all OK)
        filtered = [i for i in (items or []) if not truthy(i.get("is_series"))]
        total = int(js.get("total_items", 0) or 0) if isinstance(js, dict) else 0
        return Page(items=filtered, total=total)

    async def series_list(self, category_id: str | None, page: int) -> Page:
        """Series items. Tries type=series first, then type=vod (merged portals)."""
        base: dict = {"action": "get_ordered_list", "sortby": "added", "not_ended": "0",
                      "p": str(page), "JsHttpRequest": "1-xml"}
        if category_id:
            base["category"] = category_id
        last_err: Exception | None = None
        for type_ in ("series", "vod"):
            try:
                data = await self._get({**base, "type": type_})
                js = data.get("js") or {}
                items = js.get("data") if isinstance(js, dict) else []
                if not isinstance(items, list):
                    items = []
                # Loose: when fetching via vod, keep only series-ish items; when
                # via series endpoint everything is a series by definition.
                if type_ == "vod":
                    items = [i for i in items if truthy(i.get("is_series"))]
                total = int(js.get("total_items", 0) or 0) if isinstance(js, dict) else 0
                if items or page == 1:
                    return Page(items=items, total=total)
            except PortalError as exc:
                last_err = exc
                continue
        if last_err:
            raise last_err
        return Page(items=[], total=0)

    # ------------------------------------------------------------ seasons/episodes
    async def series_seasons(self, series_id: str) -> list[dict]:
        """Seasons of a series. movie_id flow is the common denominator."""
        params: dict = {"type": "vod", "action": "get_ordered_list", "movie_id": series_id,
                        "sortby": "series", "p": "1", "JsHttpRequest": "1-xml"}
        data = await self._get(params)
        js = data.get("js") or {}
        items = js.get("data") if isinstance(js, dict) else []
        return items if isinstance(items, list) else []

    async def series_episodes(self, series_id: str, season_id: str | None) -> list[dict]:
        """Episodes of a season; tolerant to season_id vs season param naming."""
        params: dict = {"type": "vod", "action": "get_ordered_list", "movie_id": series_id,
                        "p": "1", "JsHttpRequest": "1-xml"}
        if season_id:
            params["season_id"] = season_id
        data = await self._get(params)
        js = data.get("js") or {}
        items = js.get("data") if isinstance(js, dict) else []
        return items if isinstance(items, list) else []

    # ---------------------------------------------------------------- links
    async def create_link(self, cmd: str, kind: str = "itv") -> str:
        """Resolve a portal `cmd` to a playable stream URL."""
        type_ = {"live": "itv", "itv": "itv", "vod": "vod", "series": "vod", "episode": "vod"}.get(kind, "itv")
        data = await self._get({
            "type": type_, "action": "create_link", "cmd": cmd, "series": "0",
            "forced_storage": "false", "disable_ad": "false", "download": "false",
            "force_ch_link_check": "false", "JsHttpRequest": "1-xml",
        })
        js = data.get("js")
        if isinstance(js, dict):
            raw = js.get("cmd") or js.get("url") or ""
            link = str(raw).split()[-1] if raw else ""     # cmds look like: ffmpeg http://...
            if link.startswith("http"):
                return link
        raise PortalError(f"create_link returned no usable url for cmd={cmd!r}")

    # ------------------------------------------------------------------ epg
    async def short_epg(self, ch_id: str) -> list[dict]:
        try:
            data = await self._get({"type": "itv", "action": "get_short_epg", "ch_id": ch_id,
                                    "size": "10", "JsHttpRequest": "1-xml"})
            js = data.get("js")
            return js if isinstance(js, list) else []
        except PortalError:
            return []
