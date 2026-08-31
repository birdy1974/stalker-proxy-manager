"""
Subtitle handling in transcoded pipes - HARDWARE-ONLY.

The mpegts/HLS pipe can only carry DVB bitmap subtitles. The only subtitle
mode that works without software transcoding is dvb: the track is demuxed and
re-encoded independently of the video, so the VAAPI/QSV pipeline
(hw decode -> scale_vaapi/qsv -> hw encode) stays untouched. Burn-in was
removed (libass needs CPU video frames); these tests pin the two remaining
modes, the 2-way sync of the `subs` field, and the spawn-time gate that
checks every dvb mapping against what the source ACTUALLY carries (probe)
and degrades to plain -sn instead of killing the stream.
"""

from __future__ import annotations

import asyncio

from app.services import stream_manager as sm
from app.services.ffmpeg_templates import (
    COPY_PRESET_NAME, FFmpegOptions, REDIRECT_PRESET_NAME, URL_PLACEHOLDER,
    build_command, coerce_options, default_presets, parse_command,
)
from app.services.stream_manager import StreamManager


SW = dict(hw_accel="none", video_codec="libx264")
VA = dict(hw_accel="vaapi", video_codec="h264_vaapi")


def _argv(cmd, url, **kw):
    return StreamManager._ffmpeg_argv(cmd, url, **kw)


def _specs(args):
    return [args[i + 1] for i, t in enumerate(args) if t == "-map"]


async def _gate(cmd, url, subs, pace=True):
    args = _argv(cmd, url, pace=pace)
    async def fake_probe(target, *, is_url):
        return subs
    orig = sm.subtitle_streams
    sm.subtitle_streams = fake_probe
    try:
        return await StreamManager()._subs_gate(args, url, pace, "Test")
    finally:
        sm.subtitle_streams = orig


# --------------------------------------------------------------------------- #
# template engine: exactly two modes, dvb renders, burn is gone
# --------------------------------------------------------------------------- #
def test_drop_is_the_default_and_pins_sn():
    cmd = build_command(FFmpegOptions(**SW))
    toks = cmd.split()
    assert "-sn" in toks and "0:s?" not in toks and "-c:s" not in toks
    assert parse_command(cmd)["options"]["subs"] == "drop"


def test_dvb_mode_maps_optional_subs_and_reencodes_to_dvbsub():
    cmd = build_command(FFmpegOptions(**SW, subs="dvb"))
    assert "-map 0:s?" in cmd and "-c:s dvbsub" in cmd and "-sn" not in cmd
    parsed = parse_command(cmd)["options"]
    assert parsed["subs"] == "dvb"
    assert build_command(FFmpegOptions(**parsed)) == cmd       # fixed point


def test_dvb_mode_is_hardware_safe_no_cpu_video_filters():
    """The whole point of dvb: the video pipeline stays pure VAAPI - no
    hwdownload, no overlay, no subtitles filter; the sub track is mapped
    beside it."""
    cmd = build_command(FFmpegOptions(**VA, subs="dvb"))
    assert "subtitles=" not in cmd and "hwdownload" not in cmd
    assert "scale_vaapi" in cmd and "-c:v h264_vaapi" in cmd
    assert "-map 0:s?" in cmd and "-c:s dvbsub" in cmd


def test_burn_mode_is_removed():
    assert "burn" not in coerce_options({"subs": "burn"})          # degrades to drop
    assert coerce_options({"subs": "burn"})["subs"] == "drop"
    cmd = build_command(FFmpegOptions(**SW, subs="burn"))          # stale field value
    assert "subtitles=" not in cmd and "-sn" in cmd


def test_dvb_mode_on_a_remux_template_copies_instead_of_reencoding():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source",
                                      subs="dvb"))
    assert "-c:s copy" in cmd


def test_parse_recognises_hand_written_variants():
    dvb = parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:s? "
                        "-c:v copy -c:s dvbsub -f mpegts pipe:1")
    assert dvb["options"]["subs"] == "dvb" and not dvb["warnings"]
    assert parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -sn -c:v copy "
                         "-f mpegts pipe:1")["options"]["subs"] == "drop"
    assert parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -sn -map 0:s? -c:s dvbsub "
                         "-f mpegts pipe:1")["options"]["subs"] == "drop"


def test_parse_strips_a_legacy_burn_filter_with_a_warning():
    r = parse_command(f"ffmpeg -i {URL_PLACEHOLDER} "
                      "-vf scale=1280:720,subtitles=/media/movie.srt "
                      "-c:v libx264 -f mpegts pipe:1")
    assert any("burn" in w.lower() for w in r["warnings"])
    assert "subtitles=" not in (r["options"]["extra_output"] or "")
    assert "subtitles=" not in build_command(FFmpegOptions(**r["options"]))


def test_coerce_normalises_subs():
    assert coerce_options({"subs": "DVB"})["subs"] == "dvb"
    assert coerce_options({"subs": "nonsense"})["subs"] == "drop"
    assert coerce_options({"subs": None}) == {}


def test_every_builtin_preset_keeps_subtitles():
    """Every shipped template keeps the source's subtitles - as DVB bitmap in
    an MPEG-TS pipe, as an untouched copy of ALL tracks in a Matroska one.
    Only the redirect marker is exempt."""
    for p in default_presets():
        if p["name"] == REDIRECT_PRESET_NAME:
            assert p["command"] == "@redirect"
            continue
        cmd = p["command"]
        assert p["subs"] in ("dvb", "keep"), p["name"]
        assert "-map 0:s?" in cmd, p["name"]
        assert "-sn" not in cmd, p["name"]
        if p["subs"] == "keep":
            assert p["output_format"] == "matroska", p["name"]
            assert "-c:s copy" in cmd and "-f matroska" in cmd, p["name"]
        else:
            assert p["output_format"] == "mpegts", p["name"]
            expected = "-c:s copy" if p["name"] == COPY_PRESET_NAME else "-c:s dvbsub"
            assert expected in cmd, p["name"]
        parsed = parse_command(cmd)["options"]
        assert parsed["subs"] == p["subs"], p["name"]
        assert build_command(FFmpegOptions(**parsed)) == cmd, p["name"]


# --------------------------------------------------------------------------- #
# argv rendering: dvb templates are not flattened by the plain net
# --------------------------------------------------------------------------- #
def test_dvb_template_survives_argv_and_legacy_filters_are_stripped():
    dvb = _argv(build_command(FFmpegOptions(**VA, subs="dvb")),
                "http://cdn/live.ts", pace=False)
    assert "-c:s" in dvb and "dvbsub" in dvb
    assert "0:s?" in _specs(dvb)

    # a legacy command with a subtitles= filter loses the filter at spawn time
    legacy = _argv(f"ffmpeg -i {URL_PLACEHOLDER} -vf scale=640:360,subtitles=/x.srt "
                   "-c:v libx264 -f mpegts pipe:1", "/media/movie.mkv", pace=True)
    assert "subtitles=" not in " ".join(legacy)
    assert "-sn" in legacy and "scale=640:360" in " ".join(legacy)


# --------------------------------------------------------------------------- #
# the spawn-time gate (probe monkeypatched)
# --------------------------------------------------------------------------- #
async def test_dvb_keeps_bitmap_tracks_mapped():
    # subtitle_streams() reports SUBTITLE tracks only (probe parse), and the
    # map ordinal counts among them: the 2nd subtitle track (absolute #3 here)
    # maps as `0:s:1`, never as the absolute stream index.
    cmd = build_command(FFmpegOptions(**SW, subs="dvb"))
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 2, "codec": "subrip"},
                        {"index": 3, "codec": "hdmv_pgs_subtitle"}])
    assert _specs(args) == ["0:v:0", "0:a:0?", "0:s:1"]
    assert args[args.index("-c:s") + 1] == "dvbsub"
    assert "-sn" not in args


async def test_dvb_copies_when_source_is_already_dvb():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source",
                                      subs="dvb"))
    args = await _gate(cmd, "/media/live.ts",
                       [{"index": 2, "codec": "dvb_subtitle"}])
    assert "0:s:0" in _specs(args)
    assert args[args.index("-c:s") + 1] == "copy"


async def test_dvb_upgrades_copy_to_dvbsub_for_pgs_sources():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source",
                                      subs="dvb"))
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 3, "codec": "hdmv_pgs_subtitle"}])
    assert args[args.index("-c:s") + 1] == "dvbsub", "raw PGS cannot enter mpegts by copy"
    assert "0:s:0" in _specs(args)


async def test_dvb_drops_text_only_sources():
    cmd = build_command(FFmpegOptions(**SW, subs="dvb"))
    args = await _gate(cmd, "/media/movie.mkv", [{"index": 2, "codec": "subrip"}])
    assert "0:s" not in " ".join(_specs(args))
    assert "-c:s" not in args and "-sn" in args


async def test_dvb_drops_when_probe_fails():
    cmd = build_command(FFmpegOptions(**SW, subs="dvb"))
    args = await _gate(cmd, "/media/movie.mkv", None)
    assert "-c:s" not in args and "-sn" in args


async def test_dvb_trusts_live_without_probing():
    """The hardware-path requirement: live zapping stays instant - no probe,
    the DVB mapping is kept as-is (live TS carries DVB subs natively)."""
    cmd = build_command(FFmpegOptions(**VA, subs="dvb"))
    args = _argv(cmd, "http://cdn/live.ts", pace=False)
    async def fail_probe(target, *, is_url):
        raise AssertionError("live must not be probed")
    orig = sm.subtitle_streams
    sm.subtitle_streams = fail_probe
    try:
        out = await StreamManager()._subs_gate(args, "http://cdn/live.ts", False, "Test")
    finally:
        sm.subtitle_streams = orig
    assert out == args                      # untouched


async def test_legacy_dvbsub_command_is_dvb_intent_and_gated():
    """Templates stored before the -sn fix carried `-map 0:s? -c:s dvbsub`.
    That is dvb intent now: the gate decides per source (dropped for a
    text-only movie, kept for live)."""
    cmd = (f"ffmpeg -i {URL_PLACEHOLDER} -vf scale_vaapi=w=1280:h=720:format=nv12 "
           "-map 0:v:0 -map 0:a:0? -map 0:s? -dn -c:v h264_vaapi -c:a aac "
           "-c:s dvbsub -f mpegts pipe:1")
    args = _argv(cmd, "/media/movie.mkv", pace=True)
    assert "-c:s" in args and "dvbsub" in args          # intent survives argv
    out = await _gate(cmd, "/media/movie.mkv", [{"index": 2, "codec": "subrip"}])
    assert "-c:s" not in out and "-sn" in out           # degraded safely
    assert "0:s" not in " ".join(_specs(out))
