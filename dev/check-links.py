#!/usr/bin/env python3
# ============================================================================
# Self-check for the portal plumbing that decides whether a channel plays:
# the link helpers in app/portal/client.py, the STB identity in
# app/portal/identity.py, and the account verdict in app/portal/account.py.
#
#   python3 dev/check-links.py
#
# No pytest needed - plain asserts, so it also runs on a NAS where the image
# was built. These helpers decide whether a channel plays at all: a portal that
# mangles its create_link answer (see below) used to produce an unplayable URL
# and an empty 200 response in the player, so the rules are pinned here.
#
# The bug this guards (birdy1974, nexusconnects portal):
#   stored cmd   ffmpeg http://host/play/live.php?mac=..&stream=392166&extension=ts&play_token=OLD
#   portal reply ffmpeg http://host/play/live.php?mac=..&stream=&extension=ts&play_token=NEW
#                                                            ^ the stream id is gone
#   -> ffmpeg: "HTTP error 405 Method Not Allowed", player: black screen.
# ============================================================================
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile

# importing app.portal.client pulls in app.config, which logs its banner and
# creates the data dir: keep both out of the way of this check
logging.disable(logging.CRITICAL)
os.environ.setdefault("SPM_DATA_DIR", tempfile.mkdtemp(prefix="spm-check-links-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.portal.account import (account_verdict, mac_is_usable,  # noqa: E402
                                mac_status, parse_expiry)
from app.portal.client import (apply_mac_placeholder, extract_url, is_hls,  # noqa: E402
                               js_error, mask_token, merge_link, normalize_error,
                               sanitize_cmd, strip_volatile)
from app.portal.identity import (cookies_for, derive_identity, normalize_mac,  # noqa: E402
                                 profile_params, referer_for)

CASES: list[tuple[str, object, object]] = []


def check(label: str, got, want) -> None:
    CASES.append((label, got, want))


# --------------------------------------------------------------------------- #
# extract_url
# --------------------------------------------------------------------------- #
check("cmd with ffmpeg prefix",
      extract_url("ffmpeg http://host/a.ts"), "http://host/a.ts")
check("cmd with ffrt prefix",
      extract_url("ffrt http://host/a.ts"), "http://host/a.ts")
check("bare url", extract_url("http://host/a.ts"), "http://host/a.ts")
check("percent-encoded cmd",
      extract_url("ffmpeg http%3A%2F%2Fhost%2Fa.ts"), "http://host/a.ts")
check("url plus trailing ffmpeg args",
      extract_url("ffmpeg http://host/a.ts -loglevel error"), "http://host/a.ts")
check("quoted url", extract_url('"http://host/a.ts"'), "http://host/a.ts")
check("empty cmd", extract_url(""), "")
check("no url at all", extract_url("ffmpeg"), "")

# --------------------------------------------------------------------------- #
# strip_volatile / sanitize_cmd
# --------------------------------------------------------------------------- #
LIVE = ("http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166"
        "&extension=ts&play_token=OLD")
check("strip: token removed, id kept",
      strip_volatile(LIVE),
      "http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166&extension=ts")
check("strip: without token untouched", strip_volatile("http://host/a.ts"),
      "http://host/a.ts")
check("sanitize: cmd keeps everything but the token",
      sanitize_cmd("ffmpeg " + LIVE),
      "ffmpeg http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166&extension=ts")
check("sanitize: token-free cmd untouched", sanitize_cmd("ffmpeg http://host/a.ts"),
      "ffmpeg http://host/a.ts")

# --------------------------------------------------------------------------- #
# merge_link - the repair itself
# --------------------------------------------------------------------------- #
REQ = ("http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166&extension=ts")
check("repair: blanked stream id restored, fresh token kept",
      merge_link("http://host/play/live.php?mac=00%3A1A%3A79%3A01%3A6D%3ABF"
                 "&stream=&extension=ts&play_token=NEW", REQ),
      "http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166"
      "&extension=ts&play_token=NEW")
check("repair: dropped parameter restored",
      merge_link("http://host/play/live.php?mac=00:1A:79:01:6D:BF&play_token=NEW", REQ),
      "http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166&extension=ts"
      "&play_token=NEW")
check("repair: untouched when the answer is complete",
      merge_link("http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166"
                 "&extension=ts&play_token=NEW", REQ),
      "http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166&extension=ts"
      "&play_token=NEW")
check("repair: a different streamer host is never rewritten",
      merge_link("http://lb2.example.net/play/live.php?stream=&play_token=NEW", REQ),
      "http://lb2.example.net/play/live.php?stream=&play_token=NEW")
check("repair: a different path is never rewritten",
      merge_link("http://host/ch/392166.m3u8?play_token=NEW", REQ),
      "http://host/ch/392166.m3u8?play_token=NEW")
check("repair: a portal-supplied value wins when it is not empty",
      merge_link("http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=999"
                 "&extension=ts&play_token=NEW", REQ),
      "http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=999&extension=ts"
      "&play_token=NEW")
check("repair: no request -> answer unchanged",
      merge_link("http://host/a.ts?play_token=NEW", ""), "http://host/a.ts?play_token=NEW")

# --------------------------------------------------------------------------- #
# the full round trip the stream manager performs
# --------------------------------------------------------------------------- #
stored = "ffmpeg " + LIVE
answer = "ffmpeg http://host/play/live.php?mac=00%3A1A%3A79%3A01%3A6D%3ABF&stream=&extension=ts&play_token=NEW"
check("round trip: stored cmd -> playable url",
      merge_link(extract_url(answer), extract_url(sanitize_cmd(stored))),
      "http://host/play/live.php?mac=00:1A:79:01:6D:BF&stream=392166&extension=ts"
      "&play_token=NEW")

# --------------------------------------------------------------------------- #
# mask_token (logging must not leak a session token)
# --------------------------------------------------------------------------- #
check("mask: token hidden, rest readable",
      mask_token("http://host/a.ts?stream=392166&play_token=SECRET"),
      "http://host/a.ts?stream=392166&play_token=%2A%2A%2A")
check("mask: no query -> unchanged", mask_token("http://host/a.ts"), "http://host/a.ts")

# --------------------------------------------------------------------------- #
# apply_mac_placeholder - panels with ONE link template for every box
# --------------------------------------------------------------------------- #
# `cmd` from the portal: 'http://cdn/ch/%mac%/1234.ts' - the STB is the only
# party that knows its MAC, so a literal %mac% that reaches ffmpeg is a 404
# that looks like a dead channel.
M = "00:1A:79:01:6D:BF"
check("mac placeholder: path form",
      apply_mac_placeholder("http://cdn/ch/%mac%/1234.ts", M),
      f"http://cdn/ch/{M}/1234.ts")
check("mac placeholder: any casing",
      apply_mac_placeholder("http://cdn/ch/%MAC%/1.ts", M), f"http://cdn/ch/{M}/1.ts")
check("mac placeholder: after a query round trip (%25..%25)",
      apply_mac_placeholder("http://cdn/1.ts?m=%25mac%25", M), f"http://cdn/1.ts?m={M}")
check("mac placeholder: nothing to do",
      apply_mac_placeholder("http://cdn/1.ts?x=1", M), "http://cdn/1.ts?x=1")
check("mac placeholder: no mac -> unchanged",
      apply_mac_placeholder("http://cdn/%mac%.ts", ""), "http://cdn/%mac%.ts")
# the placeholder has to survive the repair, so the round trip is checked too
check("round trip: repair keeps the mac fill-in",
      apply_mac_placeholder(
          merge_link("http://host/play/live.php?stream=&play_token=NEW",
                     "http://host/play/live.php?stream=392166&m=%mac%"), M),
      "http://host/play/live.php?stream=392166&m=%s&play_token=NEW" % M)

# --------------------------------------------------------------------------- #
# is_hls - a playlist input needs extra ffmpeg input options
# --------------------------------------------------------------------------- #
check("hls: plain playlist", is_hls("http://h/a.m3u8"), True)
check("hls: with query", is_hls("http://h/a.m3u8?token=x"), True)
check("hls: m3u8 in the query only is not a playlist", is_hls("http://h/a.ts?f=.m3u8"), False)
check("hls: mpegts link", is_hls("http://h/a.ts"), False)
check("hls: empty", is_hls(""), False)

# --------------------------------------------------------------------------- #
# portal refusals carry their code
# --------------------------------------------------------------------------- #
check("error: portal wording normalized", normalize_error("Account is in use"),
      "account_is_in_use")
check("error: 'error': 0 next to data is not a refusal",
      js_error({"js": {"error": 0, "data": [{"id": "1"}]}}), "")
check("error: bare refusal is reported", js_error({"js": {"error": "limit"}}), "limit")
check("error: ok-with-empty-payload via msg", js_error({"js": {"msg": "OK"}}), "")
check("error: usable payload keeps an informational msg",
      js_error({"js": {"msg": "wait", "total_items": 0, "data": []}}), "wait")

# --------------------------------------------------------------------------- #
# the STB identity: derived, stable, and visible to the portal
# --------------------------------------------------------------------------- #
import hashlib  # noqa: E402
MAC0 = "00:1A:79:AA:AA:01"
check("identity: any spelling of the MAC is the same box",
      (lambda a, b: (a.sn, a.prehash) == (b.sn, b.prehash))(
          derive_identity("00:1a:79:aa:aa:01"), derive_identity("001A79AAAA01")), True)
_i = derive_identity(MAC0)
check("identity: prehash is sha1(sn+mac), so a pin stays consistent",
      _i.prehash, hashlib.sha1((_i.sn + MAC0).encode()).hexdigest())
check("identity: a pinned serial is used, not the derived one",
      derive_identity(MAC0, sn="REAL-SN").sn, "REAL-SN")
_q = profile_params(_i, not_valid=True, timestamp=1700000000)
check("identity: get_profile carries the fingerprint fields",
      all(k in _q for k in ("sn", "device_id", "signature", "prehash", "metrics",
                            "hw_version_2", "api_signature")), True)
check("identity: js.not_valid is echoed as not_valid_token", _q["not_valid_token"], "1")
check("identity: cookies carry the MAC in colon form (panels compare it verbatim)",
      cookies_for("http://h/c/portal.php", "00:1a:79:aa:aa:01")["mac"], MAC0)
check("identity: referer points at the portal's own index.html",
      referer_for("http://h:8080/stalker_portal/c/portal.php"),
      "http://h:8080/stalker_portal/c/index.html")
check("identity: normalize_mac is idempotent", normalize_mac(normalize_mac(MAC0)), MAC0)

# --------------------------------------------------------------------------- #
# what the portal says about the account becomes a decision
# --------------------------------------------------------------------------- #
check("account: a panel's 'blocked' is a ban, whatever the expiry says",
      account_verdict(profile={"blocked": "1"}, info={"phone": "2032-12-31"}, token="t").status,
      "banned")
check("account: string '0' is not blocked (bool('0') would say otherwise)",
      account_verdict(profile={"blocked": "0"}, token="t").status, "active")
check("account: a past expiry is expired, and it removes the MAC from the chains",
      (account_verdict(profile={}, info={"phone": "2024-01-01"}, token="t").status,
       mac_is_usable("expired")), ("expired", False))
check("account: our own transport verdicts stay retryable",
      mac_is_usable("offline"), True)
check("account: no token is an authorization problem, not an outage",
      mac_status(account_verdict(profile={}, info={}, token="")), "unauthorized")
check("account: an unreadable expiry is no verdict at all",
      parse_expiry("Unknown"), None)
check("account: European billing dates are parsed",
      str(parse_expiry("31.12.2032")), "2032-12-31 00:00:00+00:00")

# --------------------------------------------------------------------------- #
# dead code stays dead, and the answers that matter stay consumed
# --------------------------------------------------------------------------- #
# `profile()` and `short_epg()` were methods nothing ever called, and each built
# its own http client - a second path that quietly ignored the portal proxy and
# the TLS policy. `_network_identity` was renamed when it grew the HLS input
# options (it is no longer only about identity). Any of them coming back means a
# bypass is being reintroduced, so grep for them instead of trusting a review.
FORBIDDEN = {
    "def profile(": "dead code; the fingerprint call lives in stb_profile()",
    "def short_epg(": "dead code: it ignored the portal proxy and TLS policy",
    "_network_identity": "renamed to _network_input_options (HLS opts are not identity)",
}


def _grep(sym: str, where: str) -> list[str]:
    r = subprocess.run(["grep", "-rn", "--include=*.py", "--", sym, where],
                       capture_output=True, text=True)
    return [line for line in r.stdout.strip().splitlines() if "__pycache__" not in line]


for sym, why in FORBIDDEN.items():
    hits = _grep(sym, "app")
    check(f"no {sym!r} in app/ ({why})", hits, [])

# The mirror-image rule, and the one EStalker itself breaks: if the portal told
# us something we can act on, something must act on it. An identity call whose
# answer is discarded is how `get_profile` was dead here for its whole life, and
# "read blocked, ignore blocked" is how an expired MAC stayed in every chain.
WIRED = {
    "stb_profile(": "the fingerprint must be sent from the handshake, not on request",
    "refresh_account(": "test_portal and the fetch job must both consume the verdict",
}
for sym, why in WIRED.items():
    hits = _grep(sym, "app")
    check(f"{sym} has a caller ({why})", bool(hits), True)

# --------------------------------------------------------------------------- #
failed = 0
for label, got, want in CASES:
    if got == want:
        print(f"   OK   {label}")
    else:
        failed += 1
        print(f"   FAIL {label}\n          got:  {got}\n          want: {want}")

print()
if failed:
    print(f"check-links: {failed}/{len(CASES)} FAILED")
    sys.exit(1)
print(f"check-links OK ({len(CASES)} cases)")
