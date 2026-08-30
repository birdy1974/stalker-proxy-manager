"""
ffmpeg template engine - the two-way sync core.

The GUI edits EITHER structured fields OR the full command text; both stay in
sync at all times (spec requirement):
  * fields -> command : build_command(opts)     (deterministic renderer)
  * command -> fields : parse_command(cmd)      (tolerant parser; unknown flags
                        are preserved in extra_input / extra_output so nothing
                        the user typed is ever lost)

The DS918+-validated command from the spec is exactly reproducible here (see
tests/test_ffmpeg_templates.py). The VAAPI presets are tuned for the Intel iHD
driver on Apollo Lake (J3455):

  * `-low_power 1` selects the fixed-function H.264 encoder
    (VAEntrypointEncSliceLP) - faster and far cheaper than the EU path, and
    it is exactly the entrypoint the iHD driver advertises for H.264 on the
    DS918+ (see vainfo below).
  * `-rc_mode CQP|VBR|CBR|...` makes rate control explicit (ffmpeg's VAAPI
    encoder aliases are uppercase). VAAPI's "AUTO" mode is undocumented and
    driver-dependent; without an explicit mode `-b:v`/`-maxrate` are not
    guaranteed to be honoured the same way across driver versions. The shipped
    templates ask for **CQP** - constant quantiser, so picture quality is
    pinned and the bitrate floats with the content - and a QP only means
    something beside `-global_quality`, which is emitted in that mode.
  * `-async_depth 4` keeps more frames in flight, raising throughput and
    cutting time-to-first-frame.

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
AUDIO_CODECS = ["copy", "aac", "ac3", "mp2", "mp3", "none"]
HW_CHOICES = ["none", "vaapi", "qsv"]
# VAAPI rate-control modes (h264_vaapi/hevc_vaapi). ffmpeg's encoder aliases
# are uppercase (see `ffmpeg -h encoder=h264_vaapi`). "AUTO" = let the driver
# choose; everything else is passed through as-is so the command stays honest.
RC_MODES = ["AUTO", "CQP", "CBR", "VBR", "ICQ", "QVBR", "AVBR"]
# VAAPI encoders that understand -low_power/-rc_mode/-async_depth.
VAAPI_ENCODERS = ("h264_vaapi", "hevc_vaapi", "vp8_vaapi", "vp9_vaapi")
# The shipped reference preset (kept as a built-in, but NOT the fallback
# default - that role belongs to the redirect preset now).
REFERENCE_PRESET_NAME = "VAAPI 720p ~1M (DS918+ reference)"
# The "redirect" preset is NOT an ffmpeg command: assigning it to a channel
# makes the stream path 302 the player straight to the panel's CDN (the old
# global proxy/redirect switch, moved into a per-channel template). It is ALSO
# the default template: an item without an explicit template assignment is
# redirected instead of being proxied through ffmpeg.
REDIRECT_PRESET_NAME = "Redirect (bypass ffmpeg)"
REDIRECT_COMMAND = "@redirect"
COPY_PRESET_NAME = "Copy / passthrough (no transcode)"
URL_PLACEHOLDER = "<url>"

# Input options an HLS *playlist* needs and a plain stream does not. ffmpeg
# refuses a .m3u8 whose segments are reached over a protocol outside the
# whitelist ("Protocol not on whitelist") and rejects the fMP4/init segments
# that Ministra panels like to reference ("EXT-X-MAP ... not allowed"), so a
# perfectly valid portal link dies before a single byte is read. Added by
# StreamManager only when the resolved link actually is a playlist, and never
# over a user's own flag (an explicit -protocol_whitelist in the template
# always wins).
HLS_PROTOCOL_WHITELIST = "file,http,https,tcp,tls,crypto"
HLS_ALLOWED_EXTENSIONS = "ALL"
HLS_INPUT_OPTS = ["-protocol_whitelist", HLS_PROTOCOL_WHITELIST,
                  "-allowed_extensions", HLS_ALLOWED_EXTENSIONS]


def serves_original_file(command: str | None) -> bool:
    """True when a local file should be sent as-is (no ffmpeg).

    Redirect is a CDN 302 for portal streams; on disk that means 'give the
    player the original container'. Copy/passthrough is the same idea without
    remuxing to MPEG-TS. Anything that actually transcodes still goes through
    ffmpeg.
    """
    cmd = (command or "").strip()
    if not cmd or cmd == REDIRECT_COMMAND:
        return True
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    vcodec = acodec = None
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t in ("-c:v", "-vcodec") and nxt:
            vcodec = nxt
            i += 2
            continue
        if t in ("-c:a", "-acodec") and nxt:
            acodec = nxt
            i += 2
            continue
        if t in ("-c", "-codec") and nxt:
            vcodec = acodec = nxt
            i += 2
            continue
        if t == "-an":
            acodec = "none"
            i += 1
            continue
        i += 1
    if vcodec is None:
        return False
    return vcodec == "copy" and (acodec or "copy") in ("copy", "none")


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
    # Bitrate numbers are tuned for EXTERNAL (internet) streaming, not LAN: the
    # encoder is kept near a constant rate (maxrate ~= bitrate + 10% so spikes
    # cannot underrun a weak download link) and carries a ~2-second VBV buffer
    # (bufsize = 2x bitrate) so brief congestion is absorbed instead of stalling
    # the player. See default_presets() for the per-resolution values.
    # They are the rate-driven knobs, so build_command() renders them for
    # VBR/CBR/... and NOT for -rc_mode CQP: a constant-QP encoder sets its own
    # bitrate from the quantiser and would ignore them. They stay in the
    # template's fields, because flipping the mode back restores the tuning.
    video_bitrate: str = "1000k"
    maxrate: str = "1100k"
    bufsize: str = "2000k"
    fps: str = "25"
    gop: str = "50"
    profile: str = "high"
    level: str = "4.1"
    low_power: bool = True           # h264_vaapi: use EncSliceLP (fixed-function)
    rc_mode: str = "CQP"             # VAAPI rate control: AUTO|CQP|CBR|VBR|ICQ|QVBR|AVBR
    # QP for -rc_mode CQP (0-51, lower = better quality and more bits). Emitted
    # only in CQP, because a constant-QP mode without a QP has no target at all
    # and leaves the number to the driver. "" or "AUTO" = skip the flag on purpose.
    global_quality: str = "26"
    async_depth: str = "4"           # VAAPI frames in flight (throughput / startup)
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

    # ---- stream mapping (first video + optional audio + optional DVB subs)
    c += ["-map", "0:v:0"]
    c += ["-map", "0:a:0?"] if opts.audio_codec != "none" else ["-an"]
    c += ["-map", "0:s?"]
    c += ["-dn"]

    # ---- video encoder ------------------------------------------------------
    c += ["-c:v", opts.video_codec]
    rc = (opts.rc_mode or "").upper()
    # -rc_mode is a VAAPI tuning: libx264 and the QSV encoder do not know the
    # flag at all, so for them the bitrate knobs stay in charge whatever the
    # template's rate-control field says. Only where the mode is honoured can it
    # make the rate flags redundant.
    cqp = transcode and rc == "CQP" and opts.video_codec in VAAPI_ENCODERS
    if transcode:
        # CQP pins the quantiser instead of the rate: -b:v/-maxrate/-bufsize are
        # not honoured in that mode, so they are not rendered either. This text
        # is what the GUI shows and what the user pastes into a shell, and a
        # command carrying flags the encoder ignores is a command that lies.
        if not cqp and opts.video_bitrate:
            c += ["-b:v", opts.video_bitrate]
        if not cqp and opts.maxrate:
            c += ["-maxrate", opts.maxrate]
        if not cqp and opts.bufsize:
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
        # VAAPI tuning (Intel iHD driver; DS918+ Apollo Lake). low_power is
        # emitted only for h264_vaapi: on this silicon H.264 is the only codec
        # with a fixed-function LP entrypoint (hevc_vaapi -low_power would fail).
        if opts.video_codec in VAAPI_ENCODERS:
            if opts.video_codec == "h264_vaapi" and opts.low_power:
                c += ["-low_power", "1"]
            if rc and rc != "AUTO":
                c += ["-rc_mode", rc]
            if cqp and str(opts.global_quality or "") not in ("", "AUTO"):
                c += ["-global_quality", str(opts.global_quality)]
            if opts.async_depth:
                c += ["-async_depth", opts.async_depth]

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

    c += ["-c:s", "dvbsub"]

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
    # both spellings of the QP: the generic codec option (what build_command
    # renders) and the per-stream alias a user pastes in. One GUI field.
    "-global_quality": "global_quality", "-q:v": "global_quality",
    "-g": "gop", "-r": "fps", "-profile:v": "profile", "-level": "level",
    "-b:a": "audio_bitrate", "-ac": "audio_channels", "-ar": "audio_rate",
}

# numeric rc_mode spellings (ffmpeg -h encoder=h264_vaapi) -> names
_RC_NUM = {"0": "AUTO", "1": "CQP", "2": "CBR", "3": "VBR",
           "4": "ICQ", "5": "QVBR", "6": "AVBR"}


def _rc_name(value: str) -> str:
    """Normalise a -rc_mode argument (accepts 'VBR', 'vbr', and its int '3')."""
    v = value.strip()
    if v in _RC_NUM:
        return _RC_NUM[v]
    up = v.upper()
    return up if up in RC_MODES else (up or "AUTO")


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
        if t == "-c:s" and i + 1 < len(toks):
            i += 2
            continue
        if t == "-c:v" and i + 1 < len(toks):
            opts.video_codec = toks[i + 1]
            if opts.video_codec == "copy":
                opts.hw_accel = "none"
            i += 2
            continue
        if t == "-low_power" and i + 1 < len(toks):
            opts.low_power = toks[i + 1] not in ("0", "false", "off", "no")
            i += 2
            continue
        if t == "-rc_mode" and i + 1 < len(toks):
            opts.rc_mode = _rc_name(toks[i + 1])
            i += 2
            continue
        if t == "-async_depth" and i + 1 < len(toks):
            opts.async_depth = toks[i + 1]
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
    # Rate control for external (internet) streaming: maxrate stays ~10% above
    # the target so bitrate spikes cannot underrun a weak viewer link, while
    # bufsize = 2x bitrate (~2 seconds of VBV) absorbs short-lived congestion
    # instead of freezing the player. The VAAPI presets ship on CQP (a fixed QP:
    # quality never dips on a hard scene, whatever the network does) with those
    # bitrate numbers still filled in - flip the template to VBR or CBR and the
    # tuning above is what you get back, no retyping.
    mk = lambda name, **kw: {**asdict(FFmpegOptions(**kw)), "name": name}   # noqa: E731
    presets = [
        mk(REFERENCE_PRESET_NAME, low_power=True, async_depth="4"),
        mk("VAAPI 1080p ~2.5M", resolution="1080p", video_bitrate="2500k",
           maxrate="2750k", bufsize="5000k", low_power=True, async_depth="4"),
        # No -rc_mode for QSV: h264_qsv drives rate control through its own
        # options (-global_quality/-extbrc) and rejects the VAAPI flag, so
        # build_command keeps this tuning inside VAAPI_ENCODERS. The field says
        # CQP anyway, so the row, the GUI and every other template agree.
        mk("QSV 720p ~1M", hw_accel="qsv", video_codec="h264_qsv"),
        mk("Software 720p ~1.2M (libx264)", hw_accel="none", video_codec="libx264",
           video_bitrate="1200k", maxrate="1300k", bufsize="2400k"),
        mk(COPY_PRESET_NAME, hw_accel="none", video_codec="copy",
           audio_codec="copy", resolution="source"),
        mk("Dreambox DM800se (Enigma2 / MPEG2-SD)", hw_accel="vaapi",
           resolution="576p", aspect="16:9", video_codec="h264_vaapi",
           video_bitrate="1200k", maxrate="1300k", bufsize="2400k",
           fps="25", gop="50", profile="main", level="3.1",
           low_power=True, async_depth="4",
           audio_codec="mp2", audio_bitrate="192k"),
    ]
    for p in presets:
        opts = {k: v for k, v in p.items() if k != "name"}
        p["command"] = build_command(FFmpegOptions(**opts))
        p["command_source"] = "fields"
        p["enabled"] = True
    # The redirect preset is a marker, not an ffmpeg command. It ships with the
    # same structured fields as any other template (so the seeder and the GUI
    # treat it uniformly), but its command is a sentinel that the stream path
    # checks for to bypass ffmpeg entirely.
    presets.append({
        **asdict(FFmpegOptions(video_codec="copy", audio_codec="copy",
                               resolution="source", hw_accel="none")),
        "name": REDIRECT_PRESET_NAME,
        "command": REDIRECT_COMMAND,
        "command_source": "fields",
        "enabled": True,
    })
    return presets
