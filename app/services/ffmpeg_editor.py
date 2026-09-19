"""Editor suggestions and validation, independent of the installed FFmpeg build.

Suggestions are not a claim of hardware support. Raw extras and manual command
mode remain available for build-specific flags. No new template columns needed.
"""
from __future__ import annotations

import math
import re

from .ffmpeg_applicability import applicability_rules, disabled_parameters
import shlex


def spec(help, choices=None, *, custom=True, kind="text", minimum=None, maximum=None):
    return {"help": help, "choices": choices, "custom": custom, "kind": kind,
            "min": minimum, "max": maximum}


FIELDS = {
    "name": spec("A unique, descriptive name shown when assigning this template to a playlist item."),
    "enabled": spec("Make this template available for playback. Disabling it does not delete its settings."),
    "hw_accel": spec("Video decoding path. Match VAAPI/QSV encoders to their hardware path; CPU encoders normally need CPU decoding. Availability depends on your host.", custom=False),
    "device": spec("GPU render node inside the container. It must exist and be accessible. Ignored for CPU decoding or video copy.", ["/dev/dri/renderD128", "/dev/dri/renderD129", "/dev/dri/renderD130"]),
    "resolution": spec("Output size when re-encoding. Choose source to omit resizing; custom values accept an even WIDTHxHEIGHT (16–8192 pixels) or a height such as 900p. Explicit dimensions override Aspect.", ["source", "360p", "480p", "576p", "720p", "1080p", "1440p", "2160p", "4320p"]),
    "aspect": spec("Width:height ratio used to calculate width for a height preset. Pixels are square. Ignored for explicit WIDTHxHEIGHT, source size or video copy.", ["16:9", "4:3", "21:9", "1:1", "9:16", "64:27"]),
    "video_codec": spec("Video encoder, or copy for no re-encoding. Scaling, FPS and quality controls do not apply to copy. Custom encoder names must be available in your FFmpeg build."),
    "video_bitrate": spec("Target video bits/second: 2500k = 2.5 Mbit/s. Blank leaves it to the encoder. Not emitted for VAAPI CQP/ICQ or video copy.", ["", "500k", "750k", "1000k", "1500k", "2500k", "4000k", "6000k", "8000k", "12000k", "20000k"], kind="rate"),
    "maxrate": spec("Peak video bitrate (bits/second). Normally at least the target bitrate; pair with a VBV buffer. Blank omits it. Disabled when the selected VAAPI mode ignores it, or during video copy.", ["", "1100k", "1200k", "1800k", "2750k", "4800k", "8000k", "12000k", "20000k"], kind="rate"),
    "bufsize": spec("VBV buffer capacity in bits, not bytes. About twice the video bitrate represents two seconds of buffering. Blank omits it; ignored in VAAPI CQP/ICQ/AVBR or video copy.", ["", "2000k", "2400k", "4000k", "8000k", "12000k", "24000k", "40000k"], kind="rate"),
    "fps": spec("Output frames per second while re-encoding. Blank keeps source timing. Decimal rates such as 23.976 or 59.94 are supported (greater than 0, at most 1000).", ["", "23.976", "24", "25", "29.97", "30", "50", "59.94", "60", "100", "120"], kind="positive", minimum=0, maximum=1000),
    "gop": spec("Maximum frames between keyframes. At 25 FPS, 50 is roughly two seconds. 0 is intra-only; blank uses the encoder default.", ["", "0", "1", "24", "25", "48", "50", "60", "100", "120", "250"], kind="integer", minimum=0, maximum=2147483647),
    "profile": spec("H.264 compatibility profile. Baseline suits older decoders; main/high improve compression. Blank lets FFmpeg choose. This field is only emitted for H.264 encoders; use the full command for other codecs.", ["", "baseline", "main", "high"]),
    "level": spec("H.264 decoder limits (resolution, rate and bitrate). 4.1 is common for 1080p; larger/faster video may need 5.x/6.x. Blank is automatic. Receiver and encoder support vary.", ["", "3.0", "3.1", "3.2", "4.0", "4.1", "4.2", "5.0", "5.1", "5.2", "6.0", "6.1", "6.2"]),
    "vf_preset": spec("An extra video filter before resizing. Match the filter to the decode path. Bob deinterlacing doubles frame rate; CPU filters add CPU load. For an arbitrary filter graph, edit -vf in the full command.", custom=False),
    "low_power": spec("Use the low-power H.264 VAAPI encoder when enabled. When off, this template omits the flag and FFmpeg chooses. Driver support is required; use full command -low_power 0 to force it off."),
    "rc_mode": spec("VAAPI rate control: CQP fixes quantizer; CBR/VBR target bitrate; ICQ/QVBR/AVBR are driver-dependent. AUTO omits the flag. Does not control CPU/QSV encoders.", custom=False),
    "global_quality": spec("VAAPI quality for CQP, ICQ and QVBR: lower means better quality and larger output. CQP uses 0–51; ICQ uses 1–51. Blank/AUTO lets the encoder choose.", ["", "AUTO", "18", "20", "22", "24", "26", "28", "30", "32", "36", "40", "51"], kind="integer", minimum=0, maximum=51),
    "async_depth": spec("VAAPI frames processed concurrently. 1–64; usually 1–8. Higher values can improve throughput but add latency and require driver support. Blank omits the flag.", ["", "1", "2", "4", "8", "16", "32", "64"], kind="integer", minimum=1, maximum=64),
    "audio_codec": spec("Audio encoder; copy preserves the source and none removes audio. Custom encoders require build/container support (for example libopus in MKV).", ["aac", "ac3", "eac3", "mp2", "libmp3lame", "mp3", "libopus", "flac", "copy", "none"]),
    "audio_bitrate": spec("Target audio bits/second. 128k–192k is common for stereo AAC; surround often needs 384k–640k. Codec limits vary. Blank is automatic; ignored for copy/none and lossless FLAC/ALAC/PCM.", ["", "64k", "96k", "128k", "160k", "192k", "256k", "320k", "384k", "448k", "640k"], kind="rate"),
    "audio_channels": spec("Number of encoded audio channels: 1 mono, 2 stereo, 6 for 5.1, 8 for 7.1. 1–64; codec limits vary. Blank preserves the input layout; ignored for copy/none.", ["", "1", "2", "6", "8"], kind="integer", minimum=1, maximum=64),
    "audio_rate": spec("Encoded audio samples/second (Hz). 48000 is standard for video; 44100 for music. 8000–384000, subject to codec support. Blank keeps the source rate; ignored for copy/none.", ["", "8000", "16000", "22050", "24000", "32000", "44100", "48000", "88200", "96000", "192000"], kind="integer", minimum=8000, maximum=384000),
    "subs": spec("Drop removes subtitles. DVB is for bitmap subtitles in TS. Copy all requires Matroska for text and bitmap tracks. Arbitrary subtitle encoding or burn-in needs the full command.", custom=False),
    "output_format": spec("MPEG-TS for live TV, Matroska for subtitle-capable VOD, or HLS segment files. These are the app-supported output paths; an arbitrary muxer may not be playable by SPM.", custom=False),
    "extra_input": spec("Additional FFmpeg flags before -i. Quote values containing spaces. The helper below can insert/replace common options. Unknown options are allowed; use FFmpeg help for their syntax."),
    "extra_output": spec("Additional flags after -i, before the output muxer. Quote values containing spaces. Mapping, codecs, filters and muxer flags owned by the form must be edited in the full command instead."),
    "command": spec("Complete FFmpeg argument list, not a shell script. Keep the literal <url> input and the supported output target. Editing switches to manual mode, preserving your text exactly; switching back to fields can omit unsupported constructs."),
}


def advanced(flag, side, label, help, choices, kind="text", minimum=None, maximum=None):
    return {"flag": flag, "side": side, "label": label,
            **spec(help, choices, kind=kind, minimum=minimum, maximum=maximum)}


ADVANCED = [
    advanced("-rw_timeout", "input", "Network read timeout (µs)", "Microseconds waiting for network reads. 10000000 = 10 seconds; 0 disables this timeout.", ["0", "5000000", "10000000", "20000000", "30000000", "60000000"], "integer", 0, 2147483647),
    *[advanced(flag, "input", label, help, ["0", "1"], "integer", 0, 1) for flag, label, help in [
        ("-reconnect", "Reconnect on disconnect", "HTTP reconnect after an unexpected disconnect: 0 off, 1 on."),
        ("-reconnect_at_eof", "Reconnect at end of input", "1 treats end-of-file as an error and reconnects. Prefer 0 for finite VOD files."),
        ("-reconnect_streamed", "Reconnect non-seekable input", "1 permits reconnecting streamed/non-seekable HTTP inputs."),
        ("-reconnect_on_network_error", "Reconnect on network error", "1 retries TCP/TLS network errors. Requires FFmpeg support."),
    ]],
    advanced("-reconnect_delay_max", "input", "Maximum reconnect delay (s)", "Longest retry delay in seconds; 0–4294, depending on the HTTP protocol implementation.", ["1", "2", "5", "10", "30", "60"], "integer", 0, 4294),
    advanced("-probesize", "input", "Probe size (bytes)", "Bytes read to identify streams; at least 32. Smaller starts sooner but can miss tracks.", ["32768", "500000", "1000000", "5000000", "10000000"], "integer", 32, 2147483647),
    advanced("-analyzeduration", "input", "Analysis duration (µs)", "Time budget for stream analysis in microseconds. 0 selects FFmpeg's automatic default, not zero analysis.", ["0", "500000", "1000000", "3000000", "5000000"], "integer", 0, 2147483647),
    advanced("-thread_queue_size", "input", "Input packet queue", "Maximum queued packets. More tolerates bursts but uses memory and can add latency.", ["8", "64", "256", "512", "1024"], "integer", 1, 2147483647),
    advanced("-fflags", "input", "Input format flags", "Combine flags with +. nobuffer reduces buffering but can hurt unreliable sources.", ["+genpts+discardcorrupt", "+genpts", "+nobuffer", "+genpts+nobuffer+discardcorrupt"]),
    advanced("-err_detect", "input", "Decoder error handling", "ignore_err continues after errors; careful/compliant/strict are progressively stricter.", ["ignore_err", "careful", "compliant", "strict"]),
    advanced("-user_agent", "input", "HTTP User-Agent", "User-Agent presented to the source. A template override wins over automatic player identities.", ["Lavf/61.7.100", "VLC/3.0.21 LibVLC/3.0.21"]),
    advanced("-referer", "input", "HTTP Referer", "Optional source website URL sent as the HTTP Referer header.", []),
    advanced("-preset", "output", "Encoder speed preset", "CPU x264/x265: faster saves CPU but needs more bitrate. QSV/NVENC use different names; enter a supported custom value.", ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]),
    advanced("-crf", "output", "CPU constant quality (CRF)", "x264/x265 quality target (0–51 offered here); lower is better/larger. Encoder-specific; remove video bitrate/maxrate to avoid mixing rate targets.", ["18", "20", "22", "23", "24", "26", "28", "30"], "number", 0, 51),
    advanced("-tune", "output", "Encoder tuning", "Encoder-specific content/latency tuning. zerolatency reduces buffering; film/animation/grain are common x264 choices.", ["zerolatency", "film", "animation", "grain", "stillimage", "fastdecode"]),
    advanced("-threads", "output", "Encoder threads", "0 is automatic. Larger values use more CPU; support depends on the encoder.", ["0", "1", "2", "4", "8", "16"], "integer", 0, 2147483647),
    advanced("-max_muxing_queue_size", "output", "Muxing packet queue", "Packets buffered while waiting for all output streams. Raising it may resolve queue overflow but uses memory.", ["128", "512", "1024", "4096"], "integer", 1, 2147483647),
    advanced("-muxdelay", "output", "Mux delay (s)", "Maximum mux delay in seconds. For TS, 0 can reduce startup latency. Muxer-specific.", ["0", "0.1", "0.5", "0.7", "1"], "number", 0, 3600),
    advanced("-flush_packets", "output", "Flush output packets", "-1 automatic, 0 buffered, 1 flush after each packet. 1 normally minimizes streaming latency.", ["-1", "0", "1"], "integer", -1, 1),
    advanced("-mpegts_flags", "output", "MPEG-TS flags", "TS muxer flags; +resend_headers regularly includes stream headers for players joining the stream.", ["+resend_headers", "+resend_headers+initial_discontinuity", "+pat_pmt_at_frames"]),
    advanced("-hls_time", "output", "HLS segment duration (s)", "Target segment duration in seconds; actual boundaries follow keyframes. Only applies to HLS.", ["1", "2", "4", "6", "10"], "number", 0.1, 3600),
    advanced("-hls_list_size", "output", "HLS playlist length", "Number of segments in the playlist. 0 keeps all segments (unbounded growth); applies only to HLS.", ["0", "3", "6", "10", "20"], "integer", 0, 2147483647),
    advanced("-hls_flags", "output", "HLS flags", "Combine HLS muxer flags with +. delete_segments removes expired segments; append_list appends to a previous playlist.", ["delete_segments+append_list", "delete_segments", "independent_segments"]),
    advanced("-live", "output", "Matroska live mode", "1 creates a non-seekable live MKV stream. Keep enabled for pipe:1; only applies to Matroska.", ["0", "1"], "integer", 0, 1),
    advanced("-metadata", "output", "Output metadata", "One key=value metadata entry, quoted if it contains spaces. For multiple entries, edit the raw extra flags directly.", ["title=My stream", "comment=Stalker Proxy Manager"]),
]


def schema():
    from ..models import FFmpegTemplate
    return {"fields": {name: {**definition, "max_length": getattr(
                FFmpegTemplate.__table__.c[name].type, "length", None)
                if name in FFmpegTemplate.__table__.c else None}
            for name, definition in FIELDS.items()}, "advanced": ADVANCED, "applicability": applicability_rules()}


def value_error(value, definition):
    if value == "" or (value == "AUTO" and "AUTO" in (definition.get("choices") or [])):
        return None
    kind = definition["kind"]
    if kind == "rate":
        if not re.fullmatch(r"\d+(?:\.\d+)?[kKmMgG]?", value) or float(value.rstrip("kKmMgG")) <= 0:
            return "use a positive rate such as 2500k or 2.5M"
    elif kind in ("integer", "number", "positive"):
        try:
            if kind == "integer" and not re.fullmatch(r"-?\d+", value):
                raise ValueError
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
        except ValueError:
            return "use a whole number" if kind == "integer" else "use a finite number"
        lo, hi = definition["min"], definition["max"]
        if (lo is not None and (number <= lo if kind == "positive" else number < lo)) or (hi is not None and number > hi):
            return f"value must be {'greater than' if kind == 'positive' else 'at least'} {lo} and at most {hi}"
    return None


def field_errors(opts):
    from ..models import FFmpegTemplate
    from .ffmpeg_templates import ASPECTS, HW_CHOICES, RC_MODES, target_size
    errors = []
    disabled = disabled_parameters(opts)
    for name, definition in FIELDS.items():
        if not hasattr(opts, name):
            continue
        value = str(getattr(opts, name))
        col = FFmpegTemplate.__table__.c[name]
        limit = getattr(col.type, "length", None)
        if limit and len(value) > limit:
            errors.append(f"{name}: maximum {limit} characters in structured fields; use the full command for longer values")
        if name in disabled:
            continue  # latent settings are retained, but do not block another mode
        error = value_error(value, definition)
        if error:
            errors.append(f"{name}: {error}")
    for name in ("video_codec", "audio_codec"):
        if not getattr(opts, name).strip():
            errors.append(f"{name}: choose a codec, copy, or an available custom encoder")
    if opts.hw_accel != "none" and opts.video_codec != "copy" and not opts.device.strip():
        errors.append("device: choose the GPU device visible inside the container")
    if "hw_accel" not in disabled and opts.hw_accel not in HW_CHOICES:
        errors.append("hardware: choose none, vaapi or qsv; use the full command for another decoder")
    if "rc_mode" not in disabled and (opts.rc_mode or "AUTO").upper() not in RC_MODES:
        errors.append("rate control: choose a supported VAAPI mode")
    if "resolution" not in disabled and opts.resolution != "source" and target_size(opts.resolution, opts.aspect) is None:
        errors.append("resolution: use a preset, an even WIDTHxHEIGHT (16–8192), or an even height such as 900p")
    if "aspect" not in disabled and opts.aspect not in ASPECTS and not re.fullmatch(r"[1-9]\d{0,2}:[1-9]\d{0,2}", opts.aspect):
        errors.append("aspect: use a positive width:height ratio such as 16:9")
    for name in ("video_codec", "audio_codec", "profile", "level"):
        if name not in disabled and getattr(opts, name) and not re.fullmatch(r"[\w.:-]+", getattr(opts, name)):
            errors.append(f"{name}: enter one codec/profile/level name, not additional flags")
    return errors


def set_extra_option(raw, side, flag, value):
    """Replace one flag without damaging quoted values or other flags."""
    from .ffmpeg_templates import _safe_extra_tokens, _join_tokens
    if not all(isinstance(v, str) for v in (raw, flag, value)):
        raise ValueError("Flags and values must be text.")
    if side not in ("input", "output"):
        raise ValueError("Choose input or output placement.")
    if not re.fullmatch(r"-[A-Za-z][\w:-]*", flag or "") or flag in ("-i",):
        raise ValueError("Enter a single FFmpeg option such as -probesize, not an input or output target.")
    if any(ch in str(value) for ch in ("\0", "\r", "\n")):
        raise ValueError("Enter a single-line option value.")
    known = next((d for d in ADVANCED if d["flag"] == flag and d["side"] == side), None)
    if known:
        if value in ("", "AUTO"):
            raise ValueError(f"{flag} requires a value. Remove the override in Extra flags to use defaults.")
        error = value_error(str(value), known)
        if error:
            raise ValueError(f"{flag}: {error}")
    try:
        tokens = shlex.split(raw or "")
    except ValueError:
        raise ValueError("Fix unbalanced quotes in the existing extra flags first.") from None
    result = []
    i = 0
    while i < len(tokens):
        if tokens[i] == flag:
            i += 1
            if i < len(tokens) and (not tokens[i].startswith("-") or re.fullmatch(r"-\d+(?:\.\d+)?", tokens[i])):
                i += 1
        else:
            result.append(tokens[i])
            i += 1
    result.append(flag)
    if value != "":
        result.append(str(value))
    text = _join_tokens(result)
    _, warnings = _safe_extra_tokens(text, "extra " + side)
    if warnings:
        raise ValueError("; ".join(warnings) + ". Use the full command for form-owned options.")
    return text
