#!/usr/bin/env python3
# ============================================================================
# Self-check for the Stalker link helpers in app/portal/client.py
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
import sys
import tempfile

# importing app.portal.client pulls in app.config, which logs its banner and
# creates the data dir: keep both out of the way of this check
logging.disable(logging.CRITICAL)
os.environ.setdefault("SPM_DATA_DIR", tempfile.mkdtemp(prefix="spm-check-links-"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.portal.client import (extract_url, mask_token, merge_link,  # noqa: E402
                               sanitize_cmd, strip_volatile)

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
