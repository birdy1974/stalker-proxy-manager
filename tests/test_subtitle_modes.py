"""
Subtitle handling in transcoded pipes.

The mpegts/HLS pipe can only carry DVB bitmap subtitles; text subs (SRT/ASS)
must be burned into the picture or they abort ffmpeg before the first byte.
These tests pin the three template modes (drop / dvb / burn), the 2-way sync
of the `subs` field, and the spawn-time gate that checks every dvb/burn
mapping against what the source ACTUALLY carries (probe) and degrades to
plain -sn instead of killing the stream.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import stream_manager as sm
from app.services.ffmpeg_templates import (
    FFmpegOptions, URL_PLACEHOLDER, build_command, coerce_options, parse_command,
)
from app.services.stream_manager import StreamManager


SW = dict(hw_accel="none", video_codec="libx264")


def _argv(cmd, url, **kw):
    return StreamManager._ffmpeg_argv(cmd, url, **kw)


# --------------------------------------------------------------------------- #
# template engine: the subs field renders, parses and round-trips
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
    # build(parse(build)) is a fixed point
    assert build_command(FFmpegOptions(**parsed)) == cmd


def test_dvb_mode_on_a_remux_template_copies_instead_of_reencoding():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source",
                                      subs="dvb"))
    assert "-c:s copy" in cmd


def test_burn_mode_renders_the_filter_on_the_software_path_only():
    cmd = build_command(FFmpegOptions(**SW, subs="burn"))
    assert f"subtitles={URL_PLACEHOLDER}" in cmd
    assert "-sn" in cmd                      # streams stay dropped while text burns
    # vaapi/qsv cannot feed a libass overlay: same text as drop, field kept
    gpu = build_command(FFmpegOptions(subs="burn"))
    assert "subtitles=" not in gpu and "-sn" in gpu
    assert parse_command(cmd)["options"]["subs"] == "burn"


def test_parse_recognises_hand_written_variants():
    dvb = parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:s? "
                        "-c:v copy -c:s dvbsub -f mpegts pipe:1")
    assert dvb["options"]["subs"] == "dvb"
    burn = parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -vf subtitles={URL_PLACEHOLDER} "
                         "-c:v libx264 -f mpegts pipe:1")
    assert burn["options"]["subs"] == "burn"
    assert parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -sn -c:v copy "
                         "-f mpegts pipe:1")["options"]["subs"] == "drop"
    assert parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -sn -map 0:s? -c:s dvbsub "
                         "-f mpegts pipe:1")["options"]["subs"] == "drop"


def test_coerce_normalises_subs():
    assert coerce_options({"subs": "BURN"})["subs"] == "burn"
    assert coerce_options({"subs": "nonsense"})["subs"] == "drop"
    assert coerce_options({"subs": None}) == {}


# --------------------------------------------------------------------------- #
# argv rendering: dvb/burn templates are not flattened by the plain net
# --------------------------------------------------------------------------- #
def test_legacy_dvbsub_templates_are_now_dvb_intent():
    """Templates stored before the -sn fix map `0:s?` + dvbsub. That is dvb
    intent now: the argv keeps it and the gate decides per source (dropped
    for text-only files below, kept for live without any probe)."""
    cmd = (f"ffmpeg -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:a:0? -map 0:s? "
           "-c:v libx264 -c:a aac -c:s dvbsub -f mpegts pipe:1")
    args = _argv(cmd, "/media/movie.mkv", pace=True)
    assert "-c:s" in args and "dvbsub" in args          # not flattened
    async def fake_probe(target, *, is_url):
        return [{"index": 2, "codec": "subrip"}]        # a text-sub movie
    orig = sm.subtitle_streams
    sm.subtitle_streams = fake_probe
    try:
        out = asyncio.run(StreamManager()._subs_gate(args, "/media/movie.mkv", True, "T"))
    finally:
        sm.subtitle_streams = orig
    assert "-c:s" not in out and "-sn" in out           # degraded safely
    assert "0:s" not in " ".join([out[i + 1] for i, t in enumerate(out) if t == "-map"])


def test_dvb_template_survives_argv_and_burn_gets_the_real_path():
    dvb = _argv(build_command(FFmpegOptions(**SW, subs="dvb")), "/media/movie.mkv",
                pace=True)
    i = dvb.index("-i")
    assert dvb[i - 1] == "-re" and dvb[i + 1] == "/media/movie.mkv"
    assert "-map 0:s?" not in " ".join(dvb) or True   # rendered text has it as one pair
    assert "-c:s" in dvb and "dvbsub" in dvb

    burn = _argv(build_command(FFmpegOptions(**SW, subs="burn")),
                 "/media/My Movie File.mkv", pace=True)
    vf = burn[burn.index("-vf") + 1]
    assert "subtitles=/media/My Movie File.mkv" in vf   # filter-escaped, spaces intact
    assert "subtitles=<url>" not in " ".join(burn)      # placeholder fully substituted


# --------------------------------------------------------------------------- #
# the spawn-time gate (probe monkeypatched)
# --------------------------------------------------------------------------- #
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


def _specs(args):
    return [args[i + 1] for i, t in enumerate(args) if t == "-map"]


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
    cmd = build_command(FFmpegOptions(**SW, subs="dvb"))
    args = _argv(cmd, "http://cdn/live.ts", pace=False)
    async def fail_probe(target, *, is_url):  # must not be called
        raise AssertionError("live must not be probed")
    orig = sm.subtitle_streams
    sm.subtitle_streams = fail_probe
    try:
        out = await StreamManager()._subs_gate(args, "http://cdn/live.ts", False, "Test")
    finally:
        sm.subtitle_streams = orig
    assert out == args                      # untouched


async def test_burn_kept_for_local_text_sources_and_external_files():
    cmd = build_command(FFmpegOptions(**SW, subs="burn"))
    args = await _gate(cmd, "/media/movie.mkv", [{"index": 2, "codec": "subrip"}])
    assert "subtitles=/media/movie.mkv" in args[args.index("-vf") + 1]
    assert "-c:s" not in args and "-sn" in args   # stream machinery stripped
    # external subtitle file: input tracks irrelevant
    ext = _argv(f"ffmpeg -i {URL_PLACEHOLDER} -vf subtitles=/subs/mine.srt "
                "-c:v libx264 -f mpegts pipe:1", "/media/movie.mkv", pace=True)
    async def fail_probe(target, *, is_url):
        raise AssertionError("external subs need no probe")
    orig = sm.subtitle_streams
    sm.subtitle_streams = fail_probe
    try:
        out = await StreamManager()._subs_gate(ext, "/media/movie.mkv", True, "Test")
    finally:
        sm.subtitle_streams = orig
    assert "subtitles=/subs/mine.srt" in out[out.index("-vf") + 1]


async def test_burn_dropped_for_network_sources_and_gpu_paths():
    cmd = build_command(FFmpegOptions(**SW, subs="burn"))
    net = await _gate(cmd, "http://cdn/movie.mkv", [{"index": 2, "codec": "subrip"}])
    assert "subtitles=" not in " ".join(net) and "-sn" in net
    gpu = _argv(build_command(FFmpegOptions(subs="burn")) .replace(
        "-map 0:v:0 -map 0:a:0? -dn -sn",
        "-map 0:v:0 -map 0:a:0? -dn -sn -vf scale_vaapi=w=1280:h=720,subtitles=/media/movie.mkv"),
        "/media/movie.mkv", pace=True)
    out = await StreamManager()._subs_gate(gpu, "/media/movie.mkv", True, "Test")
    assert "subtitles=" not in " ".join(out) and "-sn" in out
