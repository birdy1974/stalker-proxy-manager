"""FFmpeg demo: a dead source must report WHY, not just 'rc=-9 after 30s'.

Reported symptom (live #117 F1TV, a stalled MAG link):

    ✘ demo playlist source (live #117) — timed out after 30s
    bytes=0   rc=-9   30203 ms

Two separate defects produced that useless result:

1. The template's live-streaming retry policy (`-reconnect* ` with
   `-reconnect_delay_max 5` and a 10 s `-rw_timeout`) was handed to the demo
   unchanged. ffmpeg kept reconnecting to the dead link for the full 30 s
   budget, so our harness SIGKILLed it (rc=-9) before ffmpeg ever printed its
   own diagnosis. A demo must fail fast and surface the panel's real error.

2. The timeout path did `proc.kill()` + an UNBOUNDED `await proc.wait()`.
   `kill()` signals only the direct child, so any surviving grandchild kept the
   stdout pipe open and the reap blocked forever - the request never returned
   at all.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import time

import pytest

from app.services import ffmpeg_validate as fv
from app.services import stream_manager as sm
from app.services.ffmpeg_templates import URL_PLACEHOLDER

# The template from the bug report, trimmed of the VAAPI bits a CI box lacks.
F1TV_CMD = (
    "ffmpeg -loglevel info -reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -fflags +genpts+discardcorrupt -err_detect ignore_err "
    f"-rw_timeout 10000000 -analyzeduration 1000000 -probesize 1000000 -i {URL_PLACEHOLDER} "
    "-t 2 -c:v libx264 -c:a mp2 -f mpegts pipe:1"
)
STALLED_URL = "http://backup.xp1.tv:80/play/live.php?mac=00:1A:79:44:EE:FA&stream=127256"


def _opt(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def _use_fake_ffmpeg(monkeypatch, path: str) -> None:
    """Point BOTH binary lookups at the stub.

    mode="playlist" renders argv through StreamManager._ffmpeg_argv, which
    substitutes its own imported FFMPEG_BIN - patching only ffmpeg_validate
    would still spawn the system ffmpeg.
    """
    monkeypatch.setattr(fv, "FFMPEG_BIN", path)
    monkeypatch.setattr(sm, "FFMPEG_BIN", path)


# --------------------------------------------------------------------------
# 1. the demo argv must be bounded so ffmpeg gives up before our SIGKILL
# --------------------------------------------------------------------------

def test_demo_caps_reconnect_and_timeouts_on_network_input():
    args = fv._argv(F1TV_CMD, STALLED_URL, lavfi=False)
    # the template's generous live-streaming values are overridden, not kept
    assert _opt(args, "-rw_timeout") == str(fv.DEMO_RW_TIMEOUT_US)
    assert _opt(args, "-reconnect_delay_max") == str(fv.DEMO_RECONNECT_DELAY_MAX)
    assert _opt(args, "-timeout") == str(fv.DEMO_CONNECT_TIMEOUT_US)
    # ...and they stay INPUT options (before -i), or ffmpeg ignores them
    i_idx = args.index("-i")
    for flag in ("-rw_timeout", "-timeout", "-reconnect_delay_max"):
        assert args.index(flag) < i_idx, f"{flag} must precede -i"


def test_demo_budget_exceeds_ffmpegs_own_give_up_time():
    """The harness kill must be the LAST resort, not the usual outcome."""
    worst_case_s = (fv.DEMO_CONNECT_TIMEOUT_US + fv.DEMO_RW_TIMEOUT_US) / 1e6
    assert worst_case_s < fv.PLAYLIST_DEMO_TIMEOUT_S


def test_demo_does_not_add_network_opts_to_local_or_lavfi_input():
    """`-rw_timeout` on a file input is an ffmpeg hard error."""
    local = fv._bound_demo(["ffmpeg", "-i", "/media/movie.mkv", "-c", "copy",
                            "-f", "mpegts", "pipe:1"])
    assert "-rw_timeout" not in local
    assert "-timeout" not in local
    lavfi = fv._argv(F1TV_CMD, STALLED_URL, lavfi=True)
    assert "-rw_timeout" not in lavfi


def test_reconnect_caps_only_added_when_template_uses_reconnect():
    """Unknown flags abort ffmpeg outright, so never add them speculatively."""
    plain = fv._bound_demo(["ffmpeg", "-i", "http://h/s.ts", "-c", "copy",
                            "-f", "mpegts", "pipe:1"])
    assert "-reconnect_delay_max" not in plain


# --------------------------------------------------------------------------
# 2. a killed demo must still RETURN, and explain itself
# --------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_timeout_returns_even_when_child_outlives_the_kill(monkeypatch, tmp_path):
    """Regression: the reap used to block forever on a surviving grandchild."""
    fake = tmp_path / "ffmpeg"
    # a wrapper whose CHILD holds the inherited stdout pipe open after the
    # direct child is killed - exactly what hung the old `await proc.wait()`
    fake.write_text("#!/bin/sh\necho 'ffmpeg version fake' >&2\nsleep 120 &\nsleep 120\n")
    fake.chmod(0o755)
    _use_fake_ffmpeg(monkeypatch, str(fake))
    # 0.5 s proves the same thing as 2 s (the stub outlives the kill) and
    # keeps two of the slowest tests in the suite cheap
    monkeypatch.setattr(fv, "PLAYLIST_DEMO_TIMEOUT_S", 0.5)

    started = time.perf_counter()
    r = await asyncio.wait_for(
        fv.run_demo(f"ffmpeg -i {URL_PLACEHOLDER} -f mpegts pipe:1",
                    mode="playlist", url=STALLED_URL, source_label="live #117"),
        timeout=30,          # the bug made this hang forever
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 20, f"demo took {elapsed:.1f}s; reap is not bounded"
    assert r["ok"] is False
    assert "timed out" in r["detail"]
    assert r["argv_text"]


async def test_timeout_detail_explains_a_dead_source(monkeypatch, tmp_path):
    """rc=-9 names no culprit; the stderr ffmpeg DID print does."""
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '[http @ 0x1] Will reconnect at 0 in 1 second(s), error=Connection timed out.' >&2\n"
        "sleep 120\n")
    fake.chmod(0o755)
    _use_fake_ffmpeg(monkeypatch, str(fake))
    # 0.5 s proves the same thing as 2 s (the stub outlives the kill) and
    # keeps two of the slowest tests in the suite cheap
    monkeypatch.setattr(fv, "PLAYLIST_DEMO_TIMEOUT_S", 0.5)

    r = await asyncio.wait_for(
        fv.run_demo(f"ffmpeg -i {URL_PLACEHOLDER} -f mpegts pipe:1",
                    mode="playlist", url=STALLED_URL), timeout=30)
    assert r["ok"] is False
    assert "never delivered data" in r["detail"]
    assert "timed out after 0.5s" in r["detail"]


def test_timeout_hint_flags_a_missing_hardware_encoder():
    """The reported command used VAAPI; a box without /dev/dri must say so."""
    hint = fv._timeout_hint(
        "[AVHWDeviceContext @ 0x1] Failed to initialise VAAPI connection: "
        "No such device.\nDevice creation failed: -19.", 0)
    assert "hardware encoder unavailable" in hint


def test_timeout_hint_reports_panel_refusals():
    assert "403" in fv._timeout_hint("[http @ 0x1] HTTP error 403 Forbidden", 0)
    assert "404" in fv._timeout_hint("[http @ 0x1] HTTP error 404 Not Found", 0)


# --------------------------------------------------------------------------
# 2b. the playlist demo walks the same media-UA ladder as the stream path
# --------------------------------------------------------------------------

async def test_playlist_demo_retries_456_with_the_browser_ua(monkeypatch, tmp_path):
    """Origin X: the player UA is refused with HTTP 456, the browser UA
    plays. The demo must respawn once and report success - the exact failure
    that used to show rc=8 / 0 bytes in the FFmpeg tab."""
    from app.services import stream_identity
    stream_identity.reset()
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *Lavf53.32.100*)\n"
        "    echo '[https @ 0x9] HTTP error 456 Server returned 4XX Client Error' >&2\n"
        "    exit 8 ;;\n"
        "  *) head -c 200000 /dev/zero ;;\n"
        "esac\n")
    fake.chmod(0o755)
    _use_fake_ffmpeg(monkeypatch, str(fake))
    try:
        r = await fv.run_demo(f"ffmpeg -i {URL_PLACEHOLDER} -f mpegts pipe:1",
                              mode="playlist", url=STALLED_URL)
        assert r["ok"] is True, r["detail"]
        assert r["bytes"] == 200000
        # the winning identity is now shared with the real stream path
        assert stream_identity.learned(STALLED_URL) == stream_identity.STB_UA
    finally:
        stream_identity.reset()


# --------------------------------------------------------------------------
# 3. the working case must be untouched
# --------------------------------------------------------------------------

async def test_successful_demo_still_reports_ok(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'ffmpeg version fake' >&2\nhead -c 200000 /dev/zero\n")
    fake.chmod(0o755)
    _use_fake_ffmpeg(monkeypatch, str(fake))

    r = await fv.run_demo(F1TV_CMD, mode="playlist", url=STALLED_URL)
    assert r["ok"] is True
    assert r["bytes"] == 200000
    assert r["rc"] == 0


def test_bounded_argv_is_still_shell_quotable():
    """The GUI echoes argv_text; it must stay a valid, re-runnable command."""
    args = fv._argv(F1TV_CMD, STALLED_URL, lavfi=False)
    assert shlex.split(" ".join(shlex.quote(a) for a in args)) == args


# -------------------------------------------------------------------------- #
# 2c. HTTP 456: the panel's non-standard "unrecoverable" answer
#
# Reported symptom (live #117 F1TV): the demo died in 294 ms with
#     [http @ …] HTTP error 456
#     Error opening input: Server returned 4XX Client Error, but not one
#     of 40{0,1,3,4}
# and the GUI showed only "ffmpeg exited rc=8 with no output". The 456 line
# IS the diagnosis; the hint must be attached to the fast failure, not only
# to the timeout path.
# -------------------------------------------------------------------------- #

def test_timeout_hint_explains_a_456():
    err = ("[http @ 0x563bbb4f4680] HTTP error 456 \n"
           "[in#0 @ 0x563bbb4d3b80] Error opening input: Server returned 4XX "
           "Client Error, but not one of 40{0,1,3,4}\n"
           "Error opening input file "
           "http://backup.xp1.tv:80/play/live.php?mac=00:1A:79:44:EE:FA&"
           "stream=127256&extension=ts&play_token=***")
    hint = fv._timeout_hint(err, 0)
    assert "456" in hint
    assert "connection slot" in hint
    assert "anti-proxy" in hint


def test_timeout_hint_still_distinguishes_403_and_404():
    assert "403" in fv._timeout_hint("[http @ 0x1] HTTP error 403 Forbidden", 0)
    assert "404" in fv._timeout_hint("[http @ 0x1] HTTP error 404 Not Found", 0)
    # and a 456 is never swallowed by the generic 4xx line
    assert "403 forbidden" not in fv._timeout_hint(
        "[http @ 0x1] HTTP error 456", 0).lower()


async def test_fast_refusal_detail_carries_the_456_hint(monkeypatch, tmp_path):
    """rc=8 with zero bytes used to report nothing beyond the rc; the stderr
    (HTTP 456) is what tells the operator WHY."""
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '[http @ 0x9] HTTP error 456' >&2\n"
        "echo 'Error opening input: Server returned 4XX Client Error, "
        "but not one of 40{0,1,3,4}' >&2\n"
        "exit 8\n")
    fake.chmod(0o755)
    _use_fake_ffmpeg(monkeypatch, str(fake))
    r = await fv.run_demo(f"ffmpeg -i {URL_PLACEHOLDER} -f mpegts pipe:1",
                          mode="url", url=STALLED_URL)
    assert r["ok"] is False
    assert r["rc"] == 8
    assert "rc=8" in r["detail"]
    assert "456" in r["detail"]
    assert "connection slot" in r["detail"]


async def test_playlist_demo_does_not_spend_the_browser_rung_when_bytes_flew(
        monkeypatch, tmp_path):
    """A 456 AFTER output started is the panel cutting a live connection
    mid-stream (slot pressure), not an identity refusal. Respawning with the
    other UA would replace the real diagnosis ('rc=8, 5000 bytes') with a
    clean second 456 - so the browser rung must stay unspent and the first
    attempt's argv is what the operator sees."""
    from app.services import stream_identity
    stream_identity.reset()
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        "#!/bin/sh\n"
        "head -c 5000 /dev/zero\n"
        "echo '[http @ 0x9] HTTP error 456' >&2\n"
        "exit 8\n")
    fake.chmod(0o755)
    _use_fake_ffmpeg(monkeypatch, str(fake))
    try:
        r = await fv.run_demo(f"ffmpeg -i {URL_PLACEHOLDER} -f mpegts pipe:1",
                              mode="playlist", url=STALLED_URL)
    finally:
        stream_identity.reset()
    assert r["ok"] is False
    assert r["bytes"] == 5000
    argv_text = " ".join(r["argv"])
    # the FIRST rung (the player identity) is what ran last
    assert stream_identity.PLAYER_UA in argv_text
    assert stream_identity.STB_UA not in argv_text
    # and no identity was "remembered" for this origin
    assert stream_identity.learned(STALLED_URL) is None
