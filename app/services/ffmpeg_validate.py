"""Run an ffmpeg template against a short demo input (or syntax-only)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import signal
import time

from ..config import FFMPEG_BIN
from . import stream_identity
from .ffmpeg_templates import (REDIRECT_COMMAND, URL_PLACEHOLDER,
                                template_command_errors)

# 10-second H.264 360p clip (CC-BY Big Buck Bunny) — small enough to probe
# a real HTTP input without downloading a movie.
TEST_VIDEO_URL = (
    "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/"
    "Big_Buck_Bunny_360_10s_1MB.mp4"
)
LAVFI_VIDEO = "testsrc2=size=640x360:rate=25:duration=4,format=yuv420p"
_NETONLY = re.compile(
    r"-(?:reconnect\w*|-?rw_timeout|timeout)\b\s*(?:\S+)?"
)
MAX_STDERR = 256 * 1024
DEMO_TIMEOUT_S = 20
PLAYLIST_DEMO_TIMEOUT_S = 30
# Grace period for a killed ffmpeg to be reaped before we stop waiting on it.
# A demo must always answer the GUI, even if a child process refuses to die.
REAP_GRACE_S = 3.0

# A demo is a ~2 s diagnostic, not a stream: it must fail FAST and report why.
# The stream path deliberately retries a flaky live link for minutes
# (-reconnect_delay_max 5 with unlimited attempts, plus a 10 s -rw_timeout per
# attempt). Inside a demo that same policy is what produces the useless
# "timed out after 30s / bytes=0 / rc=-9" result: ffmpeg was still politely
# reconnecting to a dead link when the harness killed it, so the operator sees
# a killed process instead of the panel's actual error.
#
# These caps are therefore applied to the demo argv only (never to the real
# stream path): a short connect attempt, a short read timeout and a tight
# reconnect backoff, so ffmpeg gives up and prints a real diagnostic
# (403 / 404 / Connection timed out) well before our own kill fires.
DEMO_RW_TIMEOUT_US = 5_000_000        # 5 s without data on the input socket
DEMO_CONNECT_TIMEOUT_US = 5_000_000   # 5 s to establish the TCP connection
DEMO_RECONNECT_DELAY_MAX = 2          # cap the backoff (template often says 5)


def syntax_check(command: str) -> dict:
    cmd = (command or "").strip()
    if cmd == REDIRECT_COMMAND:
        return {"ok": True, "mode": "syntax", "detail": "redirect template (no ffmpeg)"}
    if not cmd:
        return {"ok": False, "mode": "syntax", "detail": "empty command"}
    if not cmd.startswith("ffmpeg"):
        return {"ok": False, "mode": "syntax", "detail": "command must start with ffmpeg"}
    if URL_PLACEHOLDER not in cmd:
        return {"ok": False, "mode": "syntax", "detail": "command must contain <url>"}
    if "<out_dir>" in cmd:
        return {"ok": False, "mode": "syntax",
                "detail": "HLS file output cannot be live-tested; use mpegts pipe"}
    try:
        toks = shlex.split(cmd)
    except ValueError as exc:
        return {"ok": False, "mode": "syntax", "detail": f"unbalanced quotes: {exc}"}
    if "-i" not in toks:
        return {"ok": False, "mode": "syntax", "detail": "no -i input"}
    errors = template_command_errors(cmd)
    if errors:
        return {"ok": False, "mode": "syntax",
                "detail": "invalid FFmpeg argument structure: " + "; ".join(errors)}
    return {"ok": True, "mode": "syntax",
            "detail": f"{len(toks)} tokens, placeholder at input"}


def _set_input_opt(toks: list[str], flag: str, value: str) -> list[str]:
    """Force `flag value` in front of the LAST -i (an input option).

    Overwrites the template's value when the flag is already there, so a
    template carrying `-reconnect_delay_max 5` cannot out-wait the demo.
    """
    try:
        i_idx = max(n for n, t in enumerate(toks) if t == "-i")
    except ValueError:
        return toks
    for n, t in enumerate(toks):
        if t == flag and n < i_idx and n + 1 < len(toks):
            toks[n + 1] = value
            return toks
    toks[i_idx:i_idx] = [flag, value]
    return toks


def _bound_network_input(toks: list[str]) -> list[str]:
    """Make a network demo fail fast with ffmpeg's OWN error message.

    Only touches argv when the input is an http(s) URL - a file or lavfi input
    has no reconnect/timeout semantics and ffmpeg rejects the options outright.
    """
    try:
        i_idx = max(n for n, t in enumerate(toks) if t == "-i")
    except ValueError:
        return toks
    target = toks[i_idx + 1] if i_idx + 1 < len(toks) else ""
    if not target.lower().startswith(("http://", "https://")):
        return toks
    toks = _set_input_opt(toks, "-rw_timeout", str(DEMO_RW_TIMEOUT_US))
    toks = _set_input_opt(toks, "-timeout", str(DEMO_CONNECT_TIMEOUT_US))
    # Only bound reconnection if the template already asked for it. ffmpeg
    # aborts with "Unrecognized option" on a flag its build does not know, and
    # -reconnect_delay_max is the one retry control present in every build that
    # supports -reconnect at all ("give up once the backoff exceeds this").
    if any(t.startswith("-reconnect") for t in toks[:i_idx]):
        toks = _set_input_opt(toks, "-reconnect_delay_max",
                              str(DEMO_RECONNECT_DELAY_MAX))
    return toks


def _bound_demo(toks: list[str]) -> list[str]:
    """Cap a demo run at 2s and never write HLS files during a probe."""
    if "-t" not in toks:
        try:
            i = toks.index("-i")
            insert_at = i + 2 if i + 1 < len(toks) else len(toks)
            toks[insert_at:insert_at] = ["-t", "2"]
        except ValueError:
            toks += ["-t", "2"]
    if "-f" in toks:
        fi = len(toks) - 1 - toks[::-1].index("-f")
        if fi + 1 < len(toks) and toks[fi + 1] == "hls":
            toks[fi + 1] = "mpegts"
            toks = [t for t in toks if t != "<out_dir>/index.m3u8"]
            if toks[-1:] != ["pipe:1"]:
                toks += ["pipe:1"]
    # Demos are about seeing *why* a template works or fails. A template that
    # happens to carry -loglevel error would hide the very output we want.
    if "-loglevel" not in toks and "-v" not in toks:
        toks[1:1] = ["-loglevel", "info"]
    return _bound_network_input(toks)


def _argv(command: str, url: str, *, lavfi: bool) -> list[str]:
    cmd = command.strip()
    if lavfi:
        cmd = _NETONLY.sub(" ", cmd)
        cmd = cmd.replace(URL_PLACEHOLDER, LAVFI_VIDEO)
        cmd = re.sub(r"(\s)-i\s+", r"\1-f lavfi -i ", cmd, count=1)
    else:
        cmd = cmd.replace(URL_PLACEHOLDER, url)
    if cmd.startswith("ffmpeg"):
        cmd = FFMPEG_BIN + cmd[len("ffmpeg"):]
    return _bound_demo(shlex.split(cmd))


def _playlist_argv(command: str, url: str, user_agent: str | None = None) -> list[str]:
    """Build argv the same way the stream path does (UA / referer / HLS opts)."""
    from .stream_manager import StreamManager
    args = StreamManager._ffmpeg_argv(command, url, user_agent=user_agent)
    if not args:
        return _argv(command, url, lavfi=False)
    return _bound_demo(list(args))


def _timeout_hint(err: str, out_n: int) -> str:
    """Explain a killed demo, because 'bytes=0 rc=-9' names no culprit.

    rc=-9 is OUR SIGKILL, so the interesting evidence is what ffmpeg had
    already printed on stderr. The checks are ordered most-specific first.
    """
    low = (err or "").lower()
    if "no such device" in low or "failed to initialise vaapi" in low \
            or "device creation failed" in low or "cannot open the drm device" in low:
        return ("hardware encoder unavailable (VAAPI/QSV device missing) — "
                "map /dev/dri into the container or test a copy/software template")
    if "403 forbidden" in low:
        return "the panel answered 403 Forbidden (MAC/token rejected)"
    if "404 not found" in low:
        return "the panel answered 404 Not Found (stale link — re-fetch the source)"
    # The non-standard 456 is its own diagnosis (see stream_identity): it is
    # the Stalker/WAF "unrecoverable" answer, and the two causes are very
    # different to fix. ffmpeg 7 prints it as "HTTP error 456" and/or the
    # wrapper "Server returned 4XX Client Error, but not one of 40{0,1,3,4}".
    if "http error 456" in low or "returned 4xx client error" in low:
        return ("HTTP 456 is the panel's non-standard 'unrecoverable' answer "
                "on the media endpoint, not a template error: either the MAC's "
                "single connection slot was still held (a stream running on "
                "that MAC — stop the channel on the box and re-run), or the "
                "origin's anti-proxy layer refuses this server's requests "
                "(taste the token with dev/probe-link.py --read 8192, or try "
                "SPM_PLAYER_UA=… / SPM_STREAM_UA_LADDER=0)")
    if "401 unauthorized" in low or "http error 4" in low:
        return "the panel refused the request (HTTP 4xx)"
    if "will reconnect" in low or "connection timed out" in low:
        return "the source never delivered data (connect/read timed out)"
    if "connection refused" in low:
        return "connection refused by the source host"
    if "immediate exit requested" in low:
        return "ffmpeg was still shutting down"
    if out_n == 0 and "stream mapping" in low:
        return "ffmpeg opened the input but produced no output (encoder stalled)"
    if out_n == 0:
        return "ffmpeg never opened the input"
    return ""


async def _terminate(proc) -> None:
    """Kill the demo's whole process group and never block on the reap.

    `proc.kill()` alone signals the direct child only. A stalled ffmpeg spawned
    via a wrapper leaves a grandchild holding the stdout pipe, and the
    subsequent `await proc.wait()` then hangs indefinitely - which turned a
    30 s demo timeout into a request that never returned. SIGKILL goes to the
    group, and the reap itself is bounded.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(proc.wait()), REAP_GRACE_S)


def _result(*, ok: bool, mode: str, detail: str, args: list[str] | None = None,
            out_n: int = 0, rc=None, err: str = "", ms: int = 0,
            source: str = "") -> dict:
    argv = list(args or [])
    return {
        "ok": ok, "mode": mode, "detail": detail, "bytes": out_n, "rc": rc,
        "stderr": err, "ms": ms, "source": source,
        "argv": argv,
        "argv_text": " ".join(shlex.quote(a) for a in argv),
    }


async def run_demo(command: str, mode: str = "lavfi", url: str | None = None,
                   source_label: str | None = None) -> dict:
    """Spawn ffmpeg with the template; return bytes/stderr/rc plus the argv."""
    syn = syntax_check(command)
    if mode == "syntax" or not syn["ok"]:
        return syn
    if (command or "").strip() == REDIRECT_COMMAND:
        return syn

    lavfi = mode == "lavfi"
    src = (url or "").strip() or TEST_VIDEO_URL
    try:
        if lavfi:
            args = _argv(command, src, lavfi=True)
        elif mode == "playlist":
            args = _playlist_argv(command, src)
        else:
            args = _argv(command, src, lavfi=False)
    except ValueError as exc:
        return _result(ok=False, mode=mode, detail=str(exc), source=source_label or src)

    source = source_label or ("lavfi testsrc2" if lavfi else src)

    # Playlist demos hit a real resolved media URL, so they walk the same
    # media-UA ladder as the stream path: panels that 456/403 the first
    # identity get one retry with the other before the demo reports failure.
    # A template that sets its own -user_agent opts out, exactly as in the
    # pump. Every other mode is a single local attempt.
    if mode == "playlist" and "-user_agent" not in (command or ""):
        attempts = [(_playlist_argv(command, src, ua), ua)
                    for ua in stream_identity.ladder(src)]
    else:
        attempts = [(args, None)]

    async def _one(argv: list[str]) -> dict:
        started = time.perf_counter()
        try:
            # start_new_session: ffmpeg gets its own process group, so a timeout
            # can kill the whole tree. Without it a wrapper script's child (or an
            # ffmpeg that forked) survives, keeps the stdout pipe open and makes
            # the reaping `proc.wait()` below block forever - the demo then never
            # answers the GUI at all.
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except FileNotFoundError:
            return _result(ok=False, mode=mode, detail=f"ffmpeg not found: {argv[0]}",
                           args=argv, source=source)

        out_n = 0
        err_buf = bytearray()

        async def _stdout() -> None:
            nonlocal out_n
            assert proc.stdout
            while True:
                chunk = await proc.stdout.read(64 * 1024)
                if not chunk:
                    return
                out_n += len(chunk)

        async def _stderr() -> None:
            assert proc.stderr
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                err_buf.extend(chunk)
                if len(err_buf) > MAX_STDERR:
                    del err_buf[:len(err_buf) - MAX_STDERR]

        timeout = PLAYLIST_DEMO_TIMEOUT_S if mode == "playlist" else DEMO_TIMEOUT_S
        try:
            await asyncio.wait_for(asyncio.gather(_stdout(), _stderr(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            await _terminate(proc)
            err_text = err_buf.decode(errors="replace")
            detail = f"timed out after {timeout}s"
            hint = _timeout_hint(err_text, out_n)
            if hint:
                detail = f"{detail} — {hint}"
            return _result(
                ok=False, mode=mode, detail=detail,
                args=argv, out_n=out_n, rc=proc.returncode,
                err=err_text,
                ms=int((time.perf_counter() - started) * 1000),
                source=source)

        rc = proc.returncode
        err = err_buf.decode(errors="replace")
        ms = int((time.perf_counter() - started) * 1000)
        ok = rc == 0 and out_n > 0
        if rc == 0 and out_n == 0:
            detail = "ffmpeg exited 0 but produced no output bytes"
            ok = False
        elif rc not in (0, None) and out_n == 0:
            detail = f"ffmpeg exited rc={rc} with no output"
            # A fast death (rc=8 on a refused input) is where ffmpeg's stderr
            # carries the whole diagnosis (456/403/404); a bare rc used to be
            # all the GUI saw, so the hint is attached here, not only on the
            # timeout path.
            hint = _timeout_hint(err, out_n)
            if hint:
                detail = f"{detail} — {hint}"
        elif rc not in (0, None):
            # Some builds exit 255 after -t even when bytes flowed.
            ok = out_n > 8000
            detail = f"ffmpeg rc={rc}, {out_n} bytes in {ms} ms"
        else:
            detail = f"{out_n} bytes in {ms} ms"
        return _result(ok=ok, mode=mode, detail=detail, args=argv, out_n=out_n,
                       rc=rc, err=err, ms=ms, source=source)

    last: dict | None = None
    for idx, (argv, ua) in enumerate(attempts):
        res = await _one(argv)
        if res.get("ok"):
            if ua is not None:
                stream_identity.remember(src, ua)
            return res
        # `bytes == 0` matters: an identity refusal can only happen while
        # opening the input, which never produces output bytes. A 4xx that
        # appears AFTER bytes flowed is the panel cutting a live connection
        # mid-stream (slot pressure, token rotation) - respawning with the
        # other identity would replace that real diagnosis with a clean
        # second 456, which is what a mid-stream death does not deserve.
        if (ua is not None and idx + 1 < len(attempts) and res.get("bytes") == 0
                and stream_identity.http_open_error(
                    res.get("rc"), res.get("stderr", ""),
                    (res.get("ms") or 0) / 1000.0) is not None):
            # Origin refused this identity on the media endpoint: show the
            # ladder decision in the very tab the operator is watching.
            res["detail"] = (f"{res['detail']} - origin rejected this "
                             f"user-agent; retrying once with the other one")
            last = res
            continue
        return res
    return last if last is not None else {"ok": False, "mode": mode,
                                          "detail": "no attempt ran"}
