"""
ffmpeg template engine - the two-way sync core.

The GUI edits EITHER structured fields OR the full command text; both stay in
sync at all times (spec requirement):
  * fields -> command : build_command(opts)     (deterministic renderer)
  * command -> fields : parse_command(cmd)      (tolerant parser; unknown flags
                        are preserved in extra_input / extra_output so nothing
                        the user typed is ever lost)

The DS918+-validated command from the spec is exactly reproducible here (see
tests/test_ffmpeg_templates.py):
  ffmpeg -rw_timeout 10000000 -reconnect 1 -reconnect_at_eof 1 ... -i <url>
         -vf scale_vaapi=w=1280:h=720:format=nv12,fps=25 ... pipe:1

The command text contains the literal placeholder `<url>`; the stream manager
substitutes the real input before spawning.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field, asdict

# Canonical (16:9) pixel sizes per resolution label.
RESOLUTIONS = {
    "360p": (640, 360), "480p": (854, 480), "576p": (1024, 576),
    "720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}
ASPECTS = {"4:3": 4 / 3, "16:9": 16 / 9, "21:9": 21 / 9}
VIDEO_CODECS = ["copy", "libx264", "libx265", "h264_vaapi", "hevc_vaapi", "h264_qsv", "hevc_qsv"]
AUDIO_CODECS = ["copy", "aac", "ac3", "mp3", "none"]
HW_CHOICES = ["none", "vaapi", "qsv"]
URL_PLACEHOLDER = "<url>"


def target_size(resolution: str, aspect: str) -> tuple[int, int] | None:
    """Pixel (w, h) for a resolution+aspect; None = keep source size."""
    if resolution == "source" or resolution not in RESOLUTIONS:
        return None
    _, h = RESOLUTIONS[resolution]
    a = ASPECTS.get(aspect, 16 / 9)
    w = int(round(h * a / 2) * 2)   # even width (encoders require it)
    return w, h


@dataclass
class FFmpegOptions:
    """Structured state behind one template (mirrors ffmpeg_templates table)."""
    hw_accel: str = "vaapi"                 # none | vaapi | qsv
    device: str = "/dev/dri/renderD128"     # DS918+ render node
    resolution: str = "720p"
    aspect: str = "16:9"
    video_codec: str = "h264_vaapi"
    video_bitrate: str = "1000k"
    maxrate: str = "1200k"
    bufsize: str = "1800k"
    fps: str = "25"
    gop: str = "50"
    profile: str = "high"
    level: str = "4.1"
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"
    audio_channels: str = "2"
    audio_rate: str = "48000"
    output_format: str = "mpegts"           # mpegts | hls
    extra_input: str = ""                   # appended after built-in input flags
    extra_output: str = ""                  # appended before -f <format>


# --------------------------------------------------------------------------- #
#  fields -> command
# --------------------------------------------------------------------------- #
def build_command(opts: FFmpegOptions, ffmpeg_bin: str = "ffmpeg") -> str:
    """Render the complete ffmpeg command (with <url> placeholder)."""
    c: list[str] = [ffmpeg_bin]

    # ---- resilient input flags (portal streams drop/stall all the time) ----
    c += ["-rw_timeout", "10000000", "-reconnect", "1", "-reconnect_at_eof", "1",
          "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
          "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"]

    transcode = opts.video_codec != "copy"
    # ---- hardware init (only needed when actually transcoding) -------------
    if transcode and opts.hw_accel == "vaapi":
        c += ["-init_hw_device", f"vaapi=intel:{opts.device}", "-hwaccel", "vaapi",
              "-hwaccel_device", "intel", "-hwaccel_output_format", "vaapi"]
    elif transcode and opts.hw_accel == "qsv":
        c += ["-init_hw_device", f"qsv=hw:{opts.device}", "-hwaccel", "qsv",
              "-hwaccel_device", "hw", "-hwaccel_output_format", "qsv"]

    if opts.extra_input.strip():
        c += shlex.split(opts.extra_input)

    c += ["-i", URL_PLACEHOLDER]

    # ---- video filter chain ------------------------------------------------
    if transcode:
        size = target_size(opts.resolution, opts.aspect)
        filters: list[str] = []
        if opts.hw_accel == "vaapi":
            if size:
                filters.append(f"scale_vaapi=w={size[0]}:h={size[1]}:format=nv12")
            else:
                filters.append("scale_vaapi=format=nv12")
            if opts.fps:
                filters.append(f"fps={opts.fps}")
        elif opts.hw_accel == "qsv":
            if size:
                filters.append(f"scale_qsv=w={size[0]}:h={size[1]}:format=nv12")
            if opts.fps:
                filters.append(f"fps={opts.fps}")
        else:  # software
            if size:
                filters.append(f"scale={size[0]}:{size[1]}")
            if opts.fps:
                filters.append(f"fps={opts.fps}")
            filters.append("format=yuv420p")
        filters.append("setsar=1")      # neutral SAR honours the aspect choice
        c += ["-vf", ",".join(filters)]

    # ---- stream mapping (first video + best audio, drop subs/data) ---------
    c += ["-map", "0:v:0"]
    c += ["-map", "0:a:0?"] if opts.audio_codec != "none" else ["-an"]
    c += ["-sn", "-dn"]

    # ---- video encoder ------------------------------------------------------
    c += ["-c:v", opts.video_codec]
    if transcode:
        if opts.video_bitrate:
            c += ["-b:v", opts.video_bitrate]
        if opts.maxrate:
            c += ["-maxrate", opts.maxrate]
        if opts.bufsize:
            c += ["-bufsize", opts.bufsize]
        # profile/level only make sense for h.264-family encoders
        if opts.video_codec in ("libx264", "h264_vaapi", "h264_qsv"):
            if opts.profile:
                c += ["-profile:v", opts.profile]
            if opts.level:
                c += ["-level", opts.level]
        if opts.gop:
            c += ["-g", opts.gop]
        if opts.fps:
            c += ["-r", opts.fps]

    # ---- audio encoder ------------------------------------------------------
    if opts.audio_codec != "none":
        c += ["-c:a", opts.audio_codec]
        if opts.audio_codec != "copy":
            if opts.audio_bitrate:
                c += ["-b:a", opts.audio_bitrate]
            if opts.audio_channels:
                c += ["-ac", opts.audio_channels]
            if opts.audio_rate:
                c += ["-ar", opts.audio_rate]

    if opts.extra_output.strip():
        c += shlex.split(opts.extra_output)

    # ---- output -------------------------------------------------------------
    if opts.output_format == "hls":
        c += ["-f", "hls", "-hls_time", "6", "-hls_list_size", "6",
              "-hls_flags", "delete_segments+append_list", "<out_dir>/index.m3u8"]
    else:
        c += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]
    return " ".join(c)


# --------------------------------------------------------------------------- #
#  command -> fields   (tolerant; keeps unknown flags)
# --------------------------------------------------------------------------- #
_KNOWN_WITH_VALUE = {
    "-b:v": "video_bitrate", "-maxrate": "maxrate", "-bufsize": "bufsize",
    "-g": "gop", "-r": "fps", "-profile:v": "profile", "-level": "level",
    "-b:a": "audio_bitrate", "-ac": "audio_channels", "-ar": "audio_rate",
}


def parse_command(cmd: str) -> dict:
    """
    Parse a full ffmpeg command back into structured options.
    Returns {"options": {...}, "warnings": [..]} - never raises.
    """
    opts = FFmpegOptions()
    warnings: list[str] = []
    try:
        toks = shlex.split(cmd)
    except ValueError as exc:
        return {"options": asdict(opts), "warnings": [f"unbalanced quotes: {exc}"]}

    i = 1 if toks and ("ffmpeg" in toks[0]) else 0
    input_side = True
    unhandled_in: list[str] = []
    unhandled_out: list[str] = []
    while i < len(toks):
        t = toks[i]
        if t == "-i":
            input_side = False
            i += 2                     # skip the url token itself
            continue
        if input_side:
            if t == "-init_hw_device" and i + 1 < len(toks):
                spec = toks[i + 1]
                if spec.startswith("vaapi"):
                    opts.hw_accel = "vaapi"
                    if ":" in spec:
                        opts.device = spec.split(":", 1)[1]
                elif spec.startswith("qsv"):
                    opts.hw_accel = "qsv"
                    if ":" in spec:
                        opts.device = spec.split(":", 1)[1].replace("hw:", "") or opts.device
                i += 2
                continue
            if t in ("-hwaccel", "-hwaccel_device", "-hwaccel_output_format",
                     "-rw_timeout", "-reconnect_delay_max", "-fflags", "-err_detect"):
                i += 2
                continue
            if t.startswith("-reconnect"):
                i += 1
                continue
            unhandled_in.append(t)
            i += 1
            continue

        # -------- output side --------
        if t == "-vf" and i + 1 < len(toks):
            vf = toks[i + 1]
            m = None
            for scale in ("scale_vaapi", "scale_qsv", "scale"):
                if scale in vf:
                    opts.hw_accel = {"scale_vaapi": "vaapi", "scale_qsv": "qsv"}.get(scale, opts.hw_accel)
                    m = True
                    import re as _re
                    wh = _re.search(rf"{scale}=(?:w=)?(\d+)(?::h=|x)(\d+)", vf)
                    if wh:
                        w, h = int(wh.group(1)), int(wh.group(2))
                        opts.resolution = next((k for k, (_, vh) in RESOLUTIONS.items() if vh == h), "source")
                        opts.aspect = next((k for k, a in ASPECTS.items() if abs(w / h - a) < 0.05), "16:9")
            fm = __import__("re").search(r"fps=(\d+)", vf)
            if fm:
                opts.fps = fm.group(1)
            if m is None and "format" not in vf:
                warnings.append(f"unrecognised video filter kept as-is: {vf}")
                unhandled_out += ["-vf", vf]
            i += 2
            continue
        if t == "-map":
            i += 2
            continue
        if t == "-an":
            opts.audio_codec = "none"
            i += 1
            continue
        if t in ("-sn", "-dn"):
            i += 1
            continue
        if t == "-c:v" and i + 1 < len(toks):
            opts.video_codec = toks[i + 1]
            if opts.video_codec == "copy":
                opts.hw_accel = "none"
            i += 2
            continue
        if t == "-c:a" and i + 1 < len(toks):
            opts.audio_codec = toks[i + 1]
            i += 2
            continue
        if t in _KNOWN_WITH_VALUE and i + 1 < len(toks):
            setattr(opts, _KNOWN_WITH_VALUE[t], toks[i + 1])
            i += 2
            continue
        if t == "-f" and i + 1 < len(toks):
            opts.output_format = toks[i + 1] if toks[i + 1] in ("mpegts", "hls") else "mpegts"
            i += 2
            continue
        if t == "-preset" and i + 1 < len(toks):
            i += 2
            continue
        unhandled_out.append(t)
        i += 1

    opts.extra_input = " ".join(x for x in unhandled_in if x not in (URL_PLACEHOLDER,))
    opts.extra_output = " ".join(x for x in unhandled_out if x not in ("pipe:1",))
    return {"options": asdict(opts), "warnings": warnings}


# --------------------------------------------------------------------------- #
#  Presets inserted at first boot
# --------------------------------------------------------------------------- #
def default_presets() -> list[dict]:
    mk = lambda name, **kw: {**asdict(FFmpegOptions(**kw)), "name": name}   # noqa: E731
    presets = [
        mk("VAAPI 720p ~1M (DS918+ reference)"),
        mk("VAAPI 1080p ~2.5M", resolution="1080p", video_bitrate="2500k",
           maxrate="3000k", bufsize="4500k"),
        mk("QSV 720p ~1M", hw_accel="qsv", video_codec="h264_qsv"),
        mk("Software 720p ~1.2M (libx264)", hw_accel="none", video_codec="libx264",
           video_bitrate="1200k", maxrate="1500k", bufsize="2200k"),
        mk("Copy / passthrough (no transcode)", hw_accel="none", video_codec="copy",
           audio_codec="copy", resolution="source"),
    ]
    for p in presets:
        opts = {k: v for k, v in p.items() if k != "name"}
        p["command"] = build_command(FFmpegOptions(**opts))
        p["command_source"] = "fields"
        p["enabled"] = True
    return presets
