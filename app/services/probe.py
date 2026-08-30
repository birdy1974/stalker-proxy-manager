"""
Media probing for the detail popups (GUI round 3): run the bundled ffmpeg in
analyze mode against a local file or a network stream and extract codec /
resolution / aspect ratio / bitrate / fps / audio info from its stderr — the
same mechanism ffprobe would use, but with the single ffmpeg binary we ship.
"""

from __future__ import annotations

import asyncio
import os
import re
import time

from ..config import FFMPEG_BIN, log
from ..portal.client import MAG_UA, is_hls
from .ffmpeg_templates import HLS_INPUT_OPTS

_RE_DURATION = re.compile(
    r"Duration:\s*(\d+):(\d+):([\d.]+)(?:,.*?bitrate:\s*(\d+)\s*kb/s)?", re.I)
_RE_STREAM_VIDEO = re.compile(
    r"Stream .*?:\s*Video:\s*(\w+)[^,]*,[^,]*?,\s*(\d+)x(\d+)(?:.*?)((?:, )?\d+(?:\.\d+)?\s*kb/s)?"
    r"(.*?(\d+(?:\.\d+)?)\s*fps)?", re.I)
_RE_STREAM_AUDIO = re.compile(
    r"Stream .*?:\s*Audio:\s*(\w+)[^,]*,\s*(\d+)\s*Hz,\s*([^,]+)(?:.*?(\d+)\s*kb/s)?", re.I)
_RE_DAR = re.compile(r"DAR\s+([0-9]+:[0-9]+)")
_RE_STREAM_SUBTITLE = re.compile(
    r"Stream\s+#\d+:(\d+)(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*Subtitle:\s*([A-Za-z0-9_]+)", re.I)

# Wall-clock cap on a single probe. Network streams can be slow to connect and
# to deliver enough data for analysis, so 10s was far too tight; 30s is the
# default (override with SPM_PROBE_TIMEOUT). Failures are NOT cached, so a
# retry after a slow first attempt actually runs again.
PROBE_TIMEOUT = float(os.environ.get("SPM_PROBE_TIMEOUT", "30"))

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300  # seconds — probing live streams is expensive


def _probe_args(target: str, *, is_url: bool) -> list[str]:
    """ffmpeg argv for one probe. Network streams get the MAG identity and the
    per-input options of the real pipeline (same rules, so the probe reflects
    what the stream path sees - a probe that cannot open an HLS playlist would
    report "no metadata" for a channel that plays fine)."""
    args = [FFMPEG_BIN, "-hide_banner", "-nostdin"]
    if is_url:
        # generous read timeout but a hard wall-clock cap in probe_media
        args += ["-rw_timeout", "15000000", "-reconnect", "1"]
        args += ["-analyzeduration", "2500000", "-probesize", "2500000"]
        # Impersonate the MAG box exactly like the real pipeline does
        # (stream_manager._network_input_options): panels/CDNs refuse or stall
        # the default Lavf user-agent, which used to make the probe time out
        # even though the stream itself was fine.
        args += ["-user_agent", MAG_UA]
        origin = target.split("://", 1)[-1].split("/", 1)[0]
        args += ["-referer", f"{target.split('://', 1)[0]}://{origin}/"]
        if is_hls(target):
            args += HLS_INPUT_OPTS
    args += ["-i", target, "-t", "1", "-f", "null", "-"]
    return args


async def probe_duration(path: str) -> float | None:
    """Fast duration-only probe of a local file (no decode, no network flags).

    `ffmpeg -i <file>` prints `Duration: HH:MM:SS.xx` on stderr and exits.
    Used to fill `local_files.duration_s` so the M3U can advertise a real
    `#EXTINF:` instead of live `-1`.
    """
    if not path or not os.path.isfile(path):
        return None
    args = [FFMPEG_BIN, "-hide_banner", "-nostdin", "-i", path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None
    except Exception:  # noqa: BLE001
        return None
    text = (err or b"").decode("utf-8", errors="replace")
    m = _RE_DURATION.search(text)
    if not m:
        return None
    h, mnt, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
    dur = h * 3600 + mnt * 60 + sec
    return dur if dur > 0 else None


async def probe_media(target: str, *, is_url: bool) -> dict:
    """Return {'duration_s', 'overall_kbps', 'video': {...}, 'audio': [...]}
    or {'error': '...'}. Cached a few minutes per target (successes only)."""
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

    args = _probe_args(target, is_url=is_url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"error": f"probe timed out (>{PROBE_TIMEOUT:.0f}s)"}
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
    out["subtitles"] = [{"index": int(m.group(1)), "codec": m.group(2).lower()}
                        for m in _RE_STREAM_SUBTITLE.finditer(text)]
    if not out["video"] and not out["audio"] and "duration_s" not in out:
        out = {"error": "no stream info parsed — unreachable or unsupported input"}
        log.warning("probe: unparsed ffmpeg output probably; first lines: %s",
                    "\n".join(text.splitlines()[-3:]))
    _CACHE[key] = (time.time(), out)
    return out


# --------------------------------------------------------------------------
# Subtitle-track detection for the dvb/burn template modes.
#
# Unlike probe_media (GUI detail popups, generous timeout) this runs in the
# STREAM START path: a dvb/burn template asks "does this source carry a
# subtitle track we can keep?" before ffmpeg is spawned, and the answer must
# come back fast (8 s cap) and cached (10 min). Returns [{'index', 'codec'}]
# ([] = no subtitle track) or None when the probe itself failed - the caller
# treats None as "no subtitles" so a slow portal can never wedge a play.
# --------------------------------------------------------------------------
SUBS_PROBE_TIMEOUT = float(os.environ.get("SPM_SUBS_PROBE_TIMEOUT", "8"))
_SUBS_CACHE: dict[str, tuple[float, list | None]] = {}
_SUBS_CACHE_TTL = 600.0


async def subtitle_streams(target: str, *, is_url: bool) -> list[dict] | None:
    key = f"{target}|{is_url}"
    hit = _SUBS_CACHE.get(key)
    if hit and time.time() - hit[0] < _SUBS_CACHE_TTL:
        return hit[1]
    args = _probe_args(target, is_url=is_url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(),
                                            timeout=SUBS_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:  # noqa: PERF203
                pass
            return None
    except Exception:  # noqa: BLE001
        return None
    text = err.decode("utf-8", errors="replace")
    i = text.find("\nOutput #")     # only the input section describes the source
    if i != -1:
        text = text[:i]
    # The bundled static ffmpeg build can die inside its (flaky) TS demuxer
    # AFTER printing the stream list - parse whatever was printed.
    subs = [{"index": int(m.group(1)), "codec": m.group(2).lower()}
            for m in _RE_STREAM_SUBTITLE.finditer(text)]
    if not subs and proc.returncode not in (0, None):
        # nothing parsed AND ffmpeg unhappy: report failure, not "no subs",
        # so the caller logs the difference
        _SUBS_CACHE[key] = (time.time(), None)
        return None
    _SUBS_CACHE[key] = (time.time(), subs)
    return subs
