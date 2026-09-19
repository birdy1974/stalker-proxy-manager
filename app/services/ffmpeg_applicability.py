"""Shared FFmpeg parameter applicability for rendering, validation and UI.

VAAPI rate-control dependencies follow FFmpeg's vaapi_encode_rc_modes table
and vaapi_encode_init_rate_control (libavcodec/vaapi_encode.c). AUTO is left
unconstrained because the driver determines the eventual mode. Raw extra
flags remain an expert override and are never destructively rewritten here.
"""
import re


def applicability_rules():
    """One declarative rule set for the browser and server-side validation.

    First matching reason wins. Only known dependencies are encoded; protocol
    and arbitrary custom-codec behavior cannot be inferred without the source
    and installed FFmpeg build.
    """
    from .ffmpeg_templates import VAAPI_ENCODERS, VF_PRESETS
    vaapi = list(VAAPI_ENCODERS)

    def rule(targets, reason, **when):
        return {"targets": targets, "reason": reason, "when": when}

    fields = [
        rule(["hw_accel", "device", "resolution", "aspect", "fps", "gop", "profile", "level",
              "vf_preset", "low_power", "rc_mode", "global_quality", "async_depth",
              "video_bitrate", "maxrate", "bufsize"],
             "Video copy bypasses decoding, filtering and encoding.", video_codec={"in": ["copy"]}),
        rule(["device"], "CPU decoding does not use a GPU device.", hw_accel={"not_in": ["vaapi", "qsv"]}),
        rule(["aspect"], "Source resolution does not calculate a new width.", resolution={"in": ["source"]}),
        rule(["aspect"], "Explicit width × height overrides Aspect.", resolution={"regex": r"^\d+x\d+$"}),
        rule(["video_bitrate", "maxrate", "bufsize"], "VAAPI CQP/ICQ uses quality, not bitrate targets.",
             video_codec={"in": vaapi}, rc_mode={"in": ["CQP", "ICQ"]}),
        rule(["maxrate", "bufsize"], "VAAPI AVBR does not enforce peak bitrate or buffer limits.",
             video_codec={"in": vaapi}, rc_mode={"in": ["AVBR"]}),
        rule(["maxrate"], "VAAPI CBR uses the target bitrate; an explicit buffer size also overrides the maxrate buffer fallback.",
             video_codec={"in": vaapi}, rc_mode={"in": ["CBR"]}, bufsize={"regex": r"[1-9]"}),
        rule(["profile", "level"], "These fields are only generated for H.264 encoders.",
             video_codec={"not_in": ["libx264", "h264_vaapi", "h264_qsv"]}),
        rule(["low_power"], "Low-power mode is only generated for h264_vaapi.", video_codec={"not_in": ["h264_vaapi"]}),
        rule(["rc_mode", "async_depth", "global_quality"], "This encoder does not use the template's VAAPI tuning fields.",
             video_codec={"not_in": vaapi}),
        rule(["global_quality"], "Quality is only generated for VAAPI CQP, ICQ or QVBR.", rc_mode={"not_in": ["CQP", "ICQ", "QVBR"]}),
        rule(["audio_bitrate", "audio_channels", "audio_rate"], "Audio copy/none does not encode or resample audio.",
             audio_codec={"in": ["copy", "none"]}),
        rule(["audio_bitrate"], "Lossless audio does not use a target bitrate.", audio_codec={"in": ["flac", "alac"]}),
        rule(["audio_bitrate"], "PCM bitrate is determined by format, channels and sample rate.", audio_codec={"regex": r"^pcm_"}),
    ]
    advanced = [
        rule(["-preset", "-crf", "-tune"], "Video copy does not run a video encoder.", video_codec={"in": ["copy"]}),
        rule(["-preset", "-tune"], "These encoder options are not supported by VAAPI encoders.", video_codec={"regex": r"_vaapi$"}),
        rule(["-tune"], "QSV uses different encoder tuning options.", video_codec={"regex": r"_qsv$"}),
        rule(["-crf"], "This hardware encoder uses its own rate-control/quality options, not CRF.",
             video_codec={"regex": r"_(?:vaapi|qsv|nvenc|amf|v4l2m2m)$"}),
        rule(["-threads"], "Neither video nor audio is being encoded.", video_codec={"in": ["copy"]}, audio_codec={"in": ["copy", "none"]}),
        rule(["-hls_time", "-hls_list_size", "-hls_flags"], "Requires HLS output.", output_format={"not_in": ["hls"]}),
        rule(["-live"], "Requires Matroska output.", output_format={"not_in": ["matroska"]}),
        rule(["-mpegts_flags", "-muxdelay"], "Requires MPEG-TS output.", output_format={"not_in": ["mpegts"]}),
    ]
    options = [rule(["subs:keep"], "Copy all subtitles requires Matroska output.", output_format={"not_in": ["matroska"]})]
    for preset in VF_PRESETS:
        if preset.id != "none":
            paths = [name for name, snippet in (("vaapi", preset.vaapi), ("qsv", preset.qsv), ("none", preset.sw)) if snippet]
            options.append(rule(["vf_preset:" + preset.id], "This filter does not support the selected decoding path.", hw_accel={"not_in": paths}))
    return {"fields": fields, "advanced": advanced, "options": options}


def disabled_parameters(opts, group="fields"):
    values = vars(opts) if not isinstance(opts, dict) else opts
    values = {**values, "rc_mode": str(values.get("rc_mode") or "AUTO").upper()}
    disabled = {}
    for rule in applicability_rules()[group]:
        matches = True
        for key, condition in rule["when"].items():
            value = str(values.get(key, ""))
            if (("in" in condition and value not in condition["in"])
                    or ("not_in" in condition and value in condition["not_in"])
                    or ("regex" in condition and not re.search(condition["regex"], value))):
                matches = False
                break
        if matches:
            for target in rule["targets"]:
                disabled.setdefault(target, rule["reason"])
    return disabled
