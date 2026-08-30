"""
Portal URL resolution.

A Stalker/Ministra "portal URL" as typed by a user can mean many things:
    http://host/c/                 http://host/portal.php
    http://host/stalker_portal/c/  http://host/server/load.php
    http://host                    http://host:8080/client/

Resolution strategy (all attempts are logged for fault-finding):
  1. For every known base path, try to fetch `xpcom.common.js` and evaluate the
     javascript indirection (STB-Proxy approach: the file computes the real
     `ajax_loader` URL from protocol/ip/path regex groups).
  2. If that fails, directly probe `<base><path>portal.php?type=stb&action=handshake`
     (plus `/server/load.php` variants) with a MAG user-agent - a portal that
     answers the handshake with JSON is usable even without xpcom.common.js.
  3. The first working URL wins; the result carries the full attempt log so the
     GUI can show exactly what was tried.

Filters are intentionally LOOSE: any HTTP 200 whose body parses as JSON with a
`js` key, or any parseable xpcom file, counts as success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..config import PORTAL_HTTP_TIMEOUT
from ..services.http_client import outbound_client
from .identity import STB_MODEL, STB_UA, normalize_mac

# MAG-styled UA; many portals reject anything else (observed in the wild and
# during Phase-1 probing: plain curl gets an empty reply from many servers)
MAG_UA = STB_UA

# Ordered base-path candidates (D-B: resolution result is cached in Portal row).
PATH_CANDIDATES = [
    "/c/",
    "/portal.php",          # file-style base (user pasted the full file url)
    "/client/",
    "/c_/",
    "/stalker_portal/c/",
    "/stalker_portal/c_/",
    "/server/load.php",     # some panels hide the api here
    "/stalker_portal/server/load.php",
    "/",
]


@dataclass
class ResolveResult:
    ok: bool = False
    portal_url: str = ""       # full URL of the portal.php endpoint
    path: str = ""             # winning base path
    attempts: list[str] = field(default_factory=list)
    error: str = ""

    def log(self, msg: str) -> None:
        self.attempts.append(msg)


def _normalize_base(url: str) -> tuple[str, str]:
    """Return (scheme://host[:port], path-hint-from-user-input)."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    hint = p.path or ""
    return base, hint


def _portal_php_for(base: str, path: str) -> str:
    """Compute the portal.php URL for a candidate path."""
    path = path.strip()
    if path.endswith(("portal.php", "load.php")):
        return base + (path if path.startswith("/") else "/" + path)
    if path in ("", "/"):
        return base + "/portal.php"
    return base + (path if path.startswith("/") else "/" + path).rstrip("/") + "/portal.php"


def _xpcom_for(base: str, path: str) -> str | None:
    if path.endswith(".php") or path in ("", "/"):
        return None
    return base + path.rstrip("/") + "/xpcom.common.js"


def _parse_xpcom(js_url: str, text: str) -> str | None:
    """
    Evaluate the portal indirection in xpcom.common.js.
    Direct port of STB-Proxy's stb.getUrl() parsing with extra tolerance.
    """
    try:
        # Only strip whitespace for structural parsing - removing '+' would
        # corrupt regex quantifiers like [^/]+ inside the portal pattern.
        nospace = re.sub(r"\s+", "", text)
        # Grab the first "(http..." group GREEDY up to the last ")/;" so the
        # captured body is a balanced, valid regex (STB-Proxy semantics).
        m = re.search(r"varpattern.*\/(\(http.*)\/;", nospace)
        if not m:
            return None
        pattern = m.group(1)
        # Match that regex against the js-file URL itself to recover the
        # protocol / host / path fragments by index (see _idx below).
        res = re.search(pattern, js_url)
        if not res:
            return None

        def _idx(name: str, default: int) -> int:
            mm = re.search(rf"this\.{name}.*?(\d).*?;", nospace)
            return int(mm.group(1)) if mm else default

        proto_i, ip_i, path_i = _idx("portal_protocol", 1), _idx("portal_ip", 2), _idx("portal_path", 3)
        groups = res.groups()
        if max(proto_i, ip_i, path_i) > len(groups):
            return None
        # tolerate the closing quote between ".php" and ";"
        loader = re.search(r"this\.ajax_loader=(.+?\.php)['\"]*;", nospace)
        if not loader:
            return None
        tmpl = loader.group(1).replace("'", "").replace("+", "")   # concat cleanup ok HERE
        portal = (
            tmpl
            .replace("this.portal_protocol", groups[proto_i - 1])
            .replace("this.portal_ip", groups[ip_i - 1])
            .replace("this.portal_path", groups[path_i - 1])
        )
        if portal.startswith("http"):
            return portal
    except Exception:
        return None
    return None


async def resolve_portal(
    raw_url: str,
    mac: str | None = None,
    proxy: str | None = None,
    timeout: float = PORTAL_HTTP_TIMEOUT,
    tls_insecure: bool = False,
) -> ResolveResult:
    """
    Probe `raw_url` and return the working portal.php URL (if any).

    `tls_insecure` is the caller's per-portal opt-out, forwarded verbatim: a
    panel with a broken certificate chain has to be resolvable, or the user
    never gets as far as the GUI flag that would have fixed it. Verification
    stays on by default (see app/services/http_client.py).
    """
    result = ResolveResult()
    base, hint = _normalize_base(raw_url)
    # Discovery carries the *static* half of the box identity (see
    # app/portal/identity.headers_for). No Referer: during discovery we do not
    # know whether index.html exists at this path, and a referer pointing at a
    # 404 page is more suspicious to a WAF than no referer at all. The
    # authenticated calls in StalkerClient do send it.
    headers = {
        "User-Agent": MAG_UA,
        "X-User-Agent": f"Model: {STB_MODEL}; Link: WiFi",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Pragma": "no-cache",
    }
    cookies = {"mac": normalize_mac(mac) if mac else "00:1A:79:00:00:00",
               "stb_lang": "en", "timezone": "UTC"}

    # Order: user-given path first (if it looks like a real path), then candidates
    paths: list[str] = []
    if hint and hint not in ("/", ""):
        paths.append(hint if hint.endswith("/") or hint.endswith(".php") else hint + "/")
    for p in PATH_CANDIDATES:
        if p not in paths:
            paths.append(p)

    client_kwargs: dict = {"headers": headers, "cookies": cookies, "timeout": timeout,
                           "follow_redirects": True}
    if proxy:
        client_kwargs["proxy"] = proxy

    async with outbound_client(insecure=tls_insecure, **client_kwargs) as http:
        for path in paths:
            # ---- strategy 1: xpcom.common.js indirection --------------------
            xp = _xpcom_for(base, path)
            if xp:
                try:
                    r = await http.get(xp)
                    if r.status_code == 200 and len(r.text) > 200 and "ajax_loader" in r.text:
                        portal = _parse_xpcom(xp, r.text)
                        result.log(f"xpcom probe {xp} -> HTTP 200 ({len(r.text)}b)")
                        if portal:
                            result.ok, result.portal_url, result.path = True, portal, path
                            result.log(f"xpcom indirection resolved -> {portal}")
                            return result
                        result.log("xpcom found but indirection not parseable (obfuscated?) - trying direct probe")
                    else:
                        result.log(f"xpcom probe {xp} -> HTTP {r.status_code} ({len(r.content)}b)")
                except Exception as exc:  # noqa: BLE001 - we log and continue
                    result.log(f"xpcom probe {xp} -> {type(exc).__name__}: {exc}")

            # ---- strategy 2: direct handshake probe -------------------------
            portal_url = _portal_php_for(base, path)
            try:
                r = await http.get(
                    portal_url,
                    params={"type": "stb", "action": "handshake", "JsHttpRequest": "1-xml"},
                )
                ok_json = False
                try:
                    data = r.json()
                    ok_json = isinstance(data, dict) and "js" in data
                except Exception:
                    ok_json = False
                result.log(
                    f"handshake probe {portal_url} -> HTTP {r.status_code} ({len(r.content)}b)"
                    + (" JSON ok" if ok_json else "")
                )
                if r.status_code == 200 and ok_json:
                    result.ok, result.portal_url, result.path = True, portal_url, path
                    return result
            except Exception as exc:  # noqa: BLE001
                result.log(f"handshake probe {portal_url} -> {type(exc).__name__}: {exc}")

    result.error = "no working portal endpoint found"
    return result
