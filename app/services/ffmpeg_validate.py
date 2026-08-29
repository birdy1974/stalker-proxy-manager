"""Run an ffmpeg template against a short demo input (or syntax-only)."""

from __future__ import annotations

import asyncio
import re
import shlex
import time

from ..config import FFMPEG_BIN
from .ffmpeg_templates import REDIRECT_COMMAND, URL_PLACEHOLDER

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
    return {"ok": True, "mode": "syntax",
            "detail": f"{len(toks)} tokens, placeholder at input"}


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
    toks = shlex.split(cmd)
    # Bound the run so a working encoder cannot sit forever.
    if "-t" not in toks:
        try:
            i = toks.index("-i")
            insert_at = i + 2 if i + 1 < len(toks) else len(toks)
            toks[insert_at:insert_at] = ["-t", "2"]
        except ValueError:
            toks += ["-t", "2"]
    # Never write HLS files during a probe.
    if "-f" in toks:
        fi = len(toks) - 1 - toks[::-1].index("-f")
        if fi + 1 < len(toks) and toks[fi + 1] == "hls":
            toks[fi + 1] = "mpegts"
            toks = [t for t in toks if t != "<out_dir>/index.m3u8"]
            if toks[-1:] != ["pipe:1"]:
                toks += ["pipe:1"]
    return toks


async def run_demo(command: str, mode: str = "lavfi", url: str | None = None) -> dict:
    """Spawn ffmpeg with the template; return bytes/stderr/rc."""
    syn = syntax_check(command)
    if mode == "syntax" or not syn["ok"]:
        return syn
    if (command or "").strip() == REDIRECT_COMMAND:
        return syn

    lavfi = mode == "lavfi"
    src = (url or "").strip() or TEST_VIDEO_URL
    try:
        args = _argv(command, src, lavfi=lavfi)
    except ValueError as exc:
        return {"ok": False, "mode": mode, "detail": str(exc)}

    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return {"ok": False, "mode": mode, "detail": f"ffmpeg not found: {args[0]}"}

    out_n = 0
    err_chunks: list[bytes] = []

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
            line = await proc.stderr.readline()
            if not line:
                return
            err_chunks.append(line)
            del err_chunks[:-40]

    try:
        await asyncio.wait_for(asyncio.gather(_stdout(), _stderr(), proc.wait()), 20)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return {"ok": False, "mode": mode, "detail": "timed out after 20s",
                "bytes": out_n, "rc": proc.returncode,
                "stderr": b"".join(err_chunks[-12:]).decode(errors="replace")[-1500:],
                "ms": int((time.perf_counter() - started) * 1000),
                "source": "lavfi testsrc2" if lavfi else src}

    rc = proc.returncode
    err = b"".join(err_chunks[-16:]).decode(errors="replace")[-1800:]
    ms = int((time.perf_counter() - started) * 1000)
    ok = rc == 0 and out_n > 0
    if rc == 0 and out_n == 0:
        detail = "ffmpeg exited 0 but produced no output bytes"
        ok = False
    elif rc not in (0, None) and out_n == 0:
        detail = f"ffmpeg exited rc={rc} with no output"
    elif rc not in (0, None):
        # Some builds exit 255 after -t even when bytes flowed.
        ok = out_n > 8000
        detail = f"ffmpeg rc={rc}, {out_n} bytes in {ms} ms"
    else:
        detail = f"{out_n} bytes in {ms} ms"
    return {"ok": ok, "mode": mode, "detail": detail, "bytes": out_n, "rc": rc,
            "stderr": err, "ms": ms,
            "source": "lavfi testsrc2" if lavfi else src}
