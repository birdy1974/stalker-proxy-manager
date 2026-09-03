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
  * a refusal is a *code*, not just a message: many panels answer
    HTTP 200 + {"js":{"error":"limit"}}, and `limit` (this MAC is over its
    quota) has to be told apart from `nothing_to_play` (this source is dead)
  * every request/response is summarized to the debug log for fault finding
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import logging
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..config import PORTAL_HTTP_TIMEOUT
from ..services.http_client import outbound_client
from .account import AccountVerdict, account_verdict
from .capabilities import (FEATURE_MODULES, PortalVersion, enabled_modules,
                           read_version_js, supports)
# The link helpers moved to .links so the R2 policy can use them without
# importing this module (which would be a cycle); the names stay importable from
# here because probe.py, stream_manager.py, dev/check-links.py and the tests all
# reach for them on `portal.client`.
from .links import (VOLATILE_PARAMS, apply_mac_placeholder,  # noqa: F401
                    extract_url, link_request_params)
from .identity import (MAG250, MINIMAL, STB_LANG, STB_TIMEZONE, STB_UA, cookies_for,
                       derive_identity, headers_for, make_fake_bearer,
                       minimal_profile_params, missing_token, profile_params)
# The two protocol readers of the last two rounds are pure modules (no client,
# no DB), which is what lets dev/check-links.py and the unit tests drive them:
# `.epg` for the short-EPG answer, `.xtream` for the bridge in R7.
from .epg import Programme, parse_short_epg
from .xtream import XtreamCreds, harvest

# How long we keep a handshake token before refreshing it. Portals normally
# accept one for ~3600s (crispy-stalker uses exactly that); staying just under
# the limit avoids both 401 re-handshake round trips and handing out a token
# the panel is about to reject.
TOKEN_VALIDITY = float(os.environ.get("SPM_TOKEN_VALIDITY", "3000"))

log = logging.getLogger("spm.portal")

#: Send `get_profile` with the device fingerprint after a handshake (see
#: app/portal/identity.py). Set SPM_STB_PROFILE=0 for a panel that reacts badly
#: to the call - a portal that rate-limits or bans on an unexpected action is
#: worth more than the account state it reports, and this is the switch for it.
STB_PROFILE_ENABLED = os.environ.get("SPM_STB_PROFILE", "1").strip().lower() not in (
    "0", "off", "no", "false")

MAG_UA = STB_UA   # probe.py / stream_manager.py import this name for ffmpeg

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


def is_hls(url: str) -> bool:
    """
    True when a resolved link points at an HLS *playlist* rather than a file.

    Not cosmetic: a playlist input needs two extra input options or ffmpeg
    refuses to open it at all ("-protocol_whitelist" for the segment URLs,
    "-allowed_extensions ALL" for the fMP4/init segments panels like to use),
    and the browser preview has to be handed to hls.js instead of mpegts.js.
    """
    return ".m3u8" in urlsplit(str(url or "")).path.lower()


# ---------------------------------------------------------------------------
# portal error codes
# ---------------------------------------------------------------------------
# What a portal refused *with a reason*. Panels are inconsistent about how
# they say no: a real HTTP 401, an HTTP 200 carrying {"js":{"error":"limit"}},
# or a {"js":{"msg":"..."}} with no payload at all. All three arrive here and
# all three become a PortalError with a normalized `code`, because the code -
# not the message - is what tells the fallback engine whether to move to the
# next MAC or to the next source.
PORTAL_ERROR_HINTS = {
    "limit": "connection limit for this MAC (panel says it is already streaming)",
    "account_is_in_use": "the MAC is already in use on the panel",
    "max_connections": "the panel reports max connections reached",
    "nothing_to_play": "the portal has nothing to play for this item",
    "link_fault": "portal/CDN fault while building the link",
    "access_denied": "the MAC is not enrolled/authorized on this portal",
    "unauthorized": "the portal refused the credentials",
    "not_authorized": "the portal refused the credentials",
    "no_token": "handshake returned no token (MAC unknown, IP blocked?)",
    "http_401": "portal requires authentication (401)",
    "http_403": "portal forbids this client (403)",
    "empty_reply": "portal closed the connection without a body (IP blocked?)",
    "bad_json": "portal answered something that is not JSON (WAF/captive portal page?)",
    "timeout": "portal did not answer in time",
    "transport": "the portal could not be reached",
    "no_url": "the portal returned no playable URL",
}

# `js.error` values that mean "this bearer is gone", not "you are banned":
# worth exactly one transparent re-handshake (same as a real 401).
TOKEN_ERROR_CODES = frozenset({
    "token", "invalid_token", "bad_token", "no_token", "expired_token", "token_expired",
    "auth", "authorization", "unauthorized", "not_authorized", "session", "session_expired",
})

# Codes that mean "this MAC is not usable" rather than "this portal/source is down".
MAC_SUSPECT_CODES = frozenset({"limit", "account_is_in_use", "max_connections",
                               "access_denied", "unauthorized", "not_authorized",
                               "no_token", "http_401", "http_403", "blocked"})


def normalize_error(raw: Any) -> str:
    """
    A portal error value as a comparable code ('' = no error).

    'Account is in use' -> 'account_is_in_use', '403' -> '403'. Values that
    carry no information (0, '', 'ok', 'false') are NOT errors - some panels
    always answer {"error":0} next to perfectly good data.
    """
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        return str(int(raw)) if raw else ""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or text.lower() in ("ok", "0", "none", "null", "false", "success"):
        return ""
    return re.sub(r"[^0-9a-z]+", "_", text.lower()).strip("_")


def js_has_payload(data: Any) -> bool:
    """True when a `{"js": ...}` envelope carries something usable."""
    js = data.get("js") if isinstance(data, dict) else None
    if isinstance(js, dict):
        if any(js.get(k) for k in ("data", "cmd", "url", "token", "id", "play_token")):
            return True
        # genre/profile style payloads: {"1": {...}, "2": {...}}
        return any(isinstance(v, (dict, list)) and v for v in js.values())
    return bool(js)


def js_error(data: Any) -> str:
    """
    Portal error code from a `{"js": ...}` envelope, '' when the answer is fine.

    `js.error` wins; `js.msg` is only consulted when the payload is empty, so a
    panel that puts an informational msg next to real data stays readable.
    """
    js = data.get("js") if isinstance(data, dict) else None
    if not isinstance(js, dict):
        return ""
    code = normalize_error(js.get("error"))
    if code:
        return code
    if not js_has_payload(data):
        return normalize_error(js.get("msg"))
    return ""


class PortalError(RuntimeError):
    """
    Raised for portal-level failures (offline, unauthorized, bad payload).

    `code` is the machine-readable reason: the portal's own `js.error` when it
    sent one (normalized), otherwise one of ours (`timeout`, `transport`,
    `http_503`, `unauthorized`, `no_url`, ...). Callers branch on it; the
    message is for humans and the log.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code or ""

    @property
    def hint(self) -> str:
        """One-line explanation of `code` ('' when we have nothing to add)."""
        return PORTAL_ERROR_HINTS.get(self.code, "")

    @property
    def mac_suspect(self) -> bool:
        """True when this MAC - not the source or the portal - is the problem."""
        return self.code in MAC_SUSPECT_CODES

    def detail(self) -> str:
        """Message plus the human explanation of the code - for logs and GUI."""
        return f"{self}: {self.hint}" if self.hint else str(self)


def status_for_error(exc: PortalError) -> str:
    """Map a failure onto the MacAddress.status vocabulary."""
    code = (exc.code or "").lower()
    if code in ("access_denied", "unauthorized", "not_authorized", "no_token",
                "http_401", "http_403", "token", "invalid_token", "blocked"):
        return "unauthorized"
    if code in ("transport", "timeout", "empty_reply", "bad_json") or code.startswith("http_5"):
        return "offline"
    return "error"


def truthy(v: Any) -> bool:
    """Loose bool: accepts 1/'1'/True/'true' (is_series quirk)."""
    return v in (1, True, "1", "true", "True", "yes")


def _is_valid_season_item(item: dict, series_id: str) -> bool:
    """True if item is a legitimate season object, False if it is a fallback VOD movie dump."""
    if not isinstance(item, dict):
        return False
    if truthy(item.get("is_season")):
        return True
    if item.get("season_id") is not None and not item.get("cmd"):
        return True
    clean_id = series_id.split(":")[0] if ":" in series_id else series_id
    item_series_id = str(item.get("series_id", "") or item.get("movie_id", "") or "")
    if item_series_id and item_series_id in (series_id, clean_id):
        return True
    if isinstance(item.get("series"), list) and len(item.get("series")) > 0:
        return True
    if truthy(item.get("is_series")) and not item.get("cmd"):
        return True
    # Generic VOD movie dump (is_series 0/False, stream cmd, no season marker) -> False
    if not truthy(item.get("is_series")) and item.get("cmd"):
        return False
    return False


def _is_valid_episode_item(item: dict, series_id: str) -> bool:
    """True if item is an episode (not a generic movie dump from an ignored query).

    A classic-Stalker SEASON CONTAINER (a season object whose `series` field is
    the list of its episode numbers) is deliberately still accepted here: the
    fetch job recognizes and EXPANDS it into episodes (IPTVnator's
    mapRegularSeriesEpisodes). What is rejected is an explicit season object of
    the modern flavor (`is_season` set without `is_episode`) - those panels have
    real per-episode objects, and storing their season rows as episodes is how
    every season ended up as a single phantom "E02".
    """
    if not isinstance(item, dict):
        return False
    if truthy(item.get("is_season")) and not truthy(item.get("is_episode")):
        return False
    if truthy(item.get("is_episode")):
        return True
    if item.get("series_number") is not None:
        return True
    if isinstance(item.get("series"), (list, tuple)) and len(item.get("series")) >= 2:
        return True
    if item.get("video_id") or item.get("season_id"):
        return True
    if item.get("cmd"):
        return True
    return False


@dataclass
class Page:
    items: list[dict]
    total: int  # total_items reported by the portal (0 if unknown)


class StalkerClient:
    def __init__(self, portal_url: str, mac: str, password: str | None = None,
                 proxy: str | None = None, timeout: float = PORTAL_HTTP_TIMEOUT,
                 tls_insecure: bool = False, *, identity_mode: str = MAG250,
                 timezone: str = STB_TIMEZONE, lang: str = STB_LANG,
                 sn: str | None = None, device_id: str | None = None) -> None:
        self.portal_url = portal_url
        self.mac = mac
        self.password = password
        self.proxy = proxy
        self.timeout = timeout
        # What we claim to be. `minimal` sends the two-field profile and is the
        # escape hatch for a panel that dislikes a fingerprint it did not enrol.
        self.identity_mode = (MINIMAL if str(identity_mode or "").strip().lower()
                              in ("minimal", "bare", "none", "off", "no", "0") else MAG250)
        self.timezone = timezone or STB_TIMEZONE
        self.lang = lang or STB_LANG
        self.identity = derive_identity(mac, sn=sn, device_id=device_id,
                                        minimal=self.identity_mode == MINIMAL)
        #: the panel's own account state, once we have asked (see refresh_account)
        self.account: AccountVerdict | None = None
        self.account_info_error: str = ""   # why account_info was unusable
        self._handshaking = False           # re-entrancy guard, see _may_reauth
        # Per-portal opt-out for panels with a broken cert chain. TLS
        # verification is ON everywhere else, including here - see
        # app/services/http_client.py for why that distinction matters.
        self.tls_insecure = bool(tls_insecure)
        self._token: str | None = None
        self._token_at: float = 0.0        # monotonic time of the last handshake
        self._token_random: str = ""      # `js.random` from the handshake, echoed in metrics
        self._not_valid: bool = False     # `js.not_valid`: the panel wants not_valid_token=1
        self._profile: dict | None = None  # last get_profile payload (account truth lives here)
        # what the panel *is* and what it offers (R6). Cached on purpose: these
        # answers do not expire with a bearer, and `invalidate()` deliberately
        # leaves them alone - re-probing capabilities on every token rotation
        # would be two pointless requests per stream start.
        self._version: PortalVersion | None = None
        self._modules: list[str] | None = None
        self.modules_error: str = ""      # why modules is None, when it is
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()  # serialize token refresh
        # Set by the pool: a shared client's connection is not owned by the
        # caller, so its close() must not tear down anyone else's session.
        self.shared = False

    def _token_stale(self) -> bool:
        """
        Portal tokens are typically valid ~3600s (crispy-stalker uses exactly
        that). Refreshing proactively, just under the limit, is far cheaper than
        the 401 -> re-handshake -> retry round trip, and it is what lets one
        session be reused across many requests.
        """
        return self._token is not None and \
            (time.monotonic() - self._token_at) > TOKEN_VALIDITY

    # ------------------------------------------------------------------ http
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict = {
                "headers": headers_for(self.portal_url, self.identity, user_agent=MAG_UA,
                                       lang=self.lang, timezone=self.timezone),
                "cookies": cookies_for(self.portal_url, self.mac, lang=self.lang,
                                       timezone=self.timezone, adid=self.identity.adid),
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            # Same trust policy as every other outbound call in this app (OS CA
            # store, verification on) instead of a private httpx default.
            self._client = outbound_client(insecure=self.tls_insecure, **kwargs)
        return self._client

    async def close(self) -> None:
        """Close our own connection. A no-op for pooled (shared) clients."""
        if self.shared:
            return
        await self._aclose()

    async def _aclose(self) -> None:
        """Really close, shared or not - used by the pool and at shutdown."""
        self._token, self._token_at = None, 0.0
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, params: dict, *, retried: bool = False,
                   retry_on_auth: bool = True) -> Any:
        """
        Single GET with token auth, one re-handshake retry and a *code*.

        Three shapes of "no" are handled, and only the first two may retry -
        a retry is what makes one long-lived session survive a portal that
        rotates its bearer:

          1. HTTP 401/403                      -> re-handshake, retry once
          2. HTTP 200 + {"js":{"error":"token"}}  -> re-handshake, retry once
             (Ministra does answer refusals with a 200; without this the whole
             portal would look broken until the token TTL expired)
          3. anything else that is a refusal     -> PortalError(code=...)

        A portal error next to a usable payload is NOT an error: some panels
        carry `{"error":0}` or an informational `msg` on perfectly good genre
        and channel lists, so we only fail when the answer is also empty.
        """
        http = await self._http()
        if self._token is None or self._token_stale():
            async with self._lock:
                if self._token is None or self._token_stale():
                    await self.handshake()
        log.debug("GET %s params=%s", self.portal_url, {k: v for k, v in params.items() if k != "JsHttpRequest"})
        try:
            r = await http.get(self.portal_url, params=params)
        except httpx.TimeoutException as exc:
            raise PortalError(f"request failed: timeout after {self.timeout:.0f}s",
                              code="timeout") from exc
        except Exception as exc:  # noqa: BLE001 - any transport error means "unreachable"
            raise PortalError(f"request failed: {type(exc).__name__}: {exc}",
                              code="transport") from exc
        if r.status_code in (401, 403) and self._may_reauth(retry_on_auth, retried):
            log.info("portal answered %s -> re-handshaking once", r.status_code)
            async with self._lock:
                self._token = None
                await self.handshake()
            return await self._get(params, retried=True, retry_on_auth=retry_on_auth)
        if r.status_code != 200:
            raise PortalError(f"HTTP {r.status_code}", code=f"http_{r.status_code}")
        if not r.content:
            raise PortalError("empty reply (portal dropped connection or IP is blocked)",
                              code="empty_reply")
        try:
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            raise PortalError(f"invalid JSON payload ({len(r.content)} bytes)",
                              code="bad_json") from exc
        code = js_error(data)
        if code:
            if js_has_payload(data):
                log.debug("portal reported %r alongside a usable payload - ignoring it", code)
            elif code in TOKEN_ERROR_CODES and self._may_reauth(retry_on_auth, retried):
                log.info("portal refused the bearer on HTTP 200 (%s) -> re-handshaking once", code)
                async with self._lock:
                    self._token = None
                    await self.handshake()
                return await self._get(params, retried=True, retry_on_auth=retry_on_auth)
            else:
                # The portal told us WHY. Keep its code, and its text when the
                # code is not self-explanatory, so the log is actionable.
                msg = ""
                js = data.get("js") if isinstance(data, dict) else None
                if isinstance(js, dict):
                    msg = normalize_error(js.get("msg"))
                raise PortalError(f"portal said {code}"
                                  + (f" ({msg})" if msg and msg != code else ""), code=code)
        return data

    async def ensure_auth(self) -> None:
        """
        Handshake only when we do not already hold a usable token.

        The stream and preview paths used to call handshake() unconditionally
        on every request, which threw away the cached token: measured against
        the mock portal that was 1.00 handshakes per create_link. Callers that
        do not need a *fresh* identity should use this.
        """
        if self._token is None or self._token_stale():
            async with self._lock:
                if self._token is None or self._token_stale():
                    await self.handshake()

    def invalidate(self) -> None:
        """Drop the token - call after the portal URL changed under us."""
        self._token, self._token_at = None, 0.0
        self._profile = None
        self.account = None
        if self._client is not None:
            # the bearer is echoed in a cookie as well (a real box does this, and
            # some panels read the cookie instead of the header) - both have to go
            # together, or the "fresh" handshake is shadowed by a dead session
            self._client.cookies.pop("token", None)

    # ------------------------------------------------------------- handshake
    async def _handshake_stage(self, http: httpx.AsyncClient, params: dict,
                               *, bearer: str | None = None) -> tuple[dict, str, str]:
        """One handshake GET -> `(js, token, error_code)`.

        It does not raise for a refusal: a handshake *sequence* has to keep
        trying shapes, and the reason for the last failure is reported once at the
        end. Transport/timeout codes are kept distinct because "portal is down"
        must never be written to the log as "MAC not enrolled".
        """
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else None
        try:
            r = await http.get(self.portal_url, params=params, headers=headers)
        except httpx.TimeoutException:
            return {}, "", "timeout"
        except Exception as exc:  # noqa: BLE001 - unreachable is a verdict, not a bug
            log.debug("handshake transport error: %s", exc)
            return {}, "", "transport"
        if r.status_code != 200:
            return {}, "", ("unauthorized" if r.status_code in (401, 403)
                            else f"http_{r.status_code}")
        try:
            data = r.json()
        except Exception:  # noqa: BLE001 - an unparsable answer is a refusal
            log.debug("handshake reply was not JSON (%d bytes)", len(r.content or b""))
            return {}, "", "bad_json"
        js = data.get("js") if isinstance(data, dict) else None
        js = js if isinstance(js, dict) else {}
        token = str(js.get("token") or "")
        if token:
            return js, token, ""
        return js, "", (js_error(data) or "no_token")

    async def handshake(self) -> str:
        """
        Get a bearer, in up to three shapes - from least to most specific.

        1. the shape this client has always sent (no `mac` parameter);
        2. the same with `mac=<MAC>` as a parameter, which is what a real STB's
           own first request looks like - panels that key the session on it
           answer stage 1 with no token at all;
        3. **second-step auth**: a panel that answers `{"js":{"msg":"missing"}}`
           is asking for the dance - invent a bearer, send it as `Authorization`,
           and send its SHA-1 as `prehash`. It then issues the real token.

        Stage 3 is the one that decides whether a picky portal works at all, and
        it is invisible in the logs of a client that does not do it: the panel
        does not say "you forgot the prehash", it just returns nothing.
        """
        http = await self._http()
        if self._handshaking:                      # see _may_reauth
            raise PortalError("handshake already in progress", code="no_token")
        self._handshaking = True
        try:
            return await self._handshake(http)
        finally:
            self._handshaking = False

    async def _handshake(self, http: httpx.AsyncClient) -> str:
        log.info("handshake %s mac=%s (sn %s)", self.portal_url, self.mac, self.identity.sn)
        base = {"type": "stb", "action": "handshake", "JsHttpRequest": "1-xml"}
        code = "no_token"
        js: dict = {}
        token = ""
        for stage, params, bearer in (
            (1, {**base, "prehash": "0"}, None),
            (2, {**base, "prehash": "0", "token": "", "mac": self.mac}, None),
        ):
            js, token, code = await self._handshake_stage(http, params, bearer=bearer)
            if token:
                break
            if stage == 2 and missing_token(js):
                fake, prehash = make_fake_bearer()
                js, token, code = await self._handshake_stage(
                    http, {**base, "mac": self.mac, "prehash": prehash}, bearer=fake)
                if token:
                    log.info("portal required the second-step (prehash) handshake -> satisfied")
                break
        if not token:
            if code in ("transport", "timeout"):
                raise PortalError(f"handshake failed: portal unreachable ({code})", code=code)
            raise PortalError("handshake: no token in reply (MAC unknown/blocked?)"
                              + (f" - portal said {code}" if code and code != "no_token" else ""),
                              code=code or "no_token")
        self._token = token
        self._token_at = time.monotonic()
        self._token_random = str(js.get("random") or "")
        self._not_valid = str(js.get("not_valid") or "").strip().lower() in ("1", "true", "yes")
        # Authorization header + `token` cookie, like the box: the cookie is set
        # here (not in _http) because the jar is created before we have a token.
        http.headers["Authorization"] = "Bearer " + token
        http.cookies.set("token", token)
        log.debug("handshake ok, token=%s… random=%s not_valid=%s",
                  token[:8], bool(self._token_random), self._not_valid)
        if STB_PROFILE_ENABLED:
            await self.stb_profile()          # best effort, never fails the handshake
        return token

    # ------------------------------------------------------------- identity
    async def stb_profile(self, *, force: bool = False) -> dict:
        """
        `type=stb&action=get_profile`, with the device fingerprint - and the
        answer is *used*: it is where `blocked`, `status` and
        `force_ch_link_check` come from (see app/portal/account.py).

        Best effort on purpose. A panel without this action (404, or a refusal)
        is a normal panel, not a broken one, so the failure is remembered as a
        code and never raised: an account-info gap must not take a working
        stream path down with it.
        """
        if not STB_PROFILE_ENABLED:
            return self._profile or {}
        if self._profile is not None and not force:
            return self._profile
        params = profile_params(self.identity, token_random=self._token_random,
                                not_valid=self._not_valid)
        js = await self._profile_call(params, "full")
        if js is None:
            return self._profile or {}
        if not js.get("id") and self.identity_mode == MAG250:
            # The full shape was accepted but nothing came back: the panel does
            # not know this fingerprint. Retry with the two-field profile a box
            # without stbapp support sends - EStalker's own fallback, and the
            # difference between "no account data" and a working portal.
            js = await self._profile_call(minimal_profile_params(self.identity), "minimal") or js
        self._profile = js
        return js

    async def _profile_call(self, params: dict, shape: str) -> dict | None:
        try:
            data = await self._get(params, retry_on_auth=False)
        except PortalError as exc:
            log.info("get_profile (%s shape) refused: %s", shape, exc.code)
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("get_profile (%s shape) failed: %s", shape, exc)
            return None
        js = data.get("js") if isinstance(data, dict) else None
        if not isinstance(js, dict):
            return {}
        log.debug("get_profile (%s): id=%s blocked=%s status=%s force_ch_link_check=%s",
                  shape, js.get("id"), js.get("blocked"), js.get("status"),
                  js.get("force_ch_link_check"))
        return js

    async def account_info(self) -> dict:
        """`type=account_info&action=get_main_info` payload ({} when unavailable)."""
        try:
            data = await self._get({"type": "account_info", "action": "get_main_info",
                                    "JsHttpRequest": "1-xml"})
        except PortalError as exc:
            self.account_info_error = exc.code
            return {}
        js = data.get("js") if isinstance(data, dict) else None
        if isinstance(js, dict):
            return js
        if isinstance(js, list) and js and isinstance(js[0], dict):
            return js[0]
        return {}

    async def refresh_account(self) -> AccountVerdict:
        """
        Ask the portal what it thinks of this MAC, and say so in one verdict.

        Combines `get_profile` (blocked / status / force_ch_link_check) with
        `account_info` (expiry). A panel that refused *both* is not "active":
        the refusal's own code wins, mapped through `status_for_error` so an
        unreachable portal does not get logged as a banned MAC.
        """
        profile = await self.stb_profile(force=True)
        info = await self.account_info()
        verdict = account_verdict(profile=profile, info=info, token=self._token)
        if not profile and not info:
            hard = self.account_info_error or ""
            if hard and verdict.status == "active":
                verdict = replace(verdict, status=status_for_error(PortalError("", code=hard)),
                                  online=False,
                                  reason=f"account state unavailable (portal said {hard})")
        self.account = verdict
        return verdict

    # --------------------------------------------------------- capabilities
    async def version_info(self, *, force: bool = False) -> PortalVersion:
        """`version.js` - the one portal answer that needs no token at all.

        Read through this client, so it goes out with the same proxy, TLS policy
        and box identity as everything else: a panel that answers `version.js`
        to a MAG and 403s it to `python-httpx` is a panel we want to know that
        about, and a probe through a different path proves nothing.
        """
        if self._version is not None and not force:
            return self._version
        self._version = await read_version_js(await self._http(), self.portal_url)
        return self._version

    async def portal_modules(self, *, force: bool = False) -> list[str] | None:
        """The modules this panel offers *and* has enabled; None = it did not say.

        Best effort, exactly like `get_profile`: a panel without the action (404)
        is a normal panel, and "we could not ask" must never turn into "this
        portal has no series", because that sentence is how a catalogue gets
        hidden from a user for no reason.
        """
        if self._modules is not None and not force:
            return self._modules
        try:
            data = await self._get({"type": "stb", "action": "get_modules",
                                    "JsHttpRequest": "1-xml"})
        except PortalError as exc:
            log.info("get_modules refused: %s", exc.code)
            self.modules_error = exc.code
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("get_modules failed: %s", exc)
            self.modules_error = "transport"
            return None
        modules = enabled_modules(data)
        if modules is None:
            self.modules_error = self.modules_error or "no_modules_answer"
        else:
            self.modules_error = ""
        self._modules = modules
        return modules

    async def refresh_capabilities(self) -> dict:
        """version.js + get_modules, as one answer the GUI can just show."""
        version = await self.version_info(force=True)
        modules = await self.portal_modules(force=True)
        return {"version": version.public(), "modules": modules,
                "modules_error": self.modules_error if modules is None else "",
                "features": {name: supports(modules, name) for name in FEATURE_MODULES}}

    # --------------------------------------------------------- xtream bridge
    async def xtream_harvest(self, kinds: tuple[str, ...] = ("vod", "live")
                             ) -> tuple[XtreamCreds | None, str]:
        """Look for Xtream credentials in one `create_link` answer (R7).

        VOD first, then live, like EStalker does (`playlists.py:1094-1097`): a
        movie cmd is a plain file path and the panels that expose Xtream put the
        account credentials in that path.

        This is the one place the R2 fast path is deliberately *not* used - we are
        asking for the link the panel builds, so the panel has to build it. And it
        is best effort like the other probes: `(None, why)`, never an exception,
        because a portal with no Xtream side is the ordinary case and the caller is
        a portal check that must still say "portal OK".
        """
        for kind in kinds:
            try:
                if kind == "vod":
                    items = (await self.vod_list(None, 0)).items
                else:
                    items = await self.all_channels() or (await self.live_channels(None, 0)).items
            except PortalError as exc:
                log.debug("xtream harvest: %s list refused (%s)", kind, exc.code)
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("xtream harvest: %s list failed (%s)", kind, type(exc).__name__)
                continue
            row = next((i for i in items if isinstance(i, dict) and i.get("cmd")), None)
            if row is None:
                continue
            try:
                link = await self.create_link(str(row["cmd"]), kind)
            except PortalError as exc:
                log.debug("xtream harvest: create_link refused (%s)", exc.code)
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("xtream harvest: create_link failed (%s)", type(exc).__name__)
                continue
            creds = harvest(link)
            if creds:
                log.info("xtream harvest: %s link carries credentials for %s@%s",
                         kind, creds.username, creds.base)
                return creds, f"credentials in the {kind} link"
        return None, "no Xtream credentials in a portal link"

    async def short_epg(self, ch_id: str, size: int = 10, *, tz=None) -> list[Programme]:
        """`type=itv&action=get_short_epg` for one channel: the next few programmes.

        Raises what the portal raised, on purpose. A caller walking 40 channels
        needs to tell "this panel has no short EPG" (stop asking) from "this one
        request hit a 503" (retry twice and move on) - which is the fetch
        discipline (callers must batch/cache; do not walk a whole catalogue).
        """
        data = await self._get({"type": "itv", "action": "get_short_epg",
                                "ch_id": str(ch_id or ""), "size": str(int(size)),
                                "JsHttpRequest": "1-xml"})
        return parse_short_epg(data, tz)

    def _may_reauth(self, retry_on_auth: bool = True, retried: bool = False) -> bool:
        """May this failure be answered by a fresh handshake?

        Three ways to say no, each preventing a loop that is easy to write and
        impossible to see in a log: the request already used its one retry; the
        caller opted out (an identity/account call, where a refusal is *data*
        about what this portal implements, not a token problem); or we are
        INSIDE a handshake - refreshing the token from a call the handshake
        itself makes recurses forever, which is exactly how a portal without
        `get_profile` used to hang this client.
        """
        return bool(retry_on_auth) and not retried and not self._handshaking

    async def account_expires(self) -> str | None:
        """Portals report expiry in account_info.get_main_info 'phone' (STB-Proxy
        convention); `end_date` is the other name in the wild. Kept as the
        lightweight call for callers that only want the date string - everything
        that has to *decide* something should use refresh_account()."""
        self.account_info_error = ""
        js = await self.account_info()
        exp = js.get("phone") or js.get("end_date")
        return str(exp) if exp else None

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
        """
        Seasons of a series. Tries multiple query strategies (type=series, type=vod,
        clean IDs) and strictly validates returned items to reject generic VOD movie fallbacks.
        """
        clean_id = series_id.split(":")[0] if ":" in series_id else series_id
        ids = [series_id] if series_id == clean_id else [series_id, clean_id]

        for type_ in ("series", "vod"):
            for id_ in ids:
                params = {"type": type_, "action": "get_ordered_list", "movie_id": id_,
                          "sortby": "series", "p": "1", "JsHttpRequest": "1-xml"}
                try:
                    data = await self._get(params)
                    js = data.get("js") or {}
                    items = js.get("data") if isinstance(js, dict) else []
                    if isinstance(items, list) and items:
                        valid = [i for i in items if _is_valid_season_item(i, series_id)]
                        if valid:
                            return valid
                except PortalError:
                    pass

                if type_ == "series":
                    params = {"type": type_, "action": "get_ordered_list", "category": id_,
                              "p": "1", "JsHttpRequest": "1-xml"}
                    try:
                        data = await self._get(params)
                        js = data.get("js") or {}
                        items = js.get("data") if isinstance(js, dict) else []
                        if isinstance(items, list) and items:
                            valid = [i for i in items if _is_valid_season_item(i, series_id)]
                            if valid:
                                return valid
                    except PortalError:
                        pass

        return []

    async def series_episodes(self, series_id: str, season_id: str | None) -> list[dict]:
        """
        Episodes of a season. Tries type=series and type=vod, both season_id and series
        parameter naming, and clean IDs.
        """
        clean_id = series_id.split(":")[0] if ":" in series_id else series_id
        ids = [series_id] if series_id == clean_id else [series_id, clean_id]

        for type_ in ("series", "vod"):
            for id_ in ids:
                param_keys = ["season_id", "series", "season"] if season_id is not None else [None]
                for pkey in param_keys:
                    params: dict = {"type": type_, "action": "get_ordered_list",
                                    "movie_id": id_, "p": "1", "JsHttpRequest": "1-xml"}
                    if pkey and season_id is not None:
                        params[pkey] = str(season_id)
                    try:
                        data = await self._get(params)
                        js = data.get("js") or {}
                        items = js.get("data") if isinstance(js, dict) else []
                        if isinstance(items, list) and items:
                            valid = [i for i in items if _is_valid_episode_item(i, series_id)]
                            if valid:
                                return valid
                    except PortalError:
                        continue

                if type_ == "series" and season_id is not None:
                    params = {"type": type_, "action": "get_ordered_list",
                              "category": id_, "season_id": str(season_id),
                              "p": "1", "JsHttpRequest": "1-xml"}
                    try:
                        data = await self._get(params)
                        js = data.get("js") or {}
                        items = js.get("data") if isinstance(js, dict) else []
                        if isinstance(items, list) and items:
                            valid = [i for i in items if _is_valid_episode_item(i, series_id)]
                            if valid:
                                return valid
                    except PortalError:
                        pass

        return []

    # ---------------------------------------------------------------- links
    async def create_link(self, cmd: str, kind: str = "itv", *, link_flags: str | None = None,
                          force_ch_link_check: bool = False,
                          series: int | str | None = None) -> str:
        """
        Resolve a portal `cmd` to a playable stream URL.

        Three steps that matter on real panels (all learned the hard way):

        1. the outgoing cmd is stripped of volatile parameters. Many portals
           store - and we therefore keep - an already tokenised link; handing
           that back makes some panels rebuild the URL and lose the stream id.
        2. the answer is repaired against the request, so a portal that drops
           or blanks parameters still yields a complete URL (fresh token is
           always taken from the answer).
        3. `%mac%` in the answer is filled in with OUR mac: a panel that keeps
           one link template for every box expects the STB to do this, and a
           literal `%mac%` in a URL is a guaranteed 404 from ffmpeg.

        `link_flags` / `force_ch_link_check` are what the channel and the panel
        told us (R2): a box asks for an ad-free link only for a channel whose
        flags say so, and it re-checks the link when the panel set
        `force_ch_link_check`. Hardcoding `false` for both is a small lie that
        some panels happily answer with a link we must then not use.
        """
        type_ = {"live": "itv", "itv": "itv", "vod": "vod", "series": "vod", "episode": "vod"}.get(kind, "itv")
        if series is not None:
            # Classic-Stalker episode: the panel selects the episode server-side
            # by the `series` parameter of a type=vod create_link (the stored cmd
            # addresses the whole season). Same request IPTVnator sends.
            type_ = "vod"
        raw_cmd = str(cmd or "").strip()
        requested = extract_url(raw_cmd)
        out_cmd = sanitize_cmd(raw_cmd)
        if out_cmd != raw_cmd:
            log.debug("create_link: stripped stale token from cmd")
        data = await self._get({
            "type": type_, "action": "create_link", "cmd": out_cmd, "JsHttpRequest": "1-xml",
            **link_request_params(link_flags=link_flags,
                                  force_ch_link_check=force_ch_link_check,
                                  series=series),
        })
        js = data.get("js")
        raw = ""
        if isinstance(js, dict):
            raw = js.get("cmd") or js.get("url") or js.get("link") or ""
        elif isinstance(js, str):
            raw = js
        link = extract_url(raw)
        if not link:
            # No link, but no refusal either - the panel answered something we
            # cannot use (an empty cmd, a relative path, a plugin command we do
            # not speak). Say so, and name the format we got.
            raise PortalError(
                f"create_link returned no usable url for cmd={cmd!r}"
                + (f" (portal cmd: {str(raw)[:120]!r})" if raw else ""),
                code="no_url")
        repaired = merge_link(link, requested)
        if repaired != link:
            log.info("create_link: portal dropped parameters -> repaired from cmd")
        with_mac = apply_mac_placeholder(repaired, self.mac)
        if with_mac != repaired:
            log.info("create_link: portal left a mac placeholder in the link -> filled in from our MAC")
        if is_hls(with_mac):
            log.debug("create_link: HLS playlist link (ffmpeg needs the segment "
                      "protocols whitelisted; the stream path adds that)")
        log.debug("create_link -> %s", with_mac)
        log.info("create_link -> %s", mask_token(with_mac))
        return with_mac
