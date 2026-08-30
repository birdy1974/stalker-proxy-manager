#!/usr/bin/env python3
# ============================================================================
# Self-check for the portal plumbing that decides whether a channel plays:
# the link helpers in app/portal/client.py, the STB identity in
# app/portal/identity.py, the account verdict in app/portal/account.py, the link
# policy in app/portal/links.py, and the capability probe in
# app/portal/capabilities.py.
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
import pathlib
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
from app.portal.capabilities import (dumps_modules, enabled_modules,  # noqa: E402
                                     gate_feature, loads_modules, parse_modules,
                                     parse_version_js, supports, version_js_url)
from app.portal.identity import (cookies_for, derive_identity, normalize_mac,  # noqa: E402
                                 profile_params, referer_for)
from app.portal.links import (FLAG_DISABLE_AD, FLAG_LOAD_BALANCING,  # noqa: E402
                              FLAG_TMP_LINK, REBUILD_FLAGS, has_flag,
                              link_policy, link_request_params,
                              parse_link_flags, plan_for, split_flags,
                              why_not_self_served)

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
# links.py - what the panel said about a channel, and what follows from it
# --------------------------------------------------------------------------- #
# R2 turns a per-channel flag into "do we pay the portal a create_link on every
# play?". Both directions are cheap to get wrong: ask always and the flags are
# pointless; skip always and a stale token becomes a black screen with no
# fallback chain behind it. So the table is pinned here, where a NAS can run it.
URL = "http://host/play/live.php?stream=392166&extension=ts"

check("flag -1 is 'not told', not 'no'",
      parse_link_flags({"use_http_tmp_link": -1, "disable_ad": -1}), None)
check("absent flags are 'not told'", parse_link_flags({"name": "Ch"}), None)
check("zeros are 'nothing applies'",
      parse_link_flags({"use_http_tmp_link": 0, "disable_ad": 0}), "")
check("the set flags are named",
      parse_link_flags({"use_load_balancing": "1", "disable_ad": "true"}),
      f"{FLAG_LOAD_BALANCING},{FLAG_DISABLE_AD}")
check("a non-dict row tells us nothing", parse_link_flags("ffmpeg http://h"), None)
check("flags split tolerantly", split_flags(' "use_http_tmp_link" , DISABLE_AD '),
      [FLAG_TMP_LINK, FLAG_DISABLE_AD])
check("unknown flags are not 'no flags'",
      (split_flags(None) == [], has_flag(None, FLAG_TMP_LINK)), (True, False))

check("a permanent link plays as stored",
      link_policy(url=URL, link_flags="").direct, True)
check("an un-fetched row still asks",
      link_policy(url=URL, link_flags=None).create_link, True)
check("a tmp link must be rebuilt",
      link_policy(url=URL, link_flags=FLAG_TMP_LINK).create_link, True)
check("load balancing must be rebuilt",
      link_policy(url=URL, link_flags=FLAG_LOAD_BALANCING).create_link, True)
check("disable_ad must NOT be a rebuild flag",
      link_policy(url=URL, link_flags=FLAG_DISABLE_AD).direct, True)
check("the MAC's force_ch_link_check wins",
      link_policy(url=URL, link_flags="", force_ch_link_check=True).create_link, True)
check("the portal's distrust wins",
      link_policy(url=URL, link_flags="", allow_direct=False).create_link, True)
check("ffmpeg always asks",
      link_policy(url=URL, link_flags="", ffmpeg=True).create_link, True)
check("every answer explains itself",
      bool(link_policy(url=URL, link_flags="").reason)
      and bool(link_policy(url="", link_flags=None).reason), True)

check("a session token forbids the fast path",
      "session token" in why_not_self_served(URL + "&play_token=abc"), True)
check("a usertoken forbids it too",
      "session token" in why_not_self_served("http://h/a.ts?usertoken=1"), True)
check("a template cmd forbids it",
      "template" in why_not_self_served("/ch/101.ts"), True)
check("a relative path is not a URL",
      "absolute" in why_not_self_served("play/live.php?stream=1"), True)
check("the clean URL is allowed", why_not_self_served(URL), "")
check("%mac% is ours to fill, not a blocker",
      why_not_self_served("http://h/a.ts?mac=%mac%"), "")
check("%mac% is filled in the stored link",
      apply_mac_placeholder("http://h/a.ts?m=%25MAC%25", "00:1A:79:AA:AA:01"),
      "http://h/a.ts?m=00:1A:79:AA:AA:01")

# the plan is the one place a row is read; a call site that reaches for
# `src.cmd` itself is how the two stream paths grew different rules once
Plan = type("R", (), {})


def _row(cmd, flags):
    r = Plan()
    r.cmd, r.link_flags = cmd, flags
    return r


def _mac(force=False):
    m = Plan()
    m.mac, m.force_ch_link_check = "00:1A:79:AA:AA:01", force
    return m


check("the plan keeps the whole cmd for asking",
      plan_for(_row(f"ffmpeg {URL}", FLAG_TMP_LINK), _mac()).cmd, f"ffmpeg {URL}")
check("the plan hands out the extracted URL for playing",
      plan_for(_row(f"ffmpeg {URL}", ""), _mac()).direct_url, URL)
check("the plan refuses when the MAC is watched",
      plan_for(_row(f"ffmpeg {URL}", ""), _mac(force=True)).policy.create_link, True)
check("a row without the column behaves as un-fetched",
      plan_for(_row(f"ffmpeg {URL}", None), _mac()).policy.flags_known, False)
check("the request keeps the channel's own flags",
      link_request_params(link_flags=FLAG_DISABLE_AD, force_ch_link_check=False),
      {"series": "0", "forced_storage": "false", "disable_ad": "true",
       "download": "false", "force_ch_link_check": "false"})
check("a forced check reaches the panel",
      link_request_params(link_flags=None, force_ch_link_check=True, series=True)
      ["force_ch_link_check"], "true")
check("the volatile params are the same list both sides use",
      FLAG_DISABLE_AD in REBUILD_FLAGS, False)
check("stripping the token is what makes the comparison possible",
      strip_volatile(URL + "&play_token=OLD") + "|", URL + "|")

# --------------------------------------------------------------------------- #
# capabilities.py - what the panel says about itself
# --------------------------------------------------------------------------- #
check("version.js var form", parse_version_js("var ver = '5.4.2';").portal, "5.4.2")
check("xpcom object form",
      parse_version_js('xpcom.version = { portal: "5.3.14" };').portal, "5.3.14")
check("the ver-string fragment",
      parse_version_js("PORTAL version: 5.2.0; API Version: 343;").portal, "5.2.0")
check("the image version is kept",
      parse_version_js("var ver='5.4.2'; var ImageDescription = '0.2.20';").image, "0.2.20")
check("the product is named",
      parse_version_js("/* Ministra */ var ver='5.4.2';").product, "ministra")
check("an HTML page is never a version",
      parse_version_js("<html><body>var ver = '9.9';</body></html>").known, False)
check("an empty file is not a version", parse_version_js("").known, False)
check("the version file is next to portal.php",
      version_js_url("http://h/stalker_portal/c/portal.php"),
      "http://h/stalker_portal/c/version.js")

check("modules minus disabled",
      enabled_modules({"js": {"all_modules": ["tv", "vclub", "sclub"],
                              "disabled_modules": ["sclub"]}}), ["tv", "vclub"])
check("a dict of modules is read",
      enabled_modules({"js": {"all_modules": {"tv": {"status": 1}, "vclub": {"status": 0}},
                              "disabled_modules": []}}), ["tv"])
check("a module marked off inside all_modules is disabled, not lost",
      parse_modules({"js": {"all_modules": {"tv": {"status": 0}, "sclub": {"status": 1}}}}),
      (["sclub"], ["tv"]))
check("a json string list is read",
      enabled_modules({"js": {"all_modules": '["tv", "vclub"]'}}), ["tv", "vclub"])
check("a comma string is read",
      enabled_modules({"js": {"all_modules": "tv, vclub"}}), ["tv", "vclub"])
check("an empty answer is unknown, not 'nothing'",
      enabled_modules({"js": {"all_modules": []}}), None)
check("no answer at all is unknown", enabled_modules({"js": {"error": "no action"}}), None)
check("the gate allows an unknown panel", gate_feature(None, "vod"), (True, ""))
check("the gate refuses without the module", gate_feature(["tv"], "vod")[0], False)
check("the refusal names the module", "vclub" in gate_feature(["tv"], "vod")[1], True)
check("live answers to tv or itv", supports(["itv"], "live"), True)
check("the archive has two spellings",
      supports(["captured_tv_archive"], "archive"), True)
check("a module we do not gate on is allowed", supports(["tv"], "whatever"), None)
check("the stored answer round-trips sorted",
      loads_modules(dumps_modules(["vclub", "tv"])), ["tv", "vclub"])
check("unknown is stored as NULL, never as []", dumps_modules(None), None)
check("junk in a restored backup degrades to unknown", loads_modules("{oops}"), None)

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
    # R6: three probes whose whole value is being *used*. A version nobody stores
    # is a log line; a module list nobody reads is a 30-minute fetch job against
    # a panel that has no sclub.
    "read_version_js(": "the resolver must store the answer while discovering the portal",
    "portal_modules(": "Resolve must ask get_modules, or the GUI has nothing to grey out",
    "gate_feature(": "the fetch jobs must refuse, or the answer changes nothing",
    # R2: one policy, read by every path that opens a stream.
    "parse_link_flags(": "the fetch must store the flags, or the policy has nothing to read",
    "why_not_self_served(": "the policy must consult the URL shape, or a token is played as fresh",
}
for sym, why in WIRED.items():
    hits = _grep(sym, "app")
    check(f"{sym} has a caller ({why})", bool(hits), True)

# The decision itself may not be re-implemented at a call site: `links.py` is
# imported by the stream manager (plan) and by the popup probe (policy), and one
# file means one behaviour. Two callers is the floor; a third copy is the bug.
_policy_files = {line.split(":")[0] for line in _grep("plan_for(", "app")
                 + _grep("link_policy(", "app")} - {"app/portal/links.py"}
check("the link policy is shared, not inlined", len(_policy_files) >= 2, True)
check("the stream manager consults it", "app/services/stream_manager.py" in _policy_files, True)

# and the GUI switch must exist next to the column it writes, because a setting
# reachable only by API is a setting that will be reported as "not working"
_tpl = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "portals.html"
check("the portals GUI carries the direct-links switch",
      "p-direct-links" in _tpl.read_text(), True)
check("the portals GUI shows what the panel said",
      "portal_version" in _tpl.read_text(), True)

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
