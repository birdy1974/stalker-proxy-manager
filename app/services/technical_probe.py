"""Detailed, bounded ffprobe inspection for the admin preview player.

Only stored source/playlist IDs are resolved by the API; this service does not
accept an arbitrary browser-provided URL. No TMDB or playback-template changes.
"""

import asyncio
import json
import time

from ..config import FFPROBE_BIN
from . import probe, stream_identity


def summarize(data: dict) -> dict:
    streams = data.get("streams") or []
    fmt = dict(data.get("format") or {})
    fmt.pop("filename", None)  # may contain a provider URL, credentials or token

    def number(value):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def rate(value):
        try:
            a, b = str(value).split("/")
            return round(float(a) / float(b), 3) if float(b) else None
        except (ValueError, TypeError):
            return number(value)

    videos, audio, subtitles = [], [], []
    for stream in streams:
        common = {
            "index": stream.get("index"),
            "codec": stream.get("codec_name"),
            "profile": stream.get("profile"),
            "language": (stream.get("tags") or {}).get("language"),
            "kbps": (
                number(stream.get("bit_rate")) / 1000
                if number(stream.get("bit_rate"))
                else None
            ),
        }
        kind = stream.get("codec_type")
        if kind == "video":
            videos.append(
                {
                    **common,
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "ratio": stream.get("display_aspect_ratio"),
                    "fps": rate(stream.get("avg_frame_rate"))
                    or rate(stream.get("r_frame_rate")),
                    "pixel_format": stream.get("pix_fmt"),
                    "level": stream.get("level"),
                    "field_order": stream.get("field_order"),
                    "color_space": stream.get("color_space"),
                    "color_transfer": stream.get("color_transfer"),
                    "color_primaries": stream.get("color_primaries"),
                    "bits_per_raw_sample": stream.get("bits_per_raw_sample"),
                }
            )
        elif kind == "audio":
            audio.append(
                {
                    **common,
                    "rate_hz": number(stream.get("sample_rate")),
                    "channels": stream.get("channels"),
                    "channel_layout": stream.get("channel_layout"),
                    "sample_format": stream.get("sample_fmt"),
                    "bits_per_sample": stream.get("bits_per_sample"),
                }
            )
        elif kind == "subtitle":
            subtitles.append(common)
    return {
        "method": "ffprobe",
        "container": fmt.get("format_name"),
        "duration_s": number(fmt.get("duration")),
        "overall_kbps": (
            number(fmt.get("bit_rate")) / 1000 if number(fmt.get("bit_rate")) else None
        ),
        "video": videos[0] if videos else None,
        "videos": videos,
        "audio": audio,
        "subtitles": subtitles,
        "technical": {
            "format": fmt,
            "streams": streams,
            "programs": data.get("programs") or [],
            "chapters": data.get("chapters") or [],
        },
    }


async def _run(args):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        return (*await proc.communicate(), proc.returncode)
    finally:
        # Also reap the child on timeout/cancellation, not merely send SIGKILL.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


async def probe_technical(target: str, *, is_url: bool) -> dict:
    """Fresh probe, never a stale cache hit. Missing ffprobe uses the summary probe."""
    deadline = time.monotonic() + probe.PROBE_TIMEOUT
    for ua in (stream_identity.ladder(target) if is_url else [None]):
        basic = probe._probe_args(target, is_url=is_url, user_agent=ua)
        inputs = basic[3 : basic.index("-i")]  # strip ffmpeg's banner/stdin switches
        args = [
            FFPROBE_BIN,
            "-v",
            "error",
            *inputs,
            "-show_format",
            "-show_streams",
            "-show_programs",
            "-show_chapters",
            "-of",
            "json",
            "-i",
            target,
        ]
        start = time.monotonic()
        try:
            stdout, stderr, code = await asyncio.wait_for(
                _run(args), timeout=max(0.1, deadline - start)
            )
        except FileNotFoundError:
            probe._CACHE.pop(f"{target}|{is_url}", None)
            try:
                result = dict(
                    await asyncio.wait_for(
                        probe.probe_media(target, is_url=is_url),
                        timeout=max(0.1, deadline - time.monotonic()),
                    )
                )
            except asyncio.TimeoutError:
                return {"error": "Stream probe timed out. Try again."}
            result.update(
                method="ffmpeg summary",
                notice="ffprobe is unavailable; only the available FFmpeg summary is shown.",
            )
            return result
        except asyncio.TimeoutError:
            return {
                "error": f"Stream probe timed out after {probe.PROBE_TIMEOUT:g} seconds. Try again."
            }
        except OSError:
            return {
                "error": "Could not start ffprobe. Check the configured binary and permissions."
            }
        try:
            data = json.loads(stdout)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get("streams"):
            if is_url and ua:
                stream_identity.remember(target, ua)
            return summarize(data)
        if (
            not is_url
            or stream_identity.http_open_error(
                code, stderr.decode("utf-8", "replace"), time.monotonic() - start
            )
            is None
        ):
            break
        if time.monotonic() >= deadline:
            break
    return {
        "error": "No technical information returned. The stream may be unavailable, busy, or unsupported. Try again after stopping other playback."
    }
