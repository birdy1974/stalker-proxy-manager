"""
Matroska output + the `keep` subtitle mode - the VOD/series subtitle path.

Why this exists: MPEG-TS has no slot for text subtitles (SRT/ASS), and ffmpeg
cannot turn text into a bitmap track without rendering it into the picture -
which needs CPU video frames and would defeat hardware-only transcoding. The
only way to deliver VOD/series subtitles from this proxy is therefore to change
the CONTAINER: mux into Matroska and copy every subtitle track through, while
the video is either passed through or re-encoded on the GPU.

Consumer this was built for: an Enigma2 box (Vu+ Duo2 / OpenPLi) playing the
stream with ServiceApp/exteplayer3 (bouquet service reference 5002), which
shows exactly those copied tracks in its subtitle menu.
"""

from __future__ import annotations

from app.services import stream_manager as sm
from app.services.ffmpeg_templates import (
    E2_DUO2_LIVE_PRESET_NAME, E2_VOD_REMUX_PRESET_NAME,
    E2_VOD_TRANSCODE_PRESET_NAME, FFmpegOptions, URL_PLACEHOLDER,
    build_command, coerce_options, default_presets, option_warnings,
    parse_command,
)
from app.services.stream_manager import StreamManager

VA = dict(hw_accel="vaapi", video_codec="h264_vaapi")
MKV = dict(output_format="matroska", subs="keep")


def _specs(args):
    return [args[i + 1] for i, t in enumerate(args) if t == "-map"]


async def _gate(cmd, url, subs, pace=True):
    args = StreamManager._ffmpeg_argv(cmd, url, pace=pace)

    async def fake_probe(target, *, is_url):
        return subs

    orig = sm.subtitle_streams
    sm.subtitle_streams = fake_probe
    try:
        return await StreamManager()._subs_gate(args, url, pace, "Test")
    finally:
        sm.subtitle_streams = orig


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
def test_keep_copies_every_subtitle_track_into_matroska():
    cmd = build_command(FFmpegOptions(hw_accel="none", video_codec="copy",
                                      audio_codec="copy", resolution="source", **MKV))
    assert "-map 0:s?" in cmd and "-c:s copy" in cmd
    assert "-f matroska" in cmd and "-live 1" in cmd and cmd.endswith("pipe:1")
    assert "-sn" not in cmd and "-mpegts_flags" not in cmd


def test_keep_is_hardware_safe_video_stays_on_the_gpu():
    """The point of the mode: the subtitle tracks ride along a video pipeline
    that never leaves VAAPI - no hwdownload, no subtitles= burn filter."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", audio_codec="ac3", **MKV))
    assert "scale_vaapi" in cmd and "-c:v h264_vaapi" in cmd
    assert "hwdownload" not in cmd and "subtitles=" not in cmd
    assert "-c:s copy" in cmd and "-f matroska" in cmd


def test_keep_degrades_to_dvb_on_mpegts_instead_of_lying():
    """MPEG-TS cannot carry SRT/ASS. Rendering `-c:s copy` there would abort at
    the muxer, so the command comes out as the bitmap-only DVB mode - and says
    so when it is parsed back."""
    cmd = build_command(FFmpegOptions(**VA, subs="keep"))       # mpegts default
    assert "-c:s dvbsub" in cmd and "-f mpegts" in cmd
    assert parse_command(cmd)["options"]["subs"] == "dvb"
    assert any("Matroska" in w for w in
               option_warnings(FFmpegOptions(**VA, subs="keep")))
    assert option_warnings(FFmpegOptions(**VA, **MKV)) == []


def test_two_way_sync_fixed_point_for_matroska():
    for opts in (FFmpegOptions(hw_accel="none", video_codec="copy",
                               audio_codec="copy", resolution="source", **MKV),
                 FFmpegOptions(**VA, resolution="1080p", audio_codec="ac3", **MKV)):
        cmd = build_command(opts)
        parsed = parse_command(cmd)["options"]
        assert parsed["output_format"] == "matroska"
        assert parsed["subs"] == "keep"
        assert build_command(FFmpegOptions(**parsed)) == cmd


def test_parse_reads_hand_written_matroska_commands():
    r = parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:a:0? -map 0:s? "
                      "-c:v copy -c:a copy -c:s copy -f mkv pipe:1")
    assert r["options"]["output_format"] == "matroska"
    assert r["options"]["subs"] == "keep"
    # the same maps in a TS command are the bitmap mode, not this one
    assert parse_command(f"ffmpeg -i {URL_PLACEHOLDER} -map 0:s? -c:s copy "
                         "-f mpegts pipe:1")["options"]["subs"] == "dvb"


def test_coerce_normalises_the_container_and_the_new_mode():
    assert coerce_options({"output_format": "MKV"})["output_format"] == "matroska"
    assert coerce_options({"output_format": "Matroska"})["output_format"] == "matroska"
    assert coerce_options({"output_format": "avi"})["output_format"] == "mpegts"
    assert coerce_options({"subs": "KEEP"})["subs"] == "keep"


# --------------------------------------------------------------------------- #
# built-in Enigma2 presets
# --------------------------------------------------------------------------- #
def test_enigma2_presets_are_shipped_and_fit_the_duo2():
    presets = {p["name"]: p for p in default_presets()}

    remux = presets[E2_VOD_REMUX_PRESET_NAME]
    assert remux["video_codec"] == "copy" and remux["audio_codec"] == "copy"
    assert "-f matroska" in remux["command"] and "-c:s copy" in remux["command"]

    hw = presets[E2_VOD_TRANSCODE_PRESET_NAME]
    # the 4K/HEVC rescue path: GPU video, copied subtitles, box-friendly audio
    assert hw["video_codec"] == "h264_vaapi" and hw["resolution"] == "1080p"
    assert hw["profile"] == "high" and hw["level"] == "4.0"
    assert hw["audio_codec"] == "ac3"
    assert "-c:s copy" in hw["command"] and "-f matroska" in hw["command"]
    assert "libx264" not in hw["command"] and "subtitles=" not in hw["command"]

    live = presets[E2_DUO2_LIVE_PRESET_NAME]
    # live stays MPEG-TS: the box renders DVB bitmap subs natively there
    assert live["output_format"] == "mpegts" and live["subs"] == "dvb"
    assert "-f mpegts" in live["command"] and "-c:s dvbsub" in live["command"]


# --------------------------------------------------------------------------- #
# spawn-time gate
# --------------------------------------------------------------------------- #
async def test_gate_leaves_text_subtitles_alone_for_matroska():
    """The dvb gate drops SRT/ASS (a TS cannot take them). With a Matroska
    output there is nothing to drop - that is the whole feature."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 2, "codec": "subrip"},
                        {"index": 3, "codec": "ass"},
                        {"index": 4, "codec": "hdmv_pgs_subtitle"}])
    assert "0:s?" in _specs(args)              # optional map kept as rendered
    assert args[args.index("-c:s") + 1] == "copy"
    assert "-sn" not in args


async def test_gate_maps_around_codecs_matroska_cannot_hold():
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = await _gate(cmd, "/media/movie.mkv",
                       [{"index": 2, "codec": "dvb_teletext"},
                        {"index": 3, "codec": "subrip"}])
    assert _specs(args) == ["0:v:0", "0:a:0?", "0:s:1"]      # teletext skipped
    assert args[args.index("-c:s") + 1] == "copy"


async def test_gate_drops_when_only_unsupported_tracks_exist():
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = await _gate(cmd, "/media/movie.mkv", [{"index": 2, "codec": "dvb_teletext"}])
    assert "-c:s" not in args and "-sn" in args


async def test_gate_keeps_the_optional_map_when_the_probe_fails():
    """`-map 0:s?` is optional, so an unprobeable source costs nothing: no
    subtitle track simply means no subtitle track, not a dead stream."""
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    for probe in (None, []):
        args = await _gate(cmd, "http://cdn/movie.mkv", probe)
        assert "0:s?" in _specs(args) and args[args.index("-c:s") + 1] == "copy"


async def test_gate_does_not_probe_live_matroska():
    cmd = build_command(FFmpegOptions(**VA, resolution="1080p", **MKV))
    args = StreamManager._ffmpeg_argv(cmd, "http://cdn/live.ts", pace=False)

    async def fail_probe(target, *, is_url):
        raise AssertionError("live must not be probed")

    orig = sm.subtitle_streams
    sm.subtitle_streams = fail_probe
    try:
        out = await StreamManager()._subs_gate(args, "http://cdn/live.ts", False, "Test")
    finally:
        sm.subtitle_streams = orig
    assert out == args


# --------------------------------------------------------------------------- #
# HTTP surface: the .mkv aliases
# --------------------------------------------------------------------------- #
async def test_mkv_urls_exist_next_to_the_ts_ones():
    """Same items, same auth, same pipeline - only the announced container
    differs. Enigma2 bouquets point VOD/series at `.mkv` because set-top boxes
    sniff the extension; the routing must therefore exist for every kind."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.main import app, _seed_defaults
    from app.models import (
        FFmpegTemplate, LivePlaylist, LivePlaylistSource, LiveSource, Portal, User,
    )

    await _seed_defaults()
    async with SessionLocal() as s:
        tpl = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == E2_VOD_REMUX_PRESET_NAME))).scalar_one()
        assert tpl.is_builtin is True and tpl.output_format == "matroska"
        portal = Portal(name="p", base_url="http://127.0.0.1:1/c/")
        s.add(portal)
        await s.flush()
        src = LiveSource(portal_id=portal.id, portal_channel_id="1",
                         original_name="Ch", cmd="ffmpeg http://x/1.ts", enabled=True)
        s.add(src)
        await s.flush()
        pl = LivePlaylist(custom_name="Ch", enabled=True, ffmpeg_template_id=tpl.id)
        s.add(pl)
        await s.flush()
        s.add(LivePlaylistSource(live_playlist_id=pl.id, live_source_id=src.id,
                                 priority=1))
        s.add(User(name="mkv", password="pw", enabled=True, m3u_enabled=True))
        await s.commit()
        pid = pl.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        # dead portal -> 404 "no available source", but the ROUTE resolved and
        # authenticated (a missing route would be 404 with a FastAPI detail, and
        # bad credentials would be 403)
        r = await c.get(f"/play/live/{pid}.mkv?u=mkv&p=pw")
        assert r.status_code == 404 and "no available source" in r.text
        assert (await c.get(f"/play/vod/1.mkv?u=mkv&p=pw")).status_code in (404, 502)
        assert (await c.get(f"/play/episode/1.mkv?u=mkv&p=pw")).status_code in (404, 502)
        assert (await c.get(f"/movie/mkv/pw/1.mkv")).status_code in (404, 502)
        assert (await c.get(f"/series/mkv/pw/1.mkv")).status_code in (404, 502)
        # credentials are still enforced on the new routes
        assert (await c.get(f"/play/live/{pid}.mkv?u=mkv&p=wrong")).status_code == 403
