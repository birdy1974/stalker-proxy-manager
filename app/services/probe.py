"""
Media probing for the detail popups (GUI round 3): run the bundled ffmpeg in
analyze mode against a local file or a network stream and extract codec /
resolution / aspect ratio / bitrate / fps / audio info from its stderr — the
same mechanism ffprobe would use, but with the single ffmpeg binary we ship.
"""

from __future__ import annotations

import asyncio
import re
import time

from ..config import FFMPEG_BIN, log

_RE_DURATION = re.compile(
    r"Duration:\s*(\d+):(\d+):([\d.]+)(?:,.*?bitrate:\s*(\d+)\s*kb/s)?", re.I)
_RE_STREAM_VIDEO = re.compile(
    r"Stream .*?:\s*Video:\s*(\w+)[^,]*,[^,]*?,\s*(\d+)x(\d+)(?:.*?)((?:, )?\d+(?:\.\d+)?\s*kb/s)?"
    r"(.*?(\d+(?:\.\d+)?)\s*fps)?", re.I)
_RE_STREAM_AUDIO = re.compile(
    r"Stream .*?:\s*Audio:\s*(\w+)[^,]*,\s*(\d+)\s*Hz,\s*([^,]+)(?:.*?(\d+)\s*kb/s)?", re.I)
_RE_DAR = re.compile(r"DAR\s+([0-9]+:[0-9]+)")

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300  # seconds — probing live streams is expensive


async def probe_media(target: str, *, is_url: bool) -> dict:
    """Return {'duration_s', 'overall_kbps', 'video': {...}, 'audio': [...]}
    or {'error': '...'}. Cached a few minutes per target."""
    key = f"{target}|{is_url}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    # local TS-family files: instant pure-python probe (no subprocess); also
    # immune to broken TS demuxers in exotic static ffmpeg builds
    if not is_url:
        from .tsparser import probe_ts
        from pathlib import Path as _P
        if _P(target).suffix.lower() in (".ts", ".m2ts", ".mts"):
            try:
                out = probe_ts(target)
                if "error" not in out:
                    _CACHE[key] = (time.time(), out)
                    return out
            except Exception:  # noqa: BLE001 - fall through to ffmpeg
                pass

    args = [FFMPEG_BIN, "-hide_banner"]
    if is_url:
        # generous read timeout but a hard wall-clock cap below
        args += ["-rw_timeout", "6000000", "-reconnect", "1"]
        args += ["-analyzeduration", "2500000", "-probesize", "2500000"]
    args += ["-i", target, "-t", "1", "-f", "null", "-"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            out = {"error": "probe timed out (>10s)"}
            _CACHE[key] = (time.time(), out)
            return out
    except FileNotFoundError:
        return {"error": "ffmpeg binary not found"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    text = err.decode("utf-8", errors="replace")
    # only the INPUT section describes the source; "Output #0, null" lines
    # (decode sinks like wrapped_avframe) would confuse the codec parse
    for stop in ("\nOutput #", "\nStream mapping:"):
        i = text.find(stop)
        if i != -1:
            text = text[:i]
    out: dict = {"video": None, "audio": []}
    m = _RE_DURATION.search(text)
    if m:
        h, mnt, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
        out["duration_s"] = round(h * 3600 + mnt * 60 + sec, 1)
        if m.group(4):
            out["overall_kbps"] = int(m.group(4))
    for m in _RE_STREAM_VIDEO.finditer(text):
        width, height = int(m.group(2)), int(m.group(3))
        head = text[m.start():m.start() + 260]
        dar = _RE_DAR.search(head)
        kb = re.search(r"(\d+(?:\.\d+)?)\s*kb/s", head)
        v = {"codec": m.group(1), "width": width, "height": height,
             "ratio": dar.group(1) if dar else None,
             "fps": float(m.group(6)) if m.group(6) else None,
             "kbps": int(float(kb.group(1))) if kb else None}
        out["video"] = v
    for m in _RE_STREAM_AUDIO.finditer(text):
        out["audio"].append({"codec": m.group(1),
                             "rate_hz": int(m.group(2)),
                             "channels": m.group(3).strip(),
                             "kbps": int(m.group(4)) if m.group(4) else None})
    if not out["video"] and not out["audio"] and "duration_s" not in out:
        out = {"error": "no stream info parsed — unreachable or unsupported input"}
        log.warning("probe: unparsed ffmpeg output probably; first lines: %s",
                    "\n".join(text.splitlines()[-3:]))
    _CACHE[key] = (time.time(), out)
    return out
