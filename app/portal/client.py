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
  * `get_all_channels` returns the WHOLE live list in one request where the
    portal supports it - preferred over paging through every genre
  * every request/response is summarized to the debug log for fault finding
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from ..config import PORTAL_HTTP_TIMEOUT

log = logging.getLogger("spm.portal")

MAG_UA = (
    "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
    "(KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 2721 Safari/533.3"
)

# ---------------------------------------------------------------------------
# link helpers (see create_link)
# ---------------------------------------------------------------------------
# Per-session, single-use parameters. A cmd carrying one of these is an ALREADY
# RESOLVED link: handing it back to create_link makes some portals rebuild the
# URL from partial state - observed in the wild:
#     request   ...&stream=392166&extension=ts&play_token=<old>
#     answer    ...&stream=&extension=ts&play_token=<new>       <- id gone!
# so they are stripped before asking for a link, and the fresh token from the
# answer is merged into the URL we asked for instead of trusting the answer.
VOLATILE_PARAMS = frozenset({
    "play_token", "token", "tok", "auth", "auth_key", "authkey", "key",
    "signature", "sig", "sign", "session", "sess", "st", "e", "exp",
    "expires", "expire", "md5", "hash",
})
URL_SCHEMES = ("http://", "https://", "rtsp://", "rtsps://", "rtmp://",
               "rtmps://", "udp://", "rtp://", "mms://")


def extract_url(raw: Any) -> str:
    """
    Pick the stream URL out of a portal cmd.

    Portals are creative here: 'ffmpeg http://…', 'ffrt http://…', the bare
    URL, the whole cmd percent-encoded, or the URL followed by extra ffmpeg
    arguments. Returns '' when no URL is recognisable.
    """
    text = str(raw or "").strip().strip("\"'")
    if not text:
        return ""
    # some panels return the cmd fully percent-encoded ('ffmpeg http%3A%2F%2F…')
    if "://" not in text and "%3a%2f%2f" in text.lower():
        text = unquote(text)
    for tok in text.split():
        candidate = tok.strip("\"'")
        if candidate.lower().startswith(URL_SCHEMES):
            return candidate
    return ""


def strip_volatile(url: str) -> str:
    """Remove single-use token parameters from a URL (keeps everything else)."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in VOLATILE_PARAMS]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept, safe=":"), parts.fragment))


def sanitize_cmd(cmd: Any) -> str:
    """Version of `cmd` that is safe to send to create_link (no stale token)."""
    text = str(cmd or "").strip()
    url = extract_url(text)
    if not url:
        return text
    cleaned = strip_volatile(url)
    return text.replace(url, cleaned) if cleaned != url else text


def merge_link(answer: str, requested: str) -> str:
    """
    Repair an answer that lost parts of the request.

    Some portals rebuild the link instead of echoing it and drop or blank
    parameters while doing so ('&stream=392166' -> '&stream='), which leaves
    ffmpeg with an unplayable URL and the player with an empty response.
    Same scheme/host/path only: request parameters are restored (blanked or
    missing ones), while everything the portal added - above all the fresh
    play_token - wins.
    """
    if not requested:
        return answer
    req, got = urlsplit(requested), urlsplit(answer)
    if req.netloc != got.netloc or req.path != got.path:
        return answer                      # different server: nothing to repair
    req_pairs = parse_qsl(req.query, keep_blank_values=True)
    got_pairs = parse_qsl(got.query, keep_blank_values=True)
    got_map = dict(got_pairs)
    merged: list[tuple[str, str]] = []
    req_keys = {k for k, _ in req_pairs}
    for k, v in req_pairs:
        if k.lower() in VOLATILE_PARAMS:
            continue                       # token always comes from the answer
        merged.append((k, got_map[k] or v if k in got_map else v))
    for k, v in got_pairs:                 # fresh token + anything extra
        if k.lower() in VOLATILE_PARAMS or k not in req_keys:
            merged.append((k, v))
    return urlunsplit((got.scheme, got.netloc, got.path,
                       urlencode(merged, safe=":"), got.fragment))


def mask_token(url: str) -> str:
    """URL with volatile values masked - safe to write to the log."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in VOLATILE_PARAMS else v) for k, v in pairs]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(masked, safe=":"), parts.fragment))


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

    async def all_channels(self) -> list[dict]:
        """
        The COMPLETE live channel list in ONE request.

        Real set-top boxes use `type=itv&action=get_all_channels` instead of
        walking get_ordered_list genre by genre, and it is what "fetch the whole
        list" means for channels: one HTTP round trip for the entire catalog
        instead of up to FETCH_PAGE_BUDGET pages for every enabled genre.

        Portals that do NOT implement it answer with an error or an empty
        payload; this returns [] then and the caller falls back to paging.
        """
        data = await self._get({"type": "itv", "action": "get_all_channels",
                                "JsHttpRequest": "1-xml"})
        js = data.get("js")
        if isinstance(js, dict):
            items = js.get("data")
            if not isinstance(items, list):       # some panels: {id: channel}
                items = [v for v in js.values() if isinstance(v, dict)]
        elif isinstance(js, list):
            items = js
        else:
            items = []
        rows = [i for i in items if isinstance(i, dict) and str(i.get("id", "")).strip()]
        log.info("all_channels -> %d rows", len(rows))
        return rows

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
        """
        Resolve a portal `cmd` to a playable stream URL.

        Two steps that matter on real panels (both learned the hard way):

        1. the outgoing cmd is stripped of volatile parameters. Many portals
           store - and we therefore keep - an already tokenised link; handing
           that back makes some panels rebuild the URL and lose the stream id.
        2. the answer is repaired against the request, so a portal that drops
           or blanks parameters still yields a complete URL (fresh token is
           always taken from the answer).
        """
        type_ = {"live": "itv", "itv": "itv", "vod": "vod", "series": "vod", "episode": "vod"}.get(kind, "itv")
        raw_cmd = str(cmd or "").strip()
        requested = extract_url(raw_cmd)
        out_cmd = sanitize_cmd(raw_cmd)
        if out_cmd != raw_cmd:
            log.debug("create_link: stripped stale token from cmd")
        data = await self._get({
            "type": type_, "action": "create_link", "cmd": out_cmd, "series": "0",
            "forced_storage": "false", "disable_ad": "false", "download": "false",
            "force_ch_link_check": "false", "JsHttpRequest": "1-xml",
        })
        js = data.get("js")
        raw = ""
        if isinstance(js, dict):
            raw = js.get("cmd") or js.get("url") or js.get("link") or ""
        elif isinstance(js, str):
            raw = js
        link = extract_url(raw)
        if not link:
            raise PortalError(f"create_link returned no usable url for cmd={cmd!r}")
        repaired = merge_link(link, requested)
        if repaired != link:
            log.info("create_link: portal dropped parameters -> repaired from cmd")
        log.debug("create_link -> %s", repaired)
        log.info("create_link -> %s", mask_token(repaired))
        return repaired

    # ------------------------------------------------------------------ epg
    async def short_epg(self, ch_id: str) -> list[dict]:
        try:
            data = await self._get({"type": "itv", "action": "get_short_epg", "ch_id": ch_id,
                                    "size": "10", "JsHttpRequest": "1-xml"})
            js = data.get("js")
            return js if isinstance(js, list) else []
        except PortalError:
            return []
