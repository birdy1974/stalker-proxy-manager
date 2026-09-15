"""Which identity ffmpeg (and the redirect liveness probe) presents to a
*stream origin*.

The portal API and the media endpoint expect DIFFERENT clients on a real MAG
box (see ``app/portal/identity.py``, ``STB_UA`` vs ``PLAYER_UA``):

* the stbapp browser talks to ``portal.php`` with the
  ``Mozilla/5.0 (QtEmbedded…) AppleWebKit/533.3`` UA (``STB_UA``);
* the embedded media player - an old libav/ffmpeg build - fetches the resolved
  ``play/live.php`` / CDN URL announcing ``Lavf53.32.100`` (``PLAYER_UA``).

We used to present the browser UA for both. Lenient panels accept it, but
``play/live.php`` origins and the anti-proxy WAFs in front of them increasingly
answer the browser UA on the MEDIA endpoint with HTTP 456 ("unrecoverable",
also seen as 403), while the same freshly-resolved token plays fine for a
player-shaped request - the failure that reads as "redirect/direct works but
every ffmpeg template fails with rc=8 and zero bytes". Other panels do the
reverse and refuse a bare libav UA (the case the browser-UA injection was
originally written for), so neither value alone works everywhere.

The media path therefore walks a short ladder, faithful box identity first:

  1. an origin we have already LEARNED accepts gets that identity first (the
     steady state costs one request), with the other identity still queued
     behind it in case the origin's policy changed;
  2. otherwise the MAG player UA (``Lavf53.32.100``);
  3. on a pre-first-byte HTTP 4xx input-open error, the portal browser UA
     (``STB_UA``).

The winner is remembered per origin host, in-process only: the policy is
stable per origin but is nothing the database should carry, and a restart
re-learns it on the next play. A UA that the template/operator pinned with an
explicit ``-user_agent`` is never overridden by the ladder (the caller skips
it), and the whole ladder can be reverted to legacy browser-only behaviour
with ``SPM_STREAM_UA_LADDER=0``.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from ..portal.identity import PLAYER_UA, STB_UA

__all__ = ["PLAYER_UA", "STB_UA", "origin_of", "learned", "remember",
           "ladder", "reset", "http_open_error", "LADDER_ENABLED",
           "UA_POLICY_4XX"]

#: HTTP client errors that can mean "I do not like the way this client looks"
#: at an origin. ffmpeg's own HTTP layer only auto-retries 400/401/403/404;
#: 456 (the Stalker/WAF "unrecoverable" answer behind this whole module) and
#: 429 (occasional WAF rate answer) are not in that set, so retrying them
#: ourselves with the other identity is the only way to learn the answer.
UA_POLICY_4XX = frozenset({401, 403, 404, 429, 456})

LADDER_ENABLED = os.environ.get("SPM_STREAM_UA_LADDER", "1") == "1"

#: origin host[:port] -> the UA that produced bytes there
_origin_ua: dict[str, str] = {}
_MAX_ORIGINS = 512


def origin_of(url: str) -> str:
    """The identity bucket for one media URL: host[:port], lower-cased.

    The play_token in the query rotates on every create_link; the UA policy
    belongs to the origin, never to the token. Paths differ between live and
    VOD on the same host but are served by the same front end, so they share
    the learned value.
    """
    parts = urlsplit(str(url or ""))
    return (parts.netloc or "").lower()


def learned(url: str) -> str | None:
    """The UA this origin already taught us it accepts (None = not yet)."""
    origin = origin_of(url)
    return _origin_ua.get(origin) if origin else None


def remember(url: str, ua: str | None) -> None:
    """Record which UA produced bytes at this origin."""
    if not ua:
        return
    origin = origin_of(url)
    if not origin:
        return
    _origin_ua[origin] = ua
    # Tiny, bounded and replaced rather than grown forever: a deployment that
    # sees many origins keeps the most recent, like the redirect-guard memory.
    if len(_origin_ua) > _MAX_ORIGINS:
        for old in list(_origin_ua)[:len(_origin_ua) - _MAX_ORIGINS]:
            _origin_ua.pop(old, None)


def ladder(url: str) -> list[str]:
    """The UAs to offer for one media URL, in order.

    * ``SPM_STREAM_UA_LADDER=0`` restores legacy behaviour (browser UA only);
    * an origin that has already taught us a winner is asked with that
      identity FIRST - so the steady state costs one request - with the
      other identity still queued behind it, so an origin whose WAF policy
      changed re-learns within the same play instead of going black;
    * otherwise player first, browser second.
    """
    if not LADDER_ENABLED:
        return [STB_UA]
    full = [PLAYER_UA, STB_UA]
    known = learned(url)
    if known and known in full:
        return [known] + [ua for ua in full if ua != known]
    return full


def reset() -> None:
    """Forget every learned origin (tests and explicit operator resets)."""
    _origin_ua.clear()


# ffmpeg fails an HTTP input open with rc=8 (and occasionally rc=1), printing
# either the direct status ("[http @ …] HTTP error 456") or the wrapper line
# ("Error opening input: Server returned 4XX Client Error, but not one of
# 40{0,1,3,4}") - the latter being ffmpeg 7.x's way of reporting precisely the
# non-standard 456 this module exists for.
_RE_HTTP_STATUS = re.compile(r"HTTP error\s*(\d{3})")
_RE_4XX_WRAPPER = re.compile(r"returned\s+4XX\s+Client\s+Error", re.I)


def http_open_error(rc: int | None, stderr_tail: str) -> int | None:
    """The HTTP 4xx status ffmpeg died of while opening the INPUT, or None.

    None covers everything that is not an identity-policy answer: transport
    timeouts (rc/stalls, caught by the caller as "no data"), muxer/filter
    errors at output init, and any 2xx-then-EOF death. Only a clear 4xx from
    the HTTP layer - the documented 456 and the statuses in UA_POLICY_4XX -
    justifies spending the one ladder retry.
    """
    text = str(stderr_tail or "")
    m = _RE_HTTP_STATUS.search(text)
    status = int(m.group(1)) if m else None
    if status is None:
        # ffmpeg 7 wording for an unrecognised 4xx (456/429/418/...): the
        # numeric line may be absent in some builds, so recognise the wrapper.
        if rc in (1, 8) and _RE_4XX_WRAPPER.search(text):
            return 456
        return None
    if 400 <= status < 500 and status in UA_POLICY_4XX:
        return status
    return None
