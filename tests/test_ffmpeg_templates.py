"""
The DS918+ (Apollo Lake / iHD driver) VAAPI template.

The reference spec command is kept reproducible, extended with the three knobs
that make VAAPI encoding actually fast and deterministic on that silicon:

  * -low_power 1   -> VAEntrypointEncSliceLP (fixed-function H.264 encoder)
  * -rc_mode vbr   -> explicit rate control (auto is driver-dependent)
  * -async_depth 4 -> more frames in flight

These tests pin both directions of the 2-way sync so a GUI edit can never
silently drop them.
"""

from __future__ import annotations

from app.services.ffmpeg_templates import (
    FFmpegOptions, REDIRECT_COMMAND, REDIRECT_PRESET_NAME, REFERENCE_PRESET_NAME,
    build_command, default_presets, parse_command, URL_PLACEHOLDER,
)


def test_vaapi_720p_command_is_the_optimised_reference():
    cmd = build_command(FFmpegOptions())
    assert cmd.startswith("ffmpeg ")
    # resilient input flags (spec reference)
    assert "-rw_timeout 10000000" in cmd
    assert "-reconnect 1" in cmd and "-reconnect_at_eof 1" in cmd
    # full GPU decode + scale + encode pipeline (no CPU round-trip)
    assert "-init_hw_device vaapi=intel:/dev/dri/renderD128" in cmd
    assert "-hwaccel vaapi" in cmd
    assert "-hwaccel_output_format vaapi" in cmd
    assert "scale_vaapi=w=1280:h=720:format=nv12" in cmd
    # the optimisations this change adds
    assert "-c:v h264_vaapi" in cmd
    assert "-low_power 1" in cmd
    assert "-rc_mode vbr" in cmd
    assert "-async_depth 4" in cmd
    assert "-f mpegts" in cmd and "+resend_headers" in cmd
    assert URL_PLACEHOLDER in cmd


def test_low_power_is_emitted_only_for_h264_vaapi():
    """Apollo Lake has no fixed-function encoder for HEVC (vainfo: HEVC has
    EncSlice but no EncSliceLP), so -low_power must not leak into hevc_vaapi."""
    hevc = build_command(FFmpegOptions(video_codec="hevc_vaapi"))
    assert "-low_power" not in hevc
    assert "-rc_mode vbr" in hevc and "-async_depth 4" in hevc
    # but it is there for h264_vaapi
    assert "-low_power 1" in build_command(FFmpegOptions(video_codec="h264_vaapi"))


def test_software_and_copy_templates_get_no_vaapi_flags():
    sw = build_command(FFmpegOptions(hw_accel="none", video_codec="libx264"))
    assert "-low_power" not in sw and "-rc_mode" not in sw and "-async_depth" not in sw
    assert "-init_hw_device" not in sw
    cp = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                     audio_codec="copy", resolution="source"))
    assert "-c:v copy" in cp and "-low_power" not in cp


def test_low_power_and_rc_mode_are_toggleable():
    off = build_command(FFmpegOptions(low_power=False, rc_mode="auto"))
    assert "-low_power" not in off
    assert "-rc_mode" not in off
    cbr = build_command(FFmpegOptions(rc_mode="cbr"))
    assert "-rc_mode cbr" in cbr


def test_parse_command_recovers_the_new_fields():
    cmd = build_command(FFmpegOptions())
    res = parse_command(cmd)
    o = res["options"]
    assert o["hw_accel"] == "vaapi"
    assert o["video_codec"] == "h264_vaapi"
    assert o["low_power"] is True
    assert o["rc_mode"] == "vbr"
    assert o["async_depth"] == "4"
    assert o["video_bitrate"] == "1000k"
    assert o["resolution"] == "720p"
    assert res["warnings"] == []


def test_parse_command_normalises_numeric_rc_mode():
    res = parse_command("ffmpeg -i <url> -c:v h264_vaapi -rc_mode 2 -low_power 0 "
                        "-async_depth 8 -f mpegts pipe:1")
    o = res["options"]
    assert o["rc_mode"] == "cbr"
    assert o["low_power"] is False
    assert o["async_depth"] == "8"


def test_default_presets_ship_the_optimised_vaapi_commands():
    presets = {p["name"]: p for p in default_presets()}
    vaapi = presets[REFERENCE_PRESET_NAME]
    assert "-low_power 1" in vaapi["command"]
    assert "-rc_mode vbr" in vaapi["command"]
    assert "-async_depth 4" in vaapi["command"]
    # every preset's stored command must match its structured fields (2-way sync);
    # the redirect preset is exempt: it is a marker, not an ffmpeg command.
    for name, p in presets.items():
        if name == REDIRECT_PRESET_NAME:
            continue
        fields = {k: v for k, v in p.items() if k in FFmpegOptions.__dataclass_fields__}
        assert build_command(FFmpegOptions(**fields)) == p["command"], name


def test_redirect_preset_is_a_sentinel_not_a_command():
    """The per-channel 'bypass ffmpeg' preset ships as a marker row the stream
    path recognises, not as a real (and bogus) ffmpeg command."""
    presets = {p["name"]: p for p in default_presets()}
    redirect = presets[REDIRECT_PRESET_NAME]
    assert redirect["command"] == REDIRECT_COMMAND
    assert redirect["enabled"] is True
    # still carries the full structured field set so the seeder treats it
    # like any other template
    for f in FFmpegOptions.__dataclass_fields__:
        assert f in redirect, f
    assert build_command(FFmpegOptions()) != REDIRECT_COMMAND


def test_dreambox_preset_targets_mpeg2_ts_for_enigma2():
    """The DM800se (Enigma2/openpli, ancient) needs an MPEG-2 transport stream:
    H.264 Main@3.1 video + MPEG-1 Layer II audio (mp2) at a modest SD bitrate."""
    dreambox = {p["name"]: p for p in default_presets()}[
        "Dreambox DM800se (Enigma2 / MPEG2-SD)"]
    assert dreambox["resolution"] == "576p"
    assert dreambox["video_codec"] == "h264_vaapi"
    assert dreambox["profile"] == "main" and dreambox["level"] == "3.1"
    assert dreambox["audio_codec"] == "mp2"
    assert dreambox["output_format"] == "mpegts"
    assert "-c:v h264_vaapi" in dreambox["command"]
    assert "-c:a mp2" in dreambox["command"]
    assert "-profile:v main" in dreambox["command"]
    assert "scale_vaapi=w=1024:h=576:format=nv12" in dreambox["command"]
    assert "-f mpegts" in dreambox["command"]
