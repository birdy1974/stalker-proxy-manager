"""
What a real set-top box sends, and what we send in its place.

Why this exists
---------------
A Stalker panel decides whether to answer *at all* by how much the client looks
like the box it enrolled. Two clients asking for the same `portal.php` URL can
get completely different results: one is served the catalogue, the other gets an
empty body, a 403, or - the nasty one - a 200 with no data. The bare request
shape we used to send (`User-Agent` + a `mac` cookie, `prehash=0`, no fingerprint)
works on lenient panels and on our mock, and silently underperforms on the picky
ones - which is precisely the class of failure that reads as "this portal is
dead" in the logs.

Behaviour observed in kiddac/EStalker @032967f (a client that is accepted
everywhere because it *is* the box); re-implemented here, with our own literal
values for the device advertisement strings.

Rules this module follows
-------------------------
* **Derived, never random.** `sn`, `device_id`, `adid` and `prehash` come from
  the MAC, so the same box presents the same identity after a restart, after a
  re-deploy, and in every process. A portal that remembers the device it
  enrolled must never see a fresh serial from the same MAC - that is how a
  working account gets flagged. Randomness is only used for the throwaway bearer
  in the `missing` -> `prehash` dance, which is single-use by definition.
* **Overridable.** A user who captured the real values from their box can pin
  them (`MacAddress.sn` / `.device_id`), and `identity_mode="minimal"` sends the
  two-field profile instead. When the fingerprint is *wrong* for a panel it is
  worse than no fingerprint, so there has to be a way out without a code change.
* **Pure.** No I/O, no DB, no httpx - everything here is a function of its
  arguments, so the whole thing is testable without a portal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import string
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

# --------------------------------------------------------------------------- #
# the shape of the box we claim to be
# --------------------------------------------------------------------------- #
MAG250 = "mag250"
MINIMAL = "minimal"
IDENTITY_MODES = (MAG250, MINIMAL)

#: advertised model; also feeds `X-User-Agent`, which some panels match on
STB_MODEL = os.environ.get("SPM_STB_MODEL", "MAG250")
STB_TYPE_ID = os.environ.get("SPM_STB_STBAPP", "STB")
STB_IMAGE_VERSION = os.environ.get("SPM_STB_IMAGE_VERSION", "220")
STB_HW_VERSION = os.environ.get("SPM_STB_HW_VERSION", "1.7-BD-00")
STB_NUM_BANKS = "2"
STB_VIDEO_OUT = "hdmi"
STB_API_SIGNATURE = "262"

#: The `ver` parameter is a device advertisement, not a secret. Written here
#: (not copied) and env-overridable, because a panel that pins an exact image
#: build is a panel you can only satisfy by telling it what it wants to see.
STB_VER = os.environ.get("SPM_STB_VER") or (
    "ImageDescription: 0.2.20-r3-{model_suffix}; "
    "ImageDate: Wed Jun 12 09:04:11 EET 2019; "
    "PORTAL version: {portal_version}; "
    "API Version: JS API version: 350; STB API version: 149; "
    "Player Engine version: 0.6d7"
)
PORTAL_VERSION_REPORTED = os.environ.get("SPM_STB_PORTAL_VERSION", "5.4.0")

#: cookie values a MAG sends. `Europe/Amsterdam` is this deployment's locale, not
#: a magic number - per-portal override lives in Portal.stb_timezone.
STB_TIMEZONE = os.environ.get("SPM_STB_TIMEZONE", "Europe/Amsterdam")
STB_LANG = os.environ.get("SPM_STB_LANG", "en")

#: The user agent a MAG announces. Single-sourced on purpose: the portal calls,
#: the stream probes and the ffmpeg `-user_agent` all take this value, because a
#: probe that announces something else than the real path reports a result nobody
#: will ever see. Overridable - some panels pin an older `stbapp ver:` string,
#: and "the panel wants a different lie" is a supported configuration, not a bug.
STB_UA = os.environ.get("SPM_STB_UA") or (
    "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
    "(KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 2721 Safari/533.3")

_MODELS = {"MAG200": "200", "MAG245": "245", "MAG250": "250", "MAG255": "255",
           "MAG322": "322", "MAG349": "349"}


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class StbIdentity:
    """The per-MAC device identity, plus everything derived from it."""

    mac: str
    model: str = STB_MODEL
    sn: str = ""
    device_id: str = ""
    hw_version: str = STB_HW_VERSION
    hw_version_2: str = ""
    signature: str = ""
    prehash: str = ""
    adid: str = ""
    image_version: str = STB_IMAGE_VERSION
    api_signature: str = STB_API_SIGNATURE
    minimal: bool = False

    @property
    def ver(self) -> str:
        return STB_VER.format(model_suffix=_MODELS.get(self.model, "250"),
                              portal_version=PORTAL_VERSION_REPORTED)

    def metrics(self, token_random: str = "") -> str:
        """The `metrics` parameter: a compact JSON blob, URL-encoded once.

        Portals log this and some of them correlate it with `sn`, so both have to
        come from the same derivation.
        """
        blob = json.dumps({"mac": self.mac, "sn": self.sn, "model": self.model,
                           "type": STB_TYPE_ID, "uid": "", "random": token_random or ""},
                          separators=(",", ":"))
        return quote(blob, safe="")

def normalize_mac(mac: str) -> str:
    """`00:1a:79:aa:aa:01` / `001A79AAAA01` -> `00:1A:79:AA:AA:01`.

    The MAC is the seed for every derived value, so the spelling has to be
    canonical or a user typing lower-case into the GUI gets a different device
    serial for the same box.
    """
    digits = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    if len(digits) != 12:
        return str(mac or "").strip().upper()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def derive_identity(mac: str, *, sn: str | None = None, device_id: str | None = None,
                    model: str | None = None, minimal: bool = False) -> StbIdentity:
    """
    Deterministic device identity for one MAC.

    `sn`/`device_id` may be pinned (from the MAC row, or a capture of the real
    box); everything else is always derived from what we ended up using, so the
    set `sn` + `prehash` + `metrics` stays internally consistent - a panel that
    recomputes `sha1(sn+mac)` and gets something else is worse off than one that
    saw no fingerprint at all.
    """
    mac = normalize_mac(mac)
    seed = (sn or "").strip() or _md5(mac).upper()[:13]
    dev = (device_id or "").strip() or _sha256(mac).upper()
    return StbIdentity(
        mac=mac,
        model=(model or STB_MODEL),
        sn=seed,
        device_id=dev,
        hw_version_2=_sha1(mac),
        signature=_sha256(dev + dev).upper(),
        prehash=_sha1(seed + mac),
        adid=_md5(seed + mac),
        minimal=bool(minimal),
    )


# --------------------------------------------------------------------------- #
# the handshake dance
# --------------------------------------------------------------------------- #
def missing_token(js: object) -> bool:
    """True when a handshake reply says "your bearer is missing".

    Ministra's second-step auth answers the plain handshake with
    `{"js":{"msg":"missing"}}` and NO token: the box is expected to invent a
    bearer, hash it, and handshake again with that hash as `prehash`. Panels
    that do this reject a client that only knows stage 1 - and they never say
    why, which is what makes it invisible from a log.
    """
    if not isinstance(js, dict):
        return False
    return "missing" in str(js.get("msg") or "").lower()


def make_fake_bearer(length: int = 32) -> tuple[str, str]:
    """`(bearer, prehash)` for the second-step handshake.

    The box invents a throwaway bearer, sends it as `Authorization`, and sends
    its SHA-1 as `prehash`; the panel then issues the real token. Single-use by
    definition, so it is generated here rather than stored - and `secrets`
    rather than `random` because a guessable nonce is the same as none.
    """
    alphabet = string.ascii_uppercase + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(max(8, int(length))))
    return token, _sha1(token)


# --------------------------------------------------------------------------- #
# get_profile parameters
# --------------------------------------------------------------------------- #
def profile_params(idn: StbIdentity, *, token_random: str = "", not_valid: bool = False,
                   timestamp: int | None = None) -> dict[str, str]:
    """The MAG's `type=stb&action=get_profile` query (full shape).

    `auth_second_step=1` + `prehash` + `not_valid_token` are the trio that makes
    a panel accept a bearer it has never seen; `not_valid` comes from the
    handshake and must be echoed back or the second step is rejected.
    """
    ts = str(int(time.time() if timestamp is None else timestamp))
    if idn.minimal:
        return minimal_profile_params(idn, timestamp=timestamp)
    return {
        "type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml",
        "hd": "1", "ver": idn.ver, "num_banks": STB_NUM_BANKS, "sn": idn.sn,
        "stb_type": idn.model, "client_type": STB_TYPE_ID,
        "image_version": idn.image_version, "video_out": STB_VIDEO_OUT,
        "device_id": idn.device_id, "device_id2": idn.device_id,
        "signature": idn.signature, "auth_second_step": "1",
        "hw_version": idn.hw_version, "not_valid_token": "1" if not_valid else "0",
        "metrics": idn.metrics(token_random), "hw_version_2": idn.hw_version_2,
        "timestamp": ts, "api_signature": idn.api_signature, "prehash": idn.prehash,
    }


def minimal_profile_params(idn: StbIdentity, *, timestamp: int | None = None) -> dict[str, str]:
    """What a box with no fingerprint support sends - and our own fallback when
    a panel rejects the full shape (it answers without `js.id`)."""
    return {
        "type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml",
        "sn": idn.sn, "device_id": "",
        "timestamp": str(int(time.time() if timestamp is None else timestamp)),
    }


# --------------------------------------------------------------------------- #
# headers / cookies for every portal call
# --------------------------------------------------------------------------- #
def referer_for(portal_url: str) -> str:
    """`http://host/stalker_portal/c/portal.php` -> `.../c/index.html`.

    A real STB is a browser pointed at index.html, so the XHRs carry that page
    as Referer. Some panels reject a portal call without it - and, unlike the
    bearer, this one is cheap to get right.
    """
    parts = urlsplit(portal_url)
    if not parts.scheme or not parts.netloc:
        return ""
    path = parts.path.rsplit("/", 1)[0]
    return f"{parts.scheme}://{parts.netloc}{path}/index.html"


def wants_adid_cookie(portal_url: str) -> bool:
    """The `adid` cookie belongs to the full portal layout, not to `/c/`.

    Sending it on a bare `/c/portal.php` is what gets a lenient panel's WAF
    interested in us, so it goes only where real MAG firmware sets it.
    """
    return "/stalker_portal/" in (urlsplit(portal_url).path or "")


def cookies_for(portal_url: str, mac: str, *, lang: str = STB_LANG,
                timezone: str = STB_TIMEZONE, token: str | None = None,
                adid: str | None = None) -> dict[str, str]:
    """The cookie jar a MAG carries.

    The MAC goes out in its canonical colon form, NOT percent-encoded. Box
    firmware sends `00%3A1A%3A...` (its `set_cookie` runs the value through
    escape), and the panels in the field we can actually observe decode that -
    but a panel doing a naive compare against the stored form decodes nothing,
    and would then see an unknown device. The raw colon form is the one already
    proven against this deployment's portals, so identity work does not silently
    change it; percent-encoding stays a query-parameter concern.
    """
    jar = {"mac": normalize_mac(mac), "stb_lang": lang or STB_LANG,
           "timezone": timezone or STB_TIMEZONE}
    if wants_adid_cookie(portal_url) and adid:
        jar["adid"] = adid
    if token:
        jar["token"] = token
    return jar


def headers_for(portal_url: str, idn: StbIdentity, *, user_agent: str,
                lang: str = STB_LANG, timezone: str = STB_TIMEZONE,
                token: str | None = None) -> dict[str, str]:
    """Headers for every portal request (the cookies go through `cookies_for`).

    Deliberately *not* copied from the box-emulators: no `Connection: Close`
    (we hold pooled keep-alive sessions; asking for a close per request is a
    free way to double our handshake load) and no `Host` override (our httpx
    client may be talking to a proxy, which sets its own).
    """
    h = {
        "User-Agent": user_agent,
        "X-User-Agent": f"Model: {idn.model}; Link: WiFi",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Pragma": "no-cache",
    }
    ref = referer_for(portal_url)
    if ref:
        h["Referer"] = ref
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h
